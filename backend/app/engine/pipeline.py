"""run 执行编排：fencing → reset → seed → 向量就绪 → search → 指标 → finalize（EP-8）。

**编排的核心不是「按顺序调 7 个接口」，而是每一步前后的两个判断**：

1. **动手前先确认自己仍是权威持有者**。fencing acquire 失败（Java 返回 E0403）
   说明这个 run 已经是僵尸——它的期望版本过期了，另一个 run 接管了命名空间。
   此时必须**立刻停手并记失败**，绝不能继续往别人正在用的命名空间里写。
   这是 EP-8 验收条款「zombie 旧 token 被 Java 拒绝后平台能正确记录失败」。

2. **每个阶段之间续租**。租约是「我还活着」的唯一凭证；失去租约（被回收/被接管/
   被取消）后继续干活，产出的就是无人认领的垃圾，还可能与新持有者互相覆盖。

**finalize 的顺序是有硬约束的**（EP-8 验收条款「finalize 清理失败不会提前释放
run 槽位」）：清理 → 释放 fencing → CAS 置成功。清理失败时**不释放 fencing**——
命名空间里可能留着清理不掉的记忆，让继任者立刻复用它等于把脏数据喂给下一次评测。
宁可让 fencing 超时自然失效，也不要主动交出一个可能不干净的命名空间。

任务体把每一步都记进 ``checkpoint.completed_stages``，使崩溃重启后能看出「跑到哪了」。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connector import JavaEvalClient, JavaEvalError
from app.connector.schemas import SeedItem
from app.datasets.models import Case
from app.engine.dimensions.retrieval import (
    RetrievalCaseResult,
    RetrievalEvaluator,
    RetrievedItem,
)
from app.results import DIMENSION_RETRIEVAL, ResultReader, ResultWriter
from app.runs.lease import LeaseManager
from app.runs.models import Run
from app.settings.logging import get_logger

logger = get_logger(__name__)


class Stage(str, Enum):
    """阶段的**顺序即依赖**：后面的阶段依赖前面阶段产生的状态。"""

    FENCING = "fencing"
    RESET = "reset"
    SEED = "seed"
    VECTOR_READY = "vector_ready"
    SEARCH = "search"
    METRICS = "metrics"
    FINALIZE = "finalize"


class PipelineAbort(Exception):
    """中止执行但**不**终结 run（交由 reaper 处理）。

    用于「已失去租约」这类情形：这个 run 已经不归我们管，此刻写任何终态都是
    越权——真正的持有者可能正在跑它。
    """


class PipelineFailure(Exception):
    """执行失败，应把 run 置为 failed。

    ``should_release_fencing`` 决定失败时是否交出命名空间：清理阶段失败时为 False
    （见模块 docstring）。
    """

    def __init__(self, message: str, *, stage: Stage, should_release_fencing: bool = True) -> None:
        super().__init__(message)
        self.stage = stage
        self.should_release_fencing = should_release_fencing


@dataclass
class PipelineOutcome:
    """一次执行的结局，供任务体记日志与审计。"""

    run_id: uuid.UUID
    status: str  # succeeded / failed / not_owned
    completed_stages: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    error: str | None = None


class RunPipeline:
    """单次 run 的执行编排。

    刻意把 Java 交互、数据库、时钟都做成可注入的，使编排逻辑能在**不连 Java、
    不连 Redis** 的情况下被完整测试——编排里全是分支与顺序约束，
    这些才是最容易写错的地方。
    """

    def __init__(
        self,
        db: Session,
        client_factory: Callable[[], JavaEvalClient],
        *,
        owner: str,
        evaluator: RetrievalEvaluator | None = None,
        vector_ready_timeout: float = 60.0,
        vector_ready_poll_interval: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._db = db
        self._client_factory = client_factory
        self._owner = owner
        self._lease = LeaseManager(db)
        self._evaluator = evaluator or RetrievalEvaluator()
        self._vector_ready_timeout = vector_ready_timeout
        self._vector_ready_poll_interval = vector_ready_poll_interval
        self._sleep = sleep
        #: 当前正在执行的阶段；异常上报靠它带出正确的阶段名（见 :meth:`_stage`）
        self._current_stage: Stage | None = None

    # -- 对外入口 ---------------------------------------------------------

    def execute(self, run_id: uuid.UUID) -> PipelineOutcome:
        """领取租约并跑完整条流水线。

        不抛异常：所有失败都转成 ``PipelineOutcome``，因为调用方是 Celery 任务，
        抛出会让任务进入重试队列，而这个 run 的状态已经由本方法写清楚了。
        """
        claim = self._lease.claim(run_id, self._owner)
        if not claim.claimed:
            # 不是错误：这个 run 现在归别人（或已终态），正确的动作是什么都不做。
            logger.info("run 未被本 worker 领取，跳过", extra={"run_id": str(run_id)})
            return PipelineOutcome(run_id=run_id, status="not_owned")

        run = self._lease.get_run(run_id)
        if run is None:  # pragma: no cover — 刚领取成功却查不到，属数据异常
            return PipelineOutcome(run_id=run_id, status="failed", error="领取成功后查不到 run")

        completed: list[str] = list(run.checkpoint.get("completed_stages") or [])
        try:
            metrics = self._run_stages(run, completed)
        except PipelineAbort as exc:
            # 失去租约：不写终态，交给 reaper。
            logger.warning("流水线中止（租约已易主）: %s", exc, extra={"run_id": str(run_id)})
            return PipelineOutcome(
                run_id=run_id, status="aborted", completed_stages=completed, error=str(exc)
            )
        except PipelineFailure as exc:
            return self._fail(run, exc, completed)
        except JavaEvalError as exc:
            # 用 self._current_stage 而不是硬编码：曾把 fencing 阶段的失败标成 search，
            # 排障时直接把人引到错误的阶段。阶段标注错了比不标更糟。
            return self._fail(
                run, PipelineFailure(str(exc), stage=self._current_stage or Stage.FENCING), completed
            )

        return PipelineOutcome(
            run_id=run_id, status="succeeded", completed_stages=completed, metrics=metrics
        )

    # -- 阶段推进 ---------------------------------------------------------

    @contextmanager
    def _stage(self, stage: Stage) -> Iterator[None]:
        """标记当前阶段，供异常上报使用。

        阶段列表只在这里出现一次——与流水线的实际顺序同源，改顺序不必改两处。
        """
        self._current_stage = stage
        yield

    def _run_stages(self, run: Run, completed: list[str]) -> dict[str, float]:
        """按序推进各阶段；已完成的阶段可跳过（断点续跑的基础）。"""
        with self._client_factory() as client:
            with self._stage(Stage.FENCING):
                self._stage_fencing(run, client, completed)
            with self._stage(Stage.RESET):
                self._stage_reset(run, client, completed)
            with self._stage(Stage.SEED):
                self._stage_seed(run, client, completed)
            with self._stage(Stage.VECTOR_READY):
                self._stage_vector_ready(run, client, completed)
            with self._stage(Stage.SEARCH):
                collected = self._stage_search(run, client, completed)
            with self._stage(Stage.METRICS):
                metrics = self._stage_metrics(run, collected, completed)
            with self._stage(Stage.FINALIZE):
                self._stage_finalize(run, client, completed, metrics)
        return metrics

    def _check_lease(self, run: Run) -> None:
        """续租。失败即中止——不能再往下写任何东西。"""
        if not self._lease.heartbeat(run.id, self._owner):
            raise PipelineAbort("失去租约（被回收/接管/取消）")

    def _mark_stage(self, run: Run, completed: list[str], stage: Stage) -> None:
        """记录阶段完成。写入 ``checkpoint`` 使崩溃重启后能看出跑到哪了。"""
        if stage.value not in completed:
            completed.append(stage.value)
        # 重新赋值整个 dict：JSONB 列的原地修改不会被 SQLAlchemy 侦测到。
        run.checkpoint = {**run.checkpoint, "completed_stages": list(completed)}
        run.current_stage = stage.value
        self._db.flush()

    # -- 各阶段 -----------------------------------------------------------

    def _stage_fencing(self, run: Run, client: JavaEvalClient, completed: list[str]) -> None:
        """读取权威态 → 抢占 → 把抢占到的版本**镜像**回 Postgres。

        「镜像」的意义：Java 侧是权威，但平台每次都要读权威态才知道自己是否还有效；
        把版本写回 ``eval_runs.fencing_version``，排障时不必反查 Java 就能看出
        这个 run 当时持有的版本，也便于与 reaper 的接管记录对账。
        """
        self._check_lease(run)

        state = client.get_fencing(run.eval_user_id)
        result = client.acquire_fencing(run.eval_user_id, state.fencing_version, self._owner)

        if not result.acquired:
            # 期望版本过期 = 另一个 run 已经接管。**这就是僵尸**。
            # 不能重试（版本不会因为重试而变对），也不能继续（会写坏别人的命名空间）。
            raise PipelineFailure(
                f"fencing 抢占失败：期望版本 {state.fencing_version} 已过期，"
                f"当前权威版本 {result.version}。本 run 已被接管（zombie）。",
                stage=Stage.FENCING,
                should_release_fencing=False,  # 我们没抢到，无权释放别人的
            )

        run.fencing_version = result.version
        self._db.flush()
        self._mark_stage(run, completed, Stage.FENCING)
        logger.info(
            "fencing 抢占成功: version=%s", result.version, extra={"run_id": str(run.id)}
        )

    def _stage_reset(self, run: Run, client: JavaEvalClient, completed: list[str]) -> None:
        """清空命名空间。必须在 seed 之前——残留的旧记忆会让 Recall 虚高。"""
        if Stage.RESET.value in completed:
            return
        self._check_lease(run)

        result = client.reset(run.eval_user_id, run_id=self._owner, fencing_version=run.fencing_version)
        logger.info(
            "reset 完成: 删除 %s 条", result.mysql_deleted, extra={"run_id": str(run.id)}
        )
        self._mark_stage(run, completed, Stage.RESET)

    def _stage_seed(self, run: Run, client: JavaEvalClient, completed: list[str]) -> int:
        """把该数据集版本的全部 ground-truth 记忆灌进命名空间。

        语料取**所有** case 的 ground truth（不只是当前 query 的），使检索时存在
        真实的干扰项——只灌正确答案的话，检索随便返回什么都容易命中，指标虚高。
        """
        if Stage.SEED.value in completed:
            return 0
        self._check_lease(run)

        items = self._collect_seed_items(run)
        if not items:
            raise PipelineFailure("数据集版本没有可 seed 的记忆内容", stage=Stage.SEED)

        outcome = client.seed(
            run.eval_user_id, items, run_id=self._owner, fencing_version=run.fencing_version
        )
        logger.info(
            "seed 完成: inserted=%s existed=%s", outcome.inserted, outcome.existed,
            extra={"run_id": str(run.id)},
        )
        self._mark_stage(run, completed, Stage.SEED)
        return outcome.inserted + outcome.existed

    def _collect_seed_items(self, run: Run) -> list[SeedItem]:
        """汇总该版本所有 ground-truth 内容作为语料，按内容去重。"""
        cases = list(
            self._db.execute(
                select(Case).where(Case.dataset_version_id == run.dataset_version_id)
            ).scalars()
        )

        seen: set[str] = set()
        items: list[SeedItem] = []
        for case in cases:
            for content, memory_type in _ground_truth_entries(case):
                if content in seen:
                    continue
                seen.add(content)
                items.append(SeedItem(type=memory_type, content=content))
        return items

    def _stage_vector_ready(self, run: Run, client: JavaEvalClient, completed: list[str]) -> None:
        """向量就绪屏障（EP-8 验收条款「HNSW 未 ready 不进入指标计算」）。

        **exact 模式跳过**：精确检索直接读 MySQL，不依赖向量索引同步；
        对它做屏障只会白白拖慢每次运行。

        hnsw 模式必须等 ``vectorSyncPendingCount`` 归零：seed 写入 MySQL 后，
        向量是异步同步过去的，此时立刻检索会漏召回——**而漏召回会表现为
        Recall 偏低，看起来像检索算法退步，实际是评测平台自己抢跑了**。
        """
        if Stage.VECTOR_READY.value in completed:
            return
        self._check_lease(run)

        if run.mode != "hnsw":
            logger.info("exact 模式，跳过向量就绪屏障", extra={"run_id": str(run.id)})
            self._mark_stage(run, completed, Stage.VECTOR_READY)
            return

        deadline = time.monotonic() + self._vector_ready_timeout
        while True:
            self._check_lease(run)
            pending = client.metrics().vector_sync_pending_count
            if pending == 0:
                logger.info("向量已就绪", extra={"run_id": str(run.id)})
                self._mark_stage(run, completed, Stage.VECTOR_READY)
                return

            if time.monotonic() >= deadline:
                raise PipelineFailure(
                    f"向量同步超时：仍有 {pending} 条待同步（等待 {self._vector_ready_timeout}s）",
                    stage=Stage.VECTOR_READY,
                )
            self._sleep(self._vector_ready_poll_interval)

    def _stage_search(
        self, run: Run, client: JavaEvalClient, completed: list[str]
    ) -> list[tuple[Case, list[RetrievedItem]]]:
        """对每个 query case 跑检索，收集结果（此时还不算指标）。"""
        if Stage.SEARCH.value in completed:
            return []
        self._check_lease(run)

        cases = [
            case
            for case in self._db.execute(
                select(Case).where(
                    Case.dataset_version_id == run.dataset_version_id,
                    Case.case_type == "query_to_memory",
                    # 排序是**截断正确性的前提**：没有 ORDER BY 时 PostgreSQL 返回行的
                    # 顺序不保证稳定（并发插入、vacuum、计划切换都会改变它），
                    # 于是「限量 10 条」两次跑到的可能是不同的 10 条——指标不可复现，
                    # 而这正是本平台最不能出的问题。按 id 排序给出一个确定且便宜的序。
                ).order_by(Case.id)
            ).scalars()
        ]
        cases = self._apply_case_limit(run, cases)

        collected: list[tuple[Case, list[RetrievedItem]]] = []
        for case in cases:
            # 每个 case 前都续租：一个几百条的评测集可能跑很久，
            # 只在阶段之间续租的话，单阶段超时仍会被误判为死亡。
            self._check_lease(run)
            response = client.search(
                run.eval_user_id,
                str(case.payload.get("query") or ""),
                top_k=self._evaluator.k,
                exact=(run.mode == "exact"),
            )
            collected.append(
                (case, [RetrievedItem.from_search_item(item) for item in response.items])
            )

        logger.info(
            "search 完成: %d 个 case", len(collected), extra={"run_id": str(run.id)}
        )
        self._mark_stage(run, completed, Stage.SEARCH)
        return collected

    def _apply_case_limit(self, run: Run, cases: list[Case]) -> list[Case]:
        """按 ``run.case_limit`` 截断取数范围。

        **截断放在 search 之前，而不是算完指标再丢弃**：限量跑的全部意义是省时间，
        而时间几乎全花在逐 case 的检索调用上——先跑 1000 条再取前 10 条，
        省下的是零。这也是为什么 case_limit 是刚需：抽取维度每条 case 要调一次 LLM。

        记录实际取到的条数到 checkpoint：``case_limit=10`` 而数据集只有 3 条时，
        指标是基于 3 条算的——这个差异必须能看出来，否则趋势图上会莫名其妙地跳。
        """
        limited = cases if run.case_limit is None else cases[: run.case_limit]
        if run.case_limit is not None and len(limited) != run.case_limit:
            logger.info(
                "case_limit=%s 大于可用 case 数，实际取 %d 条",
                run.case_limit,
                len(limited),
                extra={"run_id": str(run.id)},
            )
        run.checkpoint = {
            **run.checkpoint,
            "cases_available": len(cases),
            "cases_selected": len(limited),
        }
        self._db.flush()
        return limited

    def _stage_metrics(
        self,
        run: Run,
        collected: list[tuple[Case, list[RetrievedItem]]],
        completed: list[str],
    ) -> dict[str, float]:
        """算指标 → 落库。此阶段不碰网络，纯计算。"""
        if Stage.METRICS.value in completed:
            # 断点续跑：指标已经算过并落库了，从库里读回来而不是返回空字典。
            # 返回空字典会让 finalize 把 result_summary 写成 {"retrieval": {}}，
            # 于是一个「续跑成功」的 run 在 API 上看起来像「什么都没算出来」。
            return ResultReader(self._db).dimension_metrics(run.id, DIMENSION_RETRIEVAL)

        results: list[RetrievalCaseResult] = [
            self._evaluator.evaluate_case(
                payload=case.payload, ground_truth=case.ground_truth, retrieved=retrieved
            )
            for case, retrieved in collected
        ]
        metrics = self._evaluator.aggregate(results)

        self._persist_metrics(run, collected, results, metrics)

        # checkpoint 里仍留一份截断明细：它是「run 对象自带的一眼可见视图」，
        # 便于只看一个 run 的 JSON 就能判断发生了什么的场景（含 case 数超限标记）。
        # **权威存储在 eval_run_results / eval_case_results**，趋势查询与逐条追溯
        # 都走那两张表——JSONB 上做跨 run 聚合不可行。
        run.checkpoint = {
            **run.checkpoint,
            "case_details": [result.as_detail() for result in results[:200]],
            "case_details_truncated": len(results) > 200,
        }
        run.result_summary = {"retrieval": metrics, "case_total": len(results)}
        self._db.flush()

        self._mark_stage(run, completed, Stage.METRICS)
        return metrics

    def _persist_metrics(
        self,
        run: Run,
        collected: list[tuple[Case, list[RetrievedItem]]],
        results: list[RetrievalCaseResult],
        metrics: dict[str, float],
    ) -> None:
        """把维度级聚合与逐 case 明细写进结果表。

        ``collected`` 与 ``results`` 是**位置一一对应**的（同一列表推导产生），
        用 ``zip`` 关联而非按 query_id 查表：query_id 允许重复（schema 只要求非空），
        按它关联会在重复时静默错配到错误的那条 case。
        """
        writer = ResultWriter(self._db)
        writer.write_dimension_metrics(
            run.id,
            DIMENSION_RETRIEVAL,
            metrics,
            detail={"match_mode": results[0].match_mode if results else None},
        )
        writer.write_case_metrics(
            run.id,
            DIMENSION_RETRIEVAL,
            (
                (case.id, result.as_metric_values(), result.as_detail())
                for (case, _), result in zip(collected, results, strict=True)
            ),
        )

    def _stage_finalize(
        self,
        run: Run,
        client: JavaEvalClient,
        completed: list[str],
        metrics: dict[str, float],
    ) -> None:
        """清理 → 释放 fencing → CAS 置成功。**顺序不可调换。**

        清理失败时抛 ``should_release_fencing=False``：命名空间可能还脏着，
        交出去等于让下一次评测读串味。宁可等 fencing 超时自然失效。
        """
        self._check_lease(run)

        try:
            client.reset(run.eval_user_id, run_id=self._owner, fencing_version=run.fencing_version)
        except JavaEvalError as exc:
            raise PipelineFailure(
                f"finalize 清理失败: {exc}",
                stage=Stage.FINALIZE,
                should_release_fencing=False,  # 关键：不交出可能脏着的命名空间
            ) from exc

        # 清理已确认成功，此时才交出命名空间。
        released = client.release_fencing(run.eval_user_id, self._owner)
        if not released:
            # 释放失败不致命：fencing 会超时自然失效。但必须留痕，否则排障时
            # 会以为「释放过了」，实际命名空间还被占着。
            logger.warning(
                "finalize 释放 fencing 返回 False（可能已被接管）", extra={"run_id": str(run.id)}
            )

        self._mark_stage(run, completed, Stage.FINALIZE)
        if not self._lease.finish(
            run.id, self._owner, status="succeeded", result_summary={"retrieval": metrics}
        ):
            # 走到这里说明租约在 finalize 期间易主：结果没写进去，
            # 但工作已经做完了——这属于「白干」，记录清楚即可。
            logger.warning(
                "finalize 完成但终态写入被拒（租约已易主）", extra={"run_id": str(run.id)}
            )

    # -- 失败处理 ---------------------------------------------------------

    def _fail(self, run: Run, failure: PipelineFailure, completed: list[str]) -> PipelineOutcome:
        """把失败写进 run 终态。

        **顺序是刻意的：先写终态，再做「尽力而为」的清理。** 反过来写过一次出过事故——
        「释放 fencing」抛了非 ``JavaEvalError`` 的异常（httpx 对非 ASCII 头值抛
        ``UnicodeEncodeError``），异常从失败路径逃逸、被任务体的兜底 except 回滚，
        于是**失败记录整条丢失**，run 卡在 pending 且没有任何痕迹。
        先写终态意味着即便后续清理炸了，失败也已经落库、可查、可被 reaper 处理。

        ``should_release_fencing`` 由抛出点决定：fencing 抢占失败时为 False
        （我们没抢到，无权释放别人的），finalize 清理失败时也为 False
        （命名空间可能还脏着，见模块 docstring）。
        """
        # 把失败的阶段也落到 run 上，而不只是写进 error_message 文本里——
        # 「这个 run 死在哪个阶段」是可以直接查询/聚合的字段，不该只存在于一段自由文本中。
        run.current_stage = failure.stage.value
        self._db.flush()

        self._lease.finish(
            run.id, self._owner, status="failed", error_message=f"[{failure.stage.value}] {failure}"
        )
        logger.error(
            "run 执行失败 stage=%s: %s", failure.stage.value, failure,
            extra={"run_id": str(run.id), "retryable": False},
        )

        if failure.should_release_fencing:
            self._release_fencing_quietly(run)

        return PipelineOutcome(
            run_id=run.id, status="failed", completed_stages=completed, error=str(failure)
        )

    def _release_fencing_quietly(self, run: Run) -> None:
        """尽力释放 fencing；失败只记日志。

        捕获 ``Exception`` 而非只捕 ``JavaEvalError``：这里的目标是「绝不因为清理
        动作本身而破坏失败记录」。放宽捕获范围的代价是可能吞掉意外异常，
        但**此时真正的失败原因已经写进 run 了**，这里再抛只会覆盖掉它。
        （实跑教训：只捕 JavaEvalError 时，httpx 的 UnicodeEncodeError 逃逸，
        把整条失败路径带崩。）
        """
        try:
            with self._client_factory() as client:
                client.release_fencing(run.eval_user_id, self._owner)
        except Exception as exc:  # noqa: BLE001 — 见 docstring：此处就是要兜住一切
            logger.warning(
                "失败路径释放 fencing 未成功（不致命）: %s: %s",
                type(exc).__name__,
                exc,
                extra={"run_id": str(run.id)},
            )


def _ground_truth_entries(case: Case) -> list[tuple[str, str]]:
    """从一条 case 的 ground truth 里取出 (内容, 记忆类型) 列表。

    两类 case 的 ground-truth 结构不同（见 `app.datasets.schemas`），
    这里统一成同一种形态供 seed 使用。
    """
    ground_truth: dict[str, Any] = case.ground_truth or {}
    entries: list[tuple[str, str]] = []

    for memory in ground_truth.get("ground_truth_memories") or []:
        if isinstance(memory, dict) and isinstance(memory.get("content"), str):
            entries.append((memory["content"], str(memory.get("type") or "fact")))

    for content in ground_truth.get("relevant_memory_contents") or []:
        if isinstance(content, str) and content.strip():
            entries.append((content, "fact"))

    return entries


__all__ = [
    "PipelineAbort",
    "PipelineFailure",
    "PipelineOutcome",
    "RunPipeline",
    "Stage",
]
