"""评测结果的落库服务（EP-10 的数据基础）。

**为什么必须落库，而不是只留在 ``run.result_summary``**
``result_summary`` 是一个 JSONB blob：它适合「看一眼这次跑了什么」，但不适合查询。
趋势对比（「上周到本周 Recall@5 怎么变的」）必须按 ``(dimension, metric_name)``
跨 run 聚合——在 JSONB 上做这件事要么走 GIN 索引 + 复杂的路径表达式，要么全表扫描。
``eval_run_results`` 天生就是这个形状，索引直接可用。

两层粒度各司其职：

- ``eval_run_results`` —— 维度级聚合（一个 run × 一个维度 × 一个指标名 = 一行）
- ``eval_case_results`` —— 逐 case 明细，``metric_values`` 是「指标名 → 值」的稀疏映射
  （不同 case 类型参与的指标不同，稀疏映射比宽表更诚实）

**写入一律 upsert**：唯一键已在迁移里建好（``uq_run_dimension_metric`` /
``uq_run_case_dimension``），重复写入同一批结果时幂等刷新。这不是为了「重跑方便」，
而是执行编排的断点续跑会重放已完成阶段——没有 upsert 的话，第二次写入直接撞唯一键，
一个本该无害的重放就变成了 run 失败。

**关于不可评测的 case**（``answerable=False``）：
维度②的口径是「把『没有 ground truth』计入分母，等于把数据问题伪装成模型退步」，
所以聚合时排除它们。但落库时**仍然写入**——排除是"不参与均值"，不是"不存在"。
不写的话就再也查不到「这个 run 有多少 case 因标注缺失而无法评测」，
而这恰恰是评测集质量最该被看见的信号。判定依据放在 ``detail.answerable``。
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.results.models import CaseResult, RunResult
from app.runs.models import Run
from app.settings.logging import get_logger

logger = get_logger(__name__)

#: 维度标识。与 ``app.engine.dimensions`` 下的模块一一对应，落库后用于分组查询。
#: 刻意用字符串常量而不是 Enum：新增维度时不必改数据库里的历史值。
DIMENSION_RETRIEVAL = "retrieval"
DIMENSION_INJECTION = "injection"
DIMENSION_GOVERNANCE = "governance"
DIMENSION_EXTRACTION = "extraction"
DIMENSION_CONSISTENCY = "consistency"


def _to_decimal(value: float, *, where: str) -> Decimal:
    """把指标值转成 ``Decimal``，并拒绝非有限值。

    ``Numeric`` 列能存 ``NaN``（PostgreSQL 的 numeric 支持 'NaN'），但一个 NaN 指标
    几乎总是**评测器里的逻辑 bug**（除零、空集合求均值）而不是真实的测量结果。
    让它落库的后果很隐蔽：趋势图上出现断点、按指标排序时 NaN 排在最前、
    比较运算全部返回 false。与其污染数据，不如在写入点就失败——异常会带上指标名，
    排查时能直接定位到是哪个维度的哪个指标算错了。
    """
    if not math.isfinite(value):
        raise ValueError(f"指标值非有限（{value}），疑似评测器逻辑错误: {where}")
    return Decimal(str(value))


class ResultWriter:
    """结果落库入口。

    **不 commit**——事务边界由调用方控制（与其余 service 一致）。
    pipeline 在 metrics 阶段写完后由任务体统一提交。
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    # -- 维度级聚合 -------------------------------------------------------

    def write_dimension_metrics(
        self,
        run_id: uuid.UUID,
        dimension: str,
        metrics: Mapping[str, float],
        *,
        detail: Mapping[str, Any] | None = None,
    ) -> int:
        """写入某维度的聚合指标，返回写入的行数。

        每个 ``(dimension, metric_name)`` 一行。``case_count`` 之类的计数值也一并写入：
        它是该维度的聚合事实（「这个均值由多少条 case 得出」），放进 ``detail``
        反而会逼着查询方去解析 JSONB。
        """
        rows = [
            {
                "run_id": run_id,
                "dimension": dimension,
                "metric_name": name,
                "metric_value": _to_decimal(
                    float(value), where=f"{dimension}.{name} (run={run_id})"
                ),
                "detail": dict(detail or {}),
            }
            for name, value in metrics.items()
        ]
        if not rows:
            return 0

        stmt = pg_insert(RunResult).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[RunResult.run_id, RunResult.dimension, RunResult.metric_name],
            set_={
                "metric_value": stmt.excluded.metric_value,
                "detail": stmt.excluded.detail,
            },
        )
        self._db.execute(stmt)
        self._db.flush()
        logger.debug(
            "写入维度聚合指标: dimension=%s metrics=%d",
            dimension,
            len(rows),
            extra={"run_id": str(run_id)},
        )
        return len(rows)

    # -- 逐 case 明细 -----------------------------------------------------

    def write_case_metrics(
        self,
        run_id: uuid.UUID,
        dimension: str,
        cases: Iterable[tuple[int, Mapping[str, float], Mapping[str, Any]]],
    ) -> int:
        """写入逐 case 明细。

        ``cases`` 的每个元素是 ``(case_id, metric_values, detail)``。
        ``case_id`` 必须是 ``eval_cases.id`` 的真实主键——外键约束会兜住传错的情况，
        但错误信息会很难读，所以调用方应当直接传 ORM 对象的 ``id``。

        返回写入行数。
        """
        rows = []
        for case_id, metric_values, detail in cases:
            rows.append(
                {
                    "run_id": run_id,
                    "case_id": case_id,
                    "dimension": dimension,
                    "metric_values": {
                        name: float(value) for name, value in metric_values.items()
                    },
                    "detail": dict(detail),
                }
            )
        if not rows:
            return 0

        stmt = pg_insert(CaseResult).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[CaseResult.run_id, CaseResult.case_id, CaseResult.dimension],
            set_={
                "metric_values": stmt.excluded.metric_values,
                "detail": stmt.excluded.detail,
            },
        )
        self._db.execute(stmt)
        self._db.flush()
        logger.debug(
            "写入逐 case 指标: dimension=%s cases=%d",
            dimension,
            len(rows),
            extra={"run_id": str(run_id)},
        )
        return len(rows)


class ResultReader:
    """结果读取入口。

    单独成类而不是把读方法挂在 :class:`ResultWriter` 上：读路径会被 API 层
    大量使用（且应当能走只读连接/只读事务），写路径只在 pipeline 里出现一次。
    混在一起会让读侧的调用方莫名获得写能力。
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    def dimension_metrics(self, run_id: uuid.UUID, dimension: str) -> dict[str, float]:
        """取某 run 某维度的聚合指标，``{指标名: 值}``。

        用于断点续跑：metrics 阶段被跳过时（checkpoint 里已标记完成），
        finalize 仍需要拿到指标去写 ``result_summary``。从库里读回来比重新计算更可靠——
        重算需要重新检索，而检索结果没被持久化。
        """
        rows = self._db.execute(
            select(RunResult.metric_name, RunResult.metric_value).where(
                RunResult.run_id == run_id, RunResult.dimension == dimension
            )
        ).all()
        return {name: float(value) for name, value in rows}

    def all_dimension_metrics(self, run_id: uuid.UUID) -> dict[str, dict[str, float]]:
        """取某 run 全部维度的聚合指标，``{维度: {指标名: 值}}``。"""
        rows = self._db.execute(
            select(RunResult.dimension, RunResult.metric_name, RunResult.metric_value)
            .where(RunResult.run_id == run_id)
            .order_by(RunResult.dimension, RunResult.metric_name)
        ).all()
        grouped: dict[str, dict[str, float]] = {}
        for dimension, name, value in rows:
            grouped.setdefault(dimension, {})[name] = float(value)
        return grouped

    def count_case_results(self, run_id: uuid.UUID, *, dimension: str | None = None) -> int:
        """逐 case 明细的总条数（分页时返回给调用方）。

        单独查询而不是「取回全部再 len()」：一个千条规模的评测集不该为了拿个计数
        就把整表拉进内存。
        """
        stmt = select(func.count()).select_from(CaseResult).where(CaseResult.run_id == run_id)
        if dimension is not None:
            stmt = stmt.where(CaseResult.dimension == dimension)
        return int(self._db.execute(stmt).scalar_one())

    def metric_trend(
        self,
        *,
        dimension: str,
        metric_name: str,
        config_fingerprint: str | None = None,
        limit: int = 100,
    ) -> list[tuple[uuid.UUID, datetime, float, str]]:
        """取某指标随时间的走势，返回 ``[(run_id, 创建时间, 值, run 状态)]``，按时间升序。

        **这是结果落库真正的用处**：``run.result_summary`` 是个 JSONB blob，
        在上面做跨 run 聚合要么走 GIN + 复杂路径表达式、要么全表扫描；
        而 ``eval_run_results`` 天生是 ``(run_id, dimension, metric_name, value)`` 的形状，
        索引直接可用。

        ``limit`` 取**最近 N 个**而不是最早 N 个：看趋势的人关心的是近期，
        而升序返回是为了直接喂给图表（时间轴从左到右）。
        实现上先按时间倒序取 N 条再反转——一步 ``ORDER BY DESC LIMIT`` 加一次内存反转，
        比子查询套正序更简单也更省。
        """
        stmt = (
            select(
                RunResult.run_id,
                Run.created_at,
                RunResult.metric_value,
                Run.status,
            )
            .join(Run, Run.id == RunResult.run_id)
            .where(
                RunResult.dimension == dimension,
                RunResult.metric_name == metric_name,
            )
            .order_by(Run.created_at.desc())
            .limit(limit)
        )
        if config_fingerprint is not None:
            # 只在同一配置指纹内比较：不同指纹的指标本就不可比（数据集/参数/模型/mode 任一不同）。
            stmt = stmt.where(Run.config_fingerprint == config_fingerprint)

        rows = self._db.execute(stmt).all()
        return [
            (run_id, created_at, float(value), status)
            for run_id, created_at, value, status in reversed(rows)
        ]

    def case_results(
        self,
        run_id: uuid.UUID,
        *,
        dimension: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[CaseResult]:
        """取某 run 的逐 case 明细，可按维度过滤并分页。

        按 ``case_id`` 排序而不是按插入顺序：明细的用途是「对照评测集逐条看」，
        稳定且与评测集同序的排列比按写入时间排更有用。**分页也依赖这个排序**——
        没有确定性的 ORDER BY，翻页会漏行或重复行。
        """
        stmt = (
            select(CaseResult)
            .where(CaseResult.run_id == run_id)
            .order_by(CaseResult.case_id)
            .offset(offset)
        )
        if dimension is not None:
            stmt = stmt.where(CaseResult.dimension == dimension)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self._db.execute(stmt).scalars())


__all__ = ["DIMENSION_RETRIEVAL", "ResultReader", "ResultWriter"]
