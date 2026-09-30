"""run 执行编排的集成测试（EP-8）。

Java 侧全部用 respx 拦截，Postgres 是真的——编排的分支与顺序约束是本模块最容易
写错的地方（「先清理还是先释放」「失败时该不该交出命名空间」），
这些逻辑必须能在不连 Java、不连 Redis 的情况下被穷举验证。

三条 EP-8 验收条款各有专属用例：

1. zombie 旧 token 被 Java 拒绝后平台能正确记录失败；
2. finalize 清理失败不会提前释放 run 槽位；
3. HNSW 未 ready 不进入指标计算。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import httpx
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.connector import JavaEvalClient
from app.connector.resilience import RateLimiter
from app.db.session import get_session_factory
from app.engine.pipeline import RunPipeline
from app.params.fingerprint import config_fingerprint
from app.params.service import ParamService
from app.results import ResultWriter
from app.runs.models import Run
from app.runs.schemas import RunCreateRequest
from app.runs.service import RunService

BASE = "http://localhost:8092"
OWNER = "worker-1"
USER = 9_000_000_001


def _url(path: str) -> str:
    return f"{BASE}{path}"


def _envelope(code: str = "0000", data: object = None, info: str = "成功") -> dict:
    return {"code": code, "info": info, "data": data}


@pytest.fixture
def db() -> Iterator[Session]:
    try:
        session = get_session_factory()()
        session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — 探活失败即跳过
        pytest.skip(f"Postgres 不可用，跳过编排集成测试: {exc}")
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def run_id(db: Session) -> Iterator[uuid.UUID]:
    """建一个带两个 query case 的数据集版本 + run。

    **必须显式清理**：编排测试会 ``commit``（模拟真实任务的事务边界），
    ``db`` fixture 的 rollback 兜不住。不清理的话开发库会随每次跑测无限膨胀，
    而且残留的 succeeded run 会让别处「全库没有终态 run」这类断言变脆——
    这个坑刚踩过一次。
    """
    suffix = uuid.uuid4().hex[:8]
    dataset_id = db.execute(
        text("INSERT INTO eval_datasets(name) VALUES (:n) RETURNING id"), {"n": f"pipe-{suffix}"}
    ).scalar_one()
    version_id = db.execute(
        text(
            "INSERT INTO eval_dataset_versions(dataset_id, version, schema_version, source, content_digest) "
            "VALUES (:d, 'v1', 1, 'manual', :digest) RETURNING id"
        ),
        {"d": dataset_id, "digest": f"sha256:{uuid.uuid4().hex}"},
    ).scalar_one()

    # 两条 query case：ground truth 用内容标注（可复现路径）
    for index, query in enumerate(("用户用什么技术栈？", "用户喜欢喝什么？")):
        db.execute(
            text(
                "INSERT INTO eval_cases(dataset_version_id, case_type, group_key, content_hash, payload, ground_truth) "
                "VALUES (:v, 'query_to_memory', 'g', :h, CAST(:p AS jsonb), CAST(:g AS jsonb))"
            ),
            {
                "v": version_id,
                "h": f"sha256:{uuid.uuid4().hex}",
                "p": f'{{"query_id": "q{index}", "query": "{query}"}}',
                "g": '{"relevant_memory_contents": ["用户用 Java 17", "用户偏好美式咖啡"]}',
            },
        )

    snapshot, _ = ParamService(db).get_or_create_param_snapshot(
        name=f"pipe-snap-{suffix}",
        params={
            "vector_store": f"pipe-{suffix}",
            "rrf_k": 60,
            "alpha": 0.5,
            "beta": 0.3,
            "recency_half_life_days": 14.0,
            "profile_boost": 0.2,
            "min_confidence": 0.4,
            "inject_max_tokens": 2000,
        },
    )
    model, _ = ParamService(db).get_or_create_model_version(
        embedding_model_id=f"pipe-emb-{suffix}", reranker_model_id=f"pipe-rr-{suffix}"
    )
    run, _ = RunService(db).create_run(
        RunCreateRequest(
            dataset_version_id=version_id,
            param_snapshot_id=snapshot.id,
            model_version_id=model.id,
            mode="exact",
        ),
        created_by=None,
    )
    db.commit()

    yield run.id

    # 按外键依赖倒序清理：run 引用版本/快照/模型版本，版本引用数据集。
    #
    # **结果表必须最先删**：eval_run_results / eval_case_results 分别以
    # ON DELETE RESTRICT 引用 eval_runs / eval_cases——这是刻意的（有结果的跑批
    # 不该被误删，审计要求）。所以清理方要先显式删结果，再删 run 与 case；
    # 漏掉这一步的报错是 ForeignKeyViolation，信息里只说「仍被引用」，
    # 不清理顺序的人很难第一时间想到是结果表。
    db.rollback()
    db.execute(text("DELETE FROM eval_run_results WHERE run_id IN "
                    "(SELECT id FROM eval_runs WHERE dataset_version_id = :v)"), {"v": version_id})
    db.execute(text("DELETE FROM eval_case_results WHERE run_id IN "
                    "(SELECT id FROM eval_runs WHERE dataset_version_id = :v)"), {"v": version_id})
    db.execute(text("DELETE FROM eval_runs WHERE dataset_version_id = :v"), {"v": version_id})
    db.execute(text("DELETE FROM eval_cases WHERE dataset_version_id = :v"), {"v": version_id})
    db.execute(text("DELETE FROM eval_dataset_versions WHERE id = :v"), {"v": version_id})
    db.execute(text("DELETE FROM eval_datasets WHERE id = :d"), {"d": dataset_id})
    db.execute(text("DELETE FROM eval_param_snapshots WHERE id = :s"), {"s": snapshot.id})
    db.execute(text("DELETE FROM eval_model_versions WHERE id = :m"), {"m": model.id})
    db.commit()


class _JavaStub:
    """注册全部 Java 端点并记录调用次数，供断言「某阶段是否真的没被执行」。"""

    def __init__(self) -> None:
        self.fencing_version = 0
        self.acquire_acquired = True
        self.vector_pending = 0
        self.search_items: list[dict] = [
            {"id": 1, "content": "用户用 Java 17", "score": 0.9},
            {"id": 2, "content": "用户偏好美式咖啡", "score": 0.8},
        ]
        self.finalize_reset_fails = False
        self.calls: dict[str, int] = {}
        self.seed_requests: list[dict] = []
        self.retrieve_context_requests: list[dict] = []
        self.governance_replay_requests: list[dict] = []
        #: 注入维度用的返回值：默认把 seed 出来的两条都注入、token 数远低于预算。
        self.budgeted_ids: list[int] = [1, 2]
        self.inject_token_count = 100
        #: ``{task: [决策...]}``，按 replay 请求里唯一为真的开关取。
        self.replay_by_task: dict[str, list[dict]] = {}
        self.replay_tasks: list[str | None] = []
        self.extract_requests: list[dict] = []
        #: ``extract`` 返回的候选（每条 case 相同），字段名用 connector 的 camelCase 别名。
        self.extract_candidates: list[dict] = []

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def install(self) -> None:
        respx.get(url__regex=rf"{BASE}/api/v1/eval/fencing/\d+").mock(
            side_effect=self._get_fencing
        )
        respx.post(_url("/api/v1/eval/fencing/acquire")).mock(side_effect=self._acquire)
        respx.post(_url("/api/v1/eval/fencing/release")).mock(side_effect=self._release)
        respx.post(_url("/api/v1/eval/reset")).mock(side_effect=self._reset)
        respx.post(_url("/api/v1/eval/seed")).mock(side_effect=self._seed)
        respx.get(_url("/api/v1/eval/metrics")).mock(side_effect=self._metrics)
        respx.post(_url("/api/v1/eval/search")).mock(side_effect=self._search)
        respx.post(_url("/api/v1/eval/retrieve-context")).mock(side_effect=self._retrieve_context)
        respx.post(_url("/api/v1/eval/governance/replay")).mock(
            side_effect=self._governance_replay
        )
        respx.post(_url("/api/v1/eval/extract")).mock(side_effect=self._extract)

    # -- 各端点 ----------------------------------------------------------

    def _get_fencing(self, request: httpx.Request) -> httpx.Response:
        self._count("get_fencing")
        return httpx.Response(
            200,
            json=_envelope(data={"fencingVersion": self.fencing_version, "activeRunId": None}),
        )

    def _acquire(self, request: httpx.Request) -> httpx.Response:
        self._count("acquire")
        if self.acquire_acquired:
            self.fencing_version += 1
            return httpx.Response(
                200, json=_envelope(data={"acquired": True, "version": self.fencing_version})
            )
        return httpx.Response(
            200, json=_envelope(data={"acquired": False, "version": self.fencing_version + 5})
        )

    def _release(self, request: httpx.Request) -> httpx.Response:
        self._count("release")
        return httpx.Response(200, json=_envelope(data=True))

    def _reset(self, request: httpx.Request) -> httpx.Response:
        self._count("reset")
        # finalize 阶段的那次 reset 由测试指定失败（第一次是开头的清理，第二次是收尾）
        if self.finalize_reset_fails and self.calls["reset"] >= 2:
            return httpx.Response(200, json=_envelope("E0403", None, "评测请求被拒绝"))
        return httpx.Response(200, json=_envelope(data={"mysqlDeleted": 0, "vectorCleared": True}))

    def _seed(self, request: httpx.Request) -> httpx.Response:
        self._count("seed")
        # 记下请求体：seed 的**内容**（几条、什么类型、含不含干扰项）只能从这里看，
        # 响应里的 inserted/existed 计数说明不了语料选对没有。
        body = json.loads(request.content)
        self.seed_requests.append(body)
        # contentToId 按请求里的顺序给 id（1 起）。真实 Java 侧由数据库自增产生，
        # 这里造一份等价映射，让注入维度有 id 可还原——返回空映射的话
        # 所有 budgeted_id 都解析不到，注入维度会以「什么都没注入」的形式静默通过。
        content_to_id = {item["content"]: index + 1 for index, item in enumerate(body["items"])}
        return httpx.Response(
            200,
            json=_envelope(
                data={
                    "inserted": len(content_to_id),
                    "existed": 0,
                    "contentToId": content_to_id,
                }
            ),
        )

    def _retrieve_context(self, request: httpx.Request) -> httpx.Response:
        self._count("retrieve_context")
        body = json.loads(request.content)
        self.retrieve_context_requests.append(body)
        return httpx.Response(
            200,
            json=_envelope(
                data={
                    "formatted": "...",
                    "tokenCount": self.inject_token_count,
                    "budgetedIds": self.budgeted_ids,
                }
            ),
        )

    def _extract(self, request: httpx.Request) -> httpx.Response:
        self._count("extract")
        self.extract_requests.append(json.loads(request.content))
        return httpx.Response(200, json=_envelope(data=self.extract_candidates))

    def _governance_replay(self, request: httpx.Request) -> httpx.Response:
        self._count("governance_replay")
        body = json.loads(request.content)
        self.governance_replay_requests.append(body)
        # replay 的入参是「跑哪几类治理分析」的开关，没有 task 字段。按**唯一为真**
        # 的那个开关判断本次请求是针对哪个 task 的——这也顺带验证了 pipeline
        # 确实是逐 task 调用而不是四个开关全开（全开时这里会是 None）。
        enabled = [name for name, value in body.items() if value is True]
        task = enabled[0] if len(enabled) == 1 else None
        self.replay_tasks.append(task)
        return httpx.Response(200, json=_envelope(data=self.replay_by_task.get(task, [])))

    def _metrics(self, request: httpx.Request) -> httpx.Response:
        self._count("metrics")
        return httpx.Response(
            200,
            json=_envelope(
                data={"extractionRejectRate": 0.0, "vectorSyncPendingCount": self.vector_pending}
            ),
        )

    def _search(self, request: httpx.Request) -> httpx.Response:
        self._count("search")
        return httpx.Response(200, json=_envelope(data={"items": self.search_items}))

    def called(self, name: str) -> int:
        return self.calls.get(name, 0)


@pytest.fixture
def java() -> _JavaStub:
    return _JavaStub()


def _client_factory() -> JavaEvalClient:
    # 限流放宽：编排测试关心的是调用顺序与分支，不该被令牌桶拖慢。
    return JavaEvalClient(base_url=BASE, rate_limiter=RateLimiter(10_000.0, burst=1_000))


def _pipeline(db: Session, monkeypatch: pytest.MonkeyPatch, **kwargs) -> RunPipeline:
    # 把 settings 里的 base_url 固定到 BASE，并把 fencing 相关的 eval_user_id 覆盖掉：
    # 编排用 run.eval_user_id（由 fingerprint 派生），与 stub 的 URL 正则无关。
    monkeypatch.setenv("JAVA_EVAL_BASE_URL", BASE)
    return RunPipeline(db, _client_factory, owner=OWNER, **kwargs)


def _reload(db: Session, run_id: uuid.UUID) -> Run:
    db.expire_all()
    run = db.get(Run, run_id)
    assert run is not None
    return run


def _force_mode(db: Session, run_id: uuid.UUID, mode: str) -> None:
    """用裸 SQL 改 run 的 mode —— 必须紧跟 expire。

    会话工厂配了 ``expire_on_commit=False``，且这段 SQL 绕过了 ORM，
    所以 commit 之后 session 里那个 Run 对象**仍带着旧 mode**。
    不 expire 的话 pipeline 读到的还是 'exact'，会直接跳过向量屏障，
    测试就变成「在测一个没生效的前提」——而且是否通过取决于 identity map
    的历史状态，属于典型的不稳定测试。
    """
    db.execute(text("UPDATE eval_runs SET mode=:m WHERE id=:id"), {"m": mode, "id": str(run_id)})
    db.commit()
    db.expire_all()


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


@respx.mock
def test_happy_path_succeeds_and_computes_metrics(
    db: Session, run_id: uuid.UUID, java: _JavaStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    java.install()

    outcome = _pipeline(db, monkeypatch).execute(run_id)
    db.commit()

    assert outcome.status == "succeeded"
    assert outcome.metrics["retrieval"]["recall_at_k"] == pytest.approx(1.0)
    assert outcome.metrics["retrieval"]["case_count"] == 2.0

    run = _reload(db, run_id)
    assert run.status == "succeeded"
    assert run.lease_owner is None
    # 阶段全部记录在案，便于崩溃后判断跑到哪了
    assert run.checkpoint["completed_stages"] == [
        "fencing",
        "reset",
        "seed",
        "vector_ready",
        "search",
        "injection",
        "governance",
        "extraction",
        "metrics",
        "finalize",
    ]


@respx.mock
def test_fencing_version_is_mirrored_to_postgres(
    db: Session, run_id: uuid.UUID, java: _JavaStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """抢占到的版本要写回 ``eval_runs.fencing_version``，排障时不必反查 Java。"""
    java.install()
    _pipeline(db, monkeypatch).execute(run_id)
    db.commit()

    assert _reload(db, run_id).fencing_version == 1


@respx.mock
def test_case_details_are_persisted(
    db: Session, run_id: uuid.UUID, java: _JavaStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验收条款「每个指标可以追溯到 case detail」。"""
    java.install()
    _pipeline(db, monkeypatch).execute(run_id)
    db.commit()

    details = _reload(db, run_id).checkpoint["case_details"]
    assert len(details) == 2
    assert {detail["query_id"] for detail in details} == {"q0", "q1"}
    # 明细要能说明命中/漏召回
    assert all("matched" in detail and "missing" in detail for detail in details)


# ---------------------------------------------------------------------------
# 结果落库（P1-A1）
#
# 这一组的价值在于：checkpoint 里那份明细是**截断到 200 条**的便捷视图，
# 而趋势查询与逐条追溯必须走 eval_run_results / eval_case_results 两张表。
# 只断言「pipeline 没崩」是不够的——上一版正是只写了 checkpoint，
# 真实运行时表里一行都没有，直到做趋势查询时才发现。
# ---------------------------------------------------------------------------


@pytest.fixture
def limited_run_id(db: Session, run_id: uuid.UUID) -> uuid.UUID:
    """与 ``run_id`` **同数据集同配置**、但 ``case_limit=1`` 的另一个 run。

    能并存这件事本身就是指纹改动的证明：``case_limit`` 参与 ``config_fingerprint`` 后，
    两个 run 落在不同的指纹与评测命名空间上，因此不撞 ``uq_runs_active_cfg``。
    改动之前它们指纹相同，第二个 run 会直接 409。
    """
    row = db.execute(
        text(
            "SELECT dataset_version_id, param_snapshot_id, model_version_id "
            "FROM eval_runs WHERE id = :r"
        ),
        {"r": str(run_id)},
    ).one()
    run, _ = RunService(db).create_run(
        RunCreateRequest(
            dataset_version_id=row[0],
            param_snapshot_id=row[1],
            model_version_id=row[2],
            mode="exact",
            case_limit=1,
        ),
        created_by=None,
    )
    db.commit()
    return run.id


def _run_rows(db: Session, run_id: uuid.UUID, *, dimension: str = "retrieval") -> dict[str, float]:
    """取某 run 某维度的聚合指标。

    **必须按维度过滤**：检索与注入共用同一批 query case，两个维度都会落行，
    不过滤就会把两个维度的指标混进同一个字典（两边都有 `case_count`，
    后者还会覆盖前者）。
    """
    rows = db.execute(
        text(
            "SELECT metric_name, metric_value FROM eval_run_results "
            "WHERE run_id = :r AND dimension = :d"
        ),
        {"r": str(run_id), "d": dimension},
    ).all()
    return {name: float(value) for name, value in rows}


@respx.mock
def test_dimension_metrics_are_persisted_to_result_table(
    db: Session, run_id: uuid.UUID, java: _JavaStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    java.install()
    outcome = _pipeline(db, monkeypatch).execute(run_id)
    db.commit()

    assert outcome.status == "succeeded"
    stored = _run_rows(db, run_id)
    # 落库的值必须与内存中算出的完全一致——否则「查库看到的指标」与
    # 「run 对象上的指标」会长期不一致，且没人会发现是哪一边错了。
    assert stored == pytest.approx(outcome.metrics["retrieval"])
    assert stored["recall_at_k"] == pytest.approx(1.0)
    assert stored["case_count"] == pytest.approx(2.0)

    dimensions = db.execute(
        text("SELECT DISTINCT dimension FROM eval_run_results WHERE run_id = :r ORDER BY 1"),
        {"r": str(run_id)},
    ).scalars().all()
    # 检索与注入共用同一批 query case，所以两个维度都会有结果；
    # 治理维度没有 governance case，因此不出现（而不是出现一个空维度）。
    assert dimensions == ["injection", "retrieval"]


@respx.mock
def test_case_results_are_persisted_per_case(
    db: Session, run_id: uuid.UUID, java: _JavaStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    java.install()
    _pipeline(db, monkeypatch).execute(run_id)
    db.commit()

    rows = db.execute(
        text(
            "SELECT metric_values, detail FROM eval_case_results "
            "WHERE run_id = :r AND dimension = 'retrieval' ORDER BY case_id"
        ),
        {"r": str(run_id)},
    ).all()
    assert len(rows) == 2, "每个 case 一行"

    for metric_values, detail in rows:
        # 数值指标落 metric_values（可聚合、可过滤）……
        assert set(metric_values) == {
            "recall_at_k",
            "precision_at_k",
            "hit_at_1",
            "reciprocal_rank",
            "ndcg_at_k",
        }
        # ……解释性上下文落 detail（标识符列表、匹配口径、可评测性）
        assert detail["match_mode"] == "content_hash"
        assert detail["answerable"] is True
        assert "matched" in detail and "missing" in detail

    assert {detail["query_id"] for _, detail in rows} == {"q0", "q1"}


def test_metric_write_is_idempotent(db: Session, run_id: uuid.UUID) -> None:
    """重复写入同一批结果不产生重复行。

    执行编排的断点续跑会重放已完成阶段，而重放必须是幂等的——否则一个
    本该无害的重放会直接撞唯一键，把 run 判成失败。
    """
    writer = ResultWriter(db)
    metrics = {"recall_at_k": 0.5, "case_count": 2.0}

    writer.write_dimension_metrics(run_id, "retrieval", metrics)
    db.commit()
    writer.write_dimension_metrics(run_id, "retrieval", {"recall_at_k": 0.75, "case_count": 2.0})
    db.commit()

    stored = _run_rows(db, run_id)
    assert len(stored) == 2, "唯一键 (run_id, dimension, metric_name) 应把第二次写入合并掉"
    assert stored["recall_at_k"] == pytest.approx(0.75), "后写应当覆盖先写"


@respx.mock
def test_resume_reads_metrics_back_from_database(
    db: Session, run_id: uuid.UUID, java: _JavaStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """断点续跑时，metrics 阶段从库里读回指标而不是返回空字典。

    返回空字典的后果很隐蔽：finalize 会把 ``result_summary`` 写成
    ``{"retrieval": {}}``，于是一个**成功续跑**的 run 在 API 上看起来像
    「什么都没算出来」，而 checkpoint 又显示所有阶段都完成了。
    """
    java.install()
    first = _pipeline(db, monkeypatch).execute(run_id)
    db.commit()
    assert first.status == "succeeded"

    # 模拟「worker 在 finalize 后崩溃、run 被放回队列」：status 回到 pending、
    # 租约清空，但 checkpoint 保留已完成的阶段（这正是续跑的依据）。
    db.execute(
        text("UPDATE eval_runs SET status='pending', lease_owner=NULL, heartbeat_at=NULL "
             "WHERE id=:r"),
        {"r": str(run_id)},
    )
    db.commit()
    db.expire_all()

    second = _pipeline(db, monkeypatch).execute(run_id)
    db.commit()

    assert second.status == "succeeded"
    # search 被跳过（已完成的阶段），所以 collected 是空的——指标只可能来自库。
    assert second.metrics["retrieval"]["recall_at_k"] == pytest.approx(
        first.metrics["retrieval"]["recall_at_k"]
    )
    assert second.metrics["retrieval"]["case_count"] == pytest.approx(2.0)

    run = _reload(db, run_id)
    assert run.result_summary["retrieval"]["recall_at_k"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# case_limit（P1-A3）
# ---------------------------------------------------------------------------


@respx.mock
def test_case_limit_truncates_before_searching(
    db: Session,
    limited_run_id: uuid.UUID,
    java: _JavaStub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """限量必须发生在**检索之前**，而不是算完再丢。

    数据集有 2 条 case、限量 1 条：检索调用次数必须是 1 而不是 2。
    只断言「指标基于 1 条算」是不够的——先跑 1000 条再取前 10 条，
    指标同样正确，但省不下任何时间，而省时间正是 case_limit 存在的理由
    （抽取维度每条 case 要调一次 LLM）。
    """
    java.install()

    outcome = _pipeline(db, monkeypatch).execute(limited_run_id)
    db.commit()

    assert outcome.status == "succeeded"
    assert java.calls["search"] == 1, "应只检索 1 条 case"
    assert outcome.metrics["retrieval"]["case_count"] == 1.0

    # 指标同样只落了 1 条 case 的明细——结果表不能留下被截断掉的 case。
    # 按维度统计：检索与注入各 1 行，两个维度都不能多。
    stored = db.execute(
        text(
            "SELECT dimension, count(*) FROM eval_case_results WHERE run_id = :r "
            "GROUP BY dimension ORDER BY dimension"
        ),
        {"r": str(limited_run_id)},
    ).all()
    assert stored == [("injection", 1), ("retrieval", 1)]


@respx.mock
def test_case_limit_records_selected_and_available(
    db: Session,
    limited_run_id: uuid.UUID,
    java: _JavaStub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """checkpoint 要能看出「数据集里有几条、这次取了几条」。

    看不出来的后果很具体：``case_limit=10`` 而数据集只有 3 条时，指标是基于 3 条算的，
    趋势图上会出现无法解释的跳变，而没有任何地方能指出原因。

    记录**按 case_type 分组**：一个 run 里 query case 与 conversation case 各自限量，
    扁平键会被后一次调用覆盖，看的人会以为那个数字是对整体的统计。
    """
    java.install()
    _pipeline(db, monkeypatch).execute(limited_run_id)
    db.commit()

    selection = _reload(db, limited_run_id).checkpoint["case_selection"]
    assert selection["query_to_memory"] == {"available": 2, "selected": 1}


def test_case_limit_separates_fingerprint_and_namespace(
    db: Session, run_id: uuid.UUID, limited_run_id: uuid.UUID
) -> None:
    """限量与不限量是两个不同的配置指纹、两个不同的评测命名空间。

    两个后果都要成立：
    1. 可比性——10 条与 100 条算的是不同总体的指标，不能并成一条趋势曲线；
    2. 并发——``uq_runs_active_cfg`` 按指纹排他，不含 case_limit 时
       一个冒烟 run 会把同配置的正式跑批挡在门外（而「先小样本试水、再跑全量」
       恰恰是最常见的操作顺序）。
    """
    rows = db.execute(
        text("SELECT id, config_fingerprint, eval_user_id, case_limit FROM eval_runs "
             "WHERE id IN (:a, :b)"),
        {"a": str(run_id), "b": str(limited_run_id)},
    ).all()
    by_limit = {limit: (fp, uid) for _, fp, uid, limit in rows}

    assert set(by_limit) == {None, 1}
    assert by_limit[None][0] != by_limit[1][0], "指纹必须不同"
    assert by_limit[None][1] != by_limit[1][1], "评测命名空间必须不同"


def test_case_limit_must_be_positive() -> None:
    """0 或负数不是「不限量」而是调用方算错了，必须在指纹层就拒绝。

    放行的后果：一个实际跑不到任何 case 的 run 会占住一个看似正常的并发槽位
    （它的指纹是凭空多出来的一个分组），排查时极难想到是 case_limit 写成了 0。
    """
    components = {
        "params_digest": "p",
        "model_digest": "m",
        "dataset_digest": "d",
        "mode": "exact",
    }
    for bad in (0, -1):
        with pytest.raises(ValueError, match="case_limit"):
            config_fingerprint(**components, case_limit=bad)


# ---------------------------------------------------------------------------
# 版本级语料（P1.5）
# ---------------------------------------------------------------------------


@respx.mock
def test_seed_uses_declared_corpus_including_distractors(
    db: Session, java: _JavaStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """版本声明了 ``config.corpus`` 时，seed 必须灌它——**包括干扰项**。

    这是「指标会不会虚高」的分水岭。从各 case 的 ground truth 汇总语料时，
    只有被某条 query 引用过的记忆会被灌进去，检索时几乎没有干扰项，
    随便返回什么都更容易命中，Recall 系统性偏高——而指标看起来完全正常。
    """
    suffix = uuid.uuid4().hex[:8]
    dataset_id = db.execute(
        text("INSERT INTO eval_datasets(name) VALUES (:n) RETURNING id"),
        {"n": f"corpus-{suffix}"},
    ).scalar_one()
    version_id = db.execute(
        text(
            "INSERT INTO eval_dataset_versions(dataset_id, version, schema_version, source, "
            "content_digest, config) "
            "VALUES (:d, 'v1', 1, 'cli', :digest, CAST(:c AS jsonb)) RETURNING id"
        ),
        {
            "d": dataset_id,
            "digest": f"sha256:{uuid.uuid4().hex}",
            # 两条相关 + 一条干扰项（不被任何 query 引用）
            "c": json.dumps(
                {
                    "corpus": [
                        {"content": "用户用 Java 17", "type": "fact"},
                        {"content": "用户偏好美式咖啡", "type": "preference"},
                        {"content": "用户养了一只叫豆豆的猫", "type": "fact"},
                    ]
                },
                ensure_ascii=False,
            ),
        },
    ).scalar_one()
    db.execute(
        text(
            "INSERT INTO eval_cases(dataset_version_id, case_type, group_key, content_hash, "
            "payload, ground_truth) "
            "VALUES (:v, 'query_to_memory', 'g', :h, CAST(:p AS jsonb), CAST(:g AS jsonb))"
        ),
        {
            "v": version_id,
            "h": f"sha256:{uuid.uuid4().hex}",
            "p": '{"query_id": "q0", "query": "用户用什么技术栈？"}',
            "g": '{"relevant_memory_contents": ["用户用 Java 17"]}',
        },
    )
    snapshot, _ = ParamService(db).get_or_create_param_snapshot(
        name=f"corpus-snap-{suffix}",
        params={
            "vector_store": f"corpus-{suffix}",
            "rrf_k": 60,
            "alpha": 0.5,
            "beta": 0.3,
            "recency_half_life_days": 14.0,
            "profile_boost": 0.2,
            "min_confidence": 0.4,
            "inject_max_tokens": 2000,
        },
    )
    model, _ = ParamService(db).get_or_create_model_version(
        embedding_model_id=f"corpus-emb-{suffix}", reranker_model_id=f"corpus-rr-{suffix}"
    )
    run, _ = RunService(db).create_run(
        RunCreateRequest(
            dataset_version_id=version_id,
            param_snapshot_id=snapshot.id,
            model_version_id=model.id,
            mode="exact",
        ),
        created_by=None,
    )
    db.commit()

    java.install()
    outcome = _pipeline(db, monkeypatch).execute(run.id)
    db.commit()

    assert outcome.status == "succeeded"
    items = java.seed_requests[-1]["items"]
    assert len(items) == 3, "干扰项也必须被灌进去"

    by_content = {item["content"]: item["type"] for item in items}
    assert by_content["用户用 Java 17"] == "fact"
    # 类型保真：走 ground-truth 回退路径时这里会退化成 "fact"（内容里没有类型信息）。
    assert by_content["用户偏好美式咖啡"] == "preference"

    db.execute(text("DELETE FROM eval_run_results WHERE run_id = :r"), {"r": str(run.id)})
    db.execute(text("DELETE FROM eval_case_results WHERE run_id = :r"), {"r": str(run.id)})
    db.execute(text("DELETE FROM eval_runs WHERE id = :r"), {"r": str(run.id)})
    db.execute(text("DELETE FROM eval_cases WHERE dataset_version_id = :v"), {"v": version_id})
    db.execute(text("DELETE FROM eval_dataset_versions WHERE id = :v"), {"v": version_id})
    db.execute(text("DELETE FROM eval_datasets WHERE id = :d"), {"d": dataset_id})
    db.execute(text("DELETE FROM eval_param_snapshots WHERE id = :s"), {"s": snapshot.id})
    db.execute(text("DELETE FROM eval_model_versions WHERE id = :m"), {"m": model.id})
    db.commit()


@respx.mock
def test_injection_dimension_is_evaluated_and_persisted(
    db: Session, run_id: uuid.UUID, java: _JavaStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """注入维度接线端到端：调一次 retrieve_context、还原 id、落库。

    seed 出来的两条（stub 给它们 id 1、2）都被注入，且都在 ground truth 里：
    无关注入率应为 0，token 利用率 = 100 / 2000 = 0.05。
    这几个数字把「id → 内容」的还原链路也一起钉住了——还原失败时注入列表为空，
    无关注入率会算成 0，用例会在 `injected_total` 上暴露。
    """
    java.install()
    outcome = _pipeline(db, monkeypatch).execute(run_id)
    db.commit()

    assert outcome.status == "succeeded"
    assert java.calls["retrieve_context"] == 2, "每个 query case 一次"

    injection = outcome.metrics["injection"]
    assert injection["case_count_total"] == 2.0
    assert injection["case_count_scored"] == 2.0
    assert injection["irrelevant_injection_rate"] == pytest.approx(0.0)
    assert injection["over_budget_rate"] == pytest.approx(0.0)
    assert injection["token_utilization"] == pytest.approx(0.05)

    detail = db.execute(
        text(
            "SELECT detail FROM eval_case_results "
            "WHERE run_id = :r AND dimension = 'injection' LIMIT 1"
        ),
        {"r": str(run_id)},
    ).scalar_one()
    assert detail["injected_total"] == 2, "id 必须被还原成内容，否则这里会是 0"
    assert detail["irrelevant_total"] == 0
    assert detail["over_budget"] is False
    # 注入预算取自**参数快照**而不是 Java 当前配置，并记进明细供解释
    assert detail["inject_max_tokens"] == 2000


@respx.mock
def test_governance_replays_once_per_task_and_is_not_case_limited(
    db: Session, java: _JavaStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """治理维度接线：逐 task 调 replay、决策按 task 归属、且**不受 case_limit 约束**。

    两件事一起验，因为它们共同决定治理指标可不可信：

    1. 逐 task 调用（而不是四个开关全开调一次）——全开时返回的是混合列表，
       无法判断某条决策来自重复检测还是过期检测，指标算错了也没法解释；
    2. 不限量——治理算的是比例类指标，限量取到的子集可能恰好全是正样本，
       误伤率就永远是 0，而看的人不知道这是抽样造成的。
    """
    suffix = uuid.uuid4().hex[:8]
    dataset_id = db.execute(
        text("INSERT INTO eval_datasets(name) VALUES (:n) RETURNING id"),
        {"n": f"gov-{suffix}"},
    ).scalar_one()
    version_id = db.execute(
        text(
            "INSERT INTO eval_dataset_versions(dataset_id, version, schema_version, source, "
            "content_digest, config) "
            "VALUES (:d, 'v1', 1, 'cli', :digest, CAST(:c AS jsonb)) RETURNING id"
        ),
        {
            "d": dataset_id,
            "digest": f"sha256:{uuid.uuid4().hex}",
            "c": json.dumps(
                {
                    "corpus": [
                        {"content": "用户用 Java 17", "type": "fact"},
                        {"content": "用户偏好美式咖啡", "type": "preference"},
                    ]
                },
                ensure_ascii=False,
            ),
        },
    ).scalar_one()

    def _add_case(case_type: str, group: str, payload: dict, ground_truth: dict) -> None:
        db.execute(
            text(
                "INSERT INTO eval_cases(dataset_version_id, case_type, group_key, content_hash, "
                "payload, ground_truth) VALUES "
                "(:v, :t, :g, :h, CAST(:p AS jsonb), CAST(:g2 AS jsonb))"
            ),
            {
                "v": version_id,
                "t": case_type,
                "g": group,
                "h": f"sha256:{uuid.uuid4().hex}",
                "p": json.dumps(payload, ensure_ascii=False),
                "g2": json.dumps(ground_truth, ensure_ascii=False),
            },
        )

    # 两条 query case（给 case_limit 一点东西可限）
    for index in range(2):
        _add_case(
            "query_to_memory",
            "g",
            {"query_id": f"q{index}", "query": "用户用什么技术栈？"},
            {"relevant_memory_contents": ["用户用 Java 17"]},
        )
    # duplicates：期望「把 Java 那条合并进咖啡那条」，stub 会返回完全一致的决策 → 无错
    _add_case(
        "governance",
        "dup",
        {"task": "duplicates"},
        {
            "decisions": [
                {
                    "action": "MERGE",
                    "memory_contents": ["用户用 Java 17"],
                    "merged_into_content": "用户偏好美式咖啡",
                }
            ]
        },
    )
    # expired：期望归档，stub 返回空 → 漏判
    _add_case(
        "governance",
        "exp",
        {"task": "expired"},
        {"decisions": [{"action": "ARCHIVE", "memory_contents": ["用户用 Java 17"]}]},
    )

    snapshot, _ = ParamService(db).get_or_create_param_snapshot(
        name=f"gov-snap-{suffix}",
        params={
            "vector_store": f"gov-{suffix}",
            "rrf_k": 60,
            "alpha": 0.5,
            "beta": 0.3,
            "recency_half_life_days": 14.0,
            "profile_boost": 0.2,
            "min_confidence": 0.4,
            "inject_max_tokens": 2000,
        },
    )
    model, _ = ParamService(db).get_or_create_model_version(
        embedding_model_id=f"gov-emb-{suffix}", reranker_model_id=f"gov-rr-{suffix}"
    )
    # case_limit=1：只该限制 query case，治理 case 必须全跑
    run, _ = RunService(db).create_run(
        RunCreateRequest(
            dataset_version_id=version_id,
            param_snapshot_id=snapshot.id,
            model_version_id=model.id,
            mode="exact",
            case_limit=1,
        ),
        created_by=None,
    )
    db.commit()

    # stub：seed 顺序决定 id——id 1 = "用户用 Java 17"、id 2 = "用户偏好美式咖啡"
    java.replay_by_task = {
        "duplicates": [{"action": "MERGE", "mergedIntoId": 2, "items": [{"memoryId": 1}]}],
        "expired": [],
    }
    java.install()
    outcome = _pipeline(db, monkeypatch).execute(run.id)
    db.commit()

    assert outcome.status == "succeeded"

    # 1) 逐 task 调用，且每次只开对应开关（replay_tasks 里出现 None 就说明全开了）
    assert sorted(java.replay_tasks) == ["duplicates", "expired"]
    assert java.calls["governance_replay"] == 2

    # 2) 不限量：case_limit=1 只砍掉 query case，治理两条都评了
    assert outcome.metrics["retrieval"]["case_count"] == 1.0
    assert outcome.metrics["governance"]["case_count"] == 2.0

    # 3) 决策按 task 正确归属。用**按 task 分开的**比率来断言比用总比率更能验归属：
    #    duplicates 完全匹配 → 误合并率 0；expired 漏判 → 误归档率 1。
    #    若归属错了（比如把 expired 的决策算到 duplicates 上），这两个数字会互换位置。
    governance = outcome.metrics["governance"]
    assert governance["duplicates_count"] == 1.0
    assert governance["expired_count"] == 1.0
    assert governance["wrong_merge_rate"] == pytest.approx(0.0)
    assert governance["wrong_archive_rate"] == pytest.approx(1.0)
    assert governance["missed_action_rate"] == pytest.approx(0.5)
    assert governance["false_action_rate"] == pytest.approx(0.0)

    db.execute(text("DELETE FROM eval_run_results WHERE run_id = :r"), {"r": str(run.id)})
    db.execute(text("DELETE FROM eval_case_results WHERE run_id = :r"), {"r": str(run.id)})
    db.execute(text("DELETE FROM eval_runs WHERE id = :r"), {"r": str(run.id)})
    db.execute(text("DELETE FROM eval_cases WHERE dataset_version_id = :v"), {"v": version_id})
    db.execute(text("DELETE FROM eval_dataset_versions WHERE id = :v"), {"v": version_id})
    db.execute(text("DELETE FROM eval_datasets WHERE id = :d"), {"d": dataset_id})
    db.execute(text("DELETE FROM eval_param_snapshots WHERE id = :s"), {"s": snapshot.id})
    db.execute(text("DELETE FROM eval_model_versions WHERE id = :m"), {"m": model.id})
    db.commit()

    # 本用例自己造了数据集/版本/run（没有走 run_id 那套带清理的夹具），
    # 必须自己收尾。顺序仍是「先结果、再 run、最后配置」——结果表是 RESTRICT 外键。
    db.execute(text("DELETE FROM eval_run_results WHERE run_id = :r"), {"r": str(run.id)})
    db.execute(text("DELETE FROM eval_case_results WHERE run_id = :r"), {"r": str(run.id)})
    db.execute(text("DELETE FROM eval_runs WHERE id = :r"), {"r": str(run.id)})
    db.execute(text("DELETE FROM eval_cases WHERE dataset_version_id = :v"), {"v": version_id})
    db.execute(text("DELETE FROM eval_dataset_versions WHERE id = :v"), {"v": version_id})
    db.execute(text("DELETE FROM eval_datasets WHERE id = :d"), {"d": dataset_id})
    db.execute(text("DELETE FROM eval_param_snapshots WHERE id = :s"), {"s": snapshot.id})
    db.execute(text("DELETE FROM eval_model_versions WHERE id = :m"), {"m": model.id})
    db.commit()


@respx.mock
def test_not_owned_run_is_skipped(
    db: Session, run_id: uuid.UUID, java: _JavaStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已被别人领取的 run 应当安静跳过，而不是报错或硬跑。"""
    java.install()
    LeaseManager = __import__("app.runs.lease", fromlist=["LeaseManager"]).LeaseManager
    LeaseManager(db).claim(run_id, "other-worker")
    db.commit()

    outcome = _pipeline(db, monkeypatch).execute(run_id)

    assert outcome.status == "not_owned"
    assert java.called("reset") == 0


# ---------------------------------------------------------------------------
# 验收条款 1：zombie 被拒 → 记失败
# ---------------------------------------------------------------------------


@respx.mock
def test_zombie_rejected_by_acquire_is_recorded_as_failure(
    db: Session, run_id: uuid.UUID, java: _JavaStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**EP-8 验收条款**：旧 token 被拒后平台能正确记录失败。

    ``acquired=False`` 表示期望版本已过期——另一个 run 接管了命名空间。
    此时必须停手：继续写会把别人的命名空间搞脏。
    """
    java.acquire_acquired = False
    java.install()

    outcome = _pipeline(db, monkeypatch).execute(run_id)
    db.commit()

    assert outcome.status == "failed"
    assert "已过期" in (outcome.error or "")

    run = _reload(db, run_id)
    assert run.status == "failed"
    assert "zombie" in (run.error_message or "").lower()
    # 没抢到就不该继续做任何破坏性操作
    assert java.called("reset") == 0
    assert java.called("seed") == 0
    # 也不该去释放别人的 fencing
    assert java.called("release") == 0


@respx.mock
def test_zombie_rejected_by_java_mid_pipeline_is_recorded(
    db: Session, run_id: uuid.UUID, java: _JavaStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """抢占成功、但后续阶段被 Java 以 E0403 拒绝——同样要记为失败。"""
    java.install()

    def _reset_rejects(request: httpx.Request) -> httpx.Response:
        java._count("reset")
        return httpx.Response(200, json=_envelope("E0403", None, "评测请求被拒绝"))

    respx.post(_url("/api/v1/eval/reset")).mock(side_effect=_reset_rejects)

    outcome = _pipeline(db, monkeypatch).execute(run_id)
    db.commit()

    assert outcome.status == "failed"
    assert _reload(db, run_id).status == "failed"


# ---------------------------------------------------------------------------
# 验收条款 2：finalize 清理失败不提前释放槽位
# ---------------------------------------------------------------------------


@respx.mock
def test_finalize_cleanup_failure_does_not_release_fencing(
    db: Session, run_id: uuid.UUID, java: _JavaStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**EP-8 验收条款**：finalize 清理失败不提前释放 run 槽位。

    命名空间里可能留着清理不掉的记忆，此刻交出去等于让下一次评测读串味。
    实现上的硬约束是顺序：清理 → 释放 fencing → CAS 置成功；
    清理失败就抛错，后面的两步根本不会执行。
    """
    java.finalize_reset_fails = True
    java.install()

    outcome = _pipeline(db, monkeypatch).execute(run_id)
    db.commit()

    assert outcome.status == "failed"
    assert "finalize 清理失败" in (outcome.error or "")

    run = _reload(db, run_id)
    # 结论必须是 failed，绝不能是 succeeded——那才是「提前释放」的实质
    assert run.status == "failed"
    # 关键断言：fencing 没有被释放
    assert java.called("release") == 0


# ---------------------------------------------------------------------------
# 验收条款 3：HNSW 未 ready 不进指标计算
# ---------------------------------------------------------------------------


@respx.mock
def test_hnsw_waits_for_vector_sync_barrier(
    db: Session, run_id: uuid.UUID, java: _JavaStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**EP-8 验收条款**：HNSW 未 ready 不进入指标计算。

    seed 后向量是异步同步的，此时立刻检索会漏召回——而漏召回表现为 Recall 偏低，
    看起来像检索算法退步，实际是平台自己抢跑了。
    """
    java.install()
    _force_mode(db, run_id, "hnsw")

    pending_reads = {"n": 0}

    def _metrics(request: httpx.Request) -> httpx.Response:
        java._count("metrics")
        pending_reads["n"] += 1
        # 前两次仍有积压，第三次归零
        pending = 3 if pending_reads["n"] < 3 else 0
        return httpx.Response(
            200,
            json=_envelope(
                data={"extractionRejectRate": 0.0, "vectorSyncPendingCount": pending}
            ),
        )

    respx.get(_url("/api/v1/eval/metrics")).mock(side_effect=_metrics)

    outcome = _pipeline(db, monkeypatch, vector_ready_poll_interval=0.0).execute(run_id)
    db.commit()

    assert outcome.status == "succeeded"
    assert pending_reads["n"] >= 3  # 确实等了


@respx.mock
def test_hnsw_barrier_timeout_fails_before_search(
    db: Session, run_id: uuid.UUID, java: _JavaStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """屏障超时必须在 search **之前**失败——绝不能带着未就绪的向量去算指标。"""
    java.vector_pending = 99
    java.install()
    _force_mode(db, run_id, "hnsw")

    outcome = _pipeline(
        db, monkeypatch, vector_ready_timeout=0.0, vector_ready_poll_interval=0.0
    ).execute(run_id)
    db.commit()

    assert outcome.status == "failed"
    assert "向量同步超时" in (outcome.error or "")
    assert java.called("search") == 0, "未就绪时绝不能进入检索与指标计算"


@respx.mock
def test_exact_mode_skips_vector_barrier(
    db: Session, run_id: uuid.UUID, java: _JavaStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exact 模式直接读 MySQL，不依赖向量索引同步，不该被屏障拖慢。"""
    java.vector_pending = 99  # 即使有积压也不该等
    java.install()

    outcome = _pipeline(db, monkeypatch, vector_ready_timeout=0.0).execute(run_id)
    db.commit()

    assert outcome.status == "succeeded"
    assert java.called("metrics") == 0, "exact 模式不该查询向量积压"


# ---------------------------------------------------------------------------
# 租约
# ---------------------------------------------------------------------------


@respx.mock
def test_lost_lease_aborts_without_writing_terminal_state(
    db: Session, run_id: uuid.UUID, java: _JavaStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失去租约时不能写终态——这个 run 已不归我们管，写任何状态都是越权。"""
    java.install()

    # 让搜索阶段发生时租约已易主
    original_search = java._search

    def _search_and_steal(request: httpx.Request) -> httpx.Response:
        db.execute(
            text(
                "UPDATE eval_runs SET lease_owner='intruder' WHERE id=:id"
            ),
            {"id": str(run_id)},
        )
        db.flush()
        return original_search(request)

    respx.post(_url("/api/v1/eval/search")).mock(side_effect=_search_and_steal)

    outcome = _pipeline(db, monkeypatch).execute(run_id)
    db.commit()

    assert outcome.status == "aborted"
    run = _reload(db, run_id)
    # 状态没有被改成 failed/succeeded，仍在 running（交由 reaper 处理）
    assert run.status == "running"
    assert run.lease_owner == "intruder"


# ---------------------------------------------------------------------------
# 语料收集
# ---------------------------------------------------------------------------


def test_seed_items_include_all_ground_truth_as_distractors(
    db: Session, run_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """语料取**全部** case 的 ground truth，使检索时存在真实干扰项。

    只灌当前 query 的正确答案，检索随便返回什么都容易命中，指标会虚高。
    """
    pipeline = _pipeline(db, monkeypatch)
    run = _reload(db, run_id)

    items = pipeline._collect_seed_items(run)

    assert len(items) == 2
    assert {item.content for item in items} == {"用户用 Java 17", "用户偏好美式咖啡"}


# ---------------------------------------------------------------------------
# 失败阶段标注
# ---------------------------------------------------------------------------


@respx.mock
def test_failure_stage_is_labelled_with_actual_stage(
    db: Session, run_id: uuid.UUID, java: _JavaStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败阶段必须标注正确。

    实跑时发现过：fencing 阶段连不上 Java，``error_message`` 却写着 ``[search]``——
    因为兜底的 ``except JavaEvalError`` 硬编码了 ``stage=Stage.SEARCH``。
    **错误的阶段标注比不标注更糟**：它会把排障的人引到完全错误的阶段去查。
    """
    java.install()
    respx.get(url__regex=rf"{BASE}/api/v1/eval/fencing/\d+").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    outcome = _pipeline(db, monkeypatch).execute(run_id)
    db.commit()

    assert outcome.status == "failed"
    run = _reload(db, run_id)
    # 阶段要写进可查询的字段，而不只是自由文本
    assert run.current_stage == "fencing"
    assert "[fencing]" in (run.error_message or "")
    assert "search" not in (run.error_message or "")


@respx.mock
def test_extraction_dimension_is_evaluated_and_persisted(
    db: Session, java: _JavaStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """抽取维度接线：调一次 extract、算 P/R/F1 与三个比率、落库。

    造的数据刻意是「抽对了一条、多抽了一条低置信度的」：
    这样 precision（0.5 而非 1.0）、低价值写入率（0.5）与 F1 都不是平凡值，
    接线若把参数接反（比如把候选当成 ground truth）会立刻体现出来。
    """
    suffix = uuid.uuid4().hex[:8]
    dataset_id = db.execute(
        text("INSERT INTO eval_datasets(name) VALUES (:n) RETURNING id"),
        {"n": f"ext-{suffix}"},
    ).scalar_one()
    version_id = db.execute(
        text(
            "INSERT INTO eval_dataset_versions(dataset_id, version, schema_version, source, "
            "content_digest) VALUES (:d, 'v1', 1, 'cli', :digest) RETURNING id"
        ),
        {"d": dataset_id, "digest": f"sha256:{uuid.uuid4().hex}"},
    ).scalar_one()
    db.execute(
        text(
            "INSERT INTO eval_cases(dataset_version_id, case_type, group_key, content_hash, "
            "payload, ground_truth) VALUES "
            "(:v, 'conversation_to_memory', 'dialogue', :h, CAST(:p AS jsonb), CAST(:g AS jsonb))"
        ),
        {
            "v": version_id,
            "h": f"sha256:{uuid.uuid4().hex}",
            "p": json.dumps(
                {
                    "dialogue_id": "d0",
                    "messages": [{"role": "user", "content": "我在用 Java 17 写后端"}],
                },
                ensure_ascii=False,
            ),
            "g": json.dumps(
                {
                    "ground_truth_memories": [
                        {"content": "用户用 Java 17", "type": "fact", "attributed_to": "user"}
                    ]
                },
                ensure_ascii=False,
            ),
        },
    )
    snapshot, _ = ParamService(db).get_or_create_param_snapshot(
        name=f"ext-snap-{suffix}",
        params={
            "vector_store": f"ext-{suffix}",
            "rrf_k": 60,
            "alpha": 0.5,
            "beta": 0.3,
            "recency_half_life_days": 14.0,
            "profile_boost": 0.2,
            "min_confidence": 0.4,
            "inject_max_tokens": 2000,
        },
    )
    model, _ = ParamService(db).get_or_create_model_version(
        embedding_model_id=f"ext-emb-{suffix}", reranker_model_id=f"ext-rr-{suffix}"
    )
    run, _ = RunService(db).create_run(
        RunCreateRequest(
            dataset_version_id=version_id,
            param_snapshot_id=snapshot.id,
            model_version_id=model.id,
            mode="exact",
        ),
        created_by=None,
    )
    db.commit()

    java.extract_candidates = [
        # 命中且归属正确、置信度高
        {"content": "用户用 Java 17", "type": "fact", "attributedTo": "user", "confidence": 0.9},
        # 多抽：不在 ground truth 里，且置信度低于默认阈值 0.5
        {"content": "用户喜欢咖啡", "type": "preference", "attributedTo": "user", "confidence": 0.2},
    ]
    java.install()
    outcome = _pipeline(db, monkeypatch).execute(run.id)
    db.commit()

    assert outcome.status == "succeeded"
    assert java.calls["extract"] == 1, "每个对话 case 一次 LLM 调用"

    extraction = outcome.metrics["extraction"]
    assert extraction["case_count_scored"] == 1.0
    assert extraction["precision"] == pytest.approx(0.5), "抽了两条、命中一条"
    assert extraction["recall"] == pytest.approx(1.0)
    assert extraction["f1"] == pytest.approx(2 * 0.5 * 1.0 / 1.5)
    assert extraction["attribution_error_rate"] == pytest.approx(0.0)
    assert extraction["duplicate_extraction_rate"] == pytest.approx(0.0)
    assert extraction["low_value_write_rate"] == pytest.approx(0.5), "0.2 < 阈值 0.5"

    # 阈值要记进明细：不同阈值算出的低价值率不可直接比较，看指标的人必须知道用的是哪个
    detail = db.execute(
        text(
            "SELECT detail FROM eval_run_results "
            "WHERE run_id = :r AND dimension = 'extraction' LIMIT 1"
        ),
        {"r": str(run.id)},
    ).scalar_one()
    assert detail["confidence_threshold"] == pytest.approx(0.5)

    db.execute(text("DELETE FROM eval_run_results WHERE run_id = :r"), {"r": str(run.id)})
    db.execute(text("DELETE FROM eval_case_results WHERE run_id = :r"), {"r": str(run.id)})
    db.execute(text("DELETE FROM eval_runs WHERE id = :r"), {"r": str(run.id)})
    db.execute(text("DELETE FROM eval_cases WHERE dataset_version_id = :v"), {"v": version_id})
    db.execute(text("DELETE FROM eval_dataset_versions WHERE id = :v"), {"v": version_id})
    db.execute(text("DELETE FROM eval_datasets WHERE id = :d"), {"d": dataset_id})
    db.execute(text("DELETE FROM eval_param_snapshots WHERE id = :s"), {"s": snapshot.id})
    db.execute(text("DELETE FROM eval_model_versions WHERE id = :m"), {"m": model.id})
    db.commit()
