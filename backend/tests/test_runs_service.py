"""run 服务层的**真实 Postgres** 集成测试（EP-6 出口条件）。

这一层的并发正确性**只靠数据库约束保证**，所以只在真库上测才有意义：

- ``uq_runs_idempotency`` / ``uq_runs_active_cfg`` / ``uq_runs_active_user``
  三个 partial unique index 是防重复跑批的最终防线，应用层预检只为错误信息友好；
- ``eval_run_guard`` 的 ``SELECT ... FOR UPDATE`` 必须真的串行化并发取锁；
- 行锁 + 同事务写入必须让「检查守卫 → 写 owner → 插 run」之间没有窗口。

不可用时整模块跳过。服务层只 flush 不 commit，用例结束 rollback 清场。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.datasets.models import Dataset, DatasetVersion
from app.db.session import get_session_factory
from app.params.service import ParamService
from app.runs.eval_user import eval_namespace_bounds
from app.runs.models import RunGuard
from app.runs.schemas import ExperimentCreateRequest, RunCreateRequest
from app.runs.service import (
    ExclusiveGuardUnavailableError,
    ExperimentNotFoundError,
    RunConflictError,
    RunNotFoundError,
    RunService,
    RunStateError,
)


@pytest.fixture
def db() -> Iterator[Session]:
    try:
        session = get_session_factory()()
        session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — 探活失败即跳过
        pytest.skip(f"Postgres 不可用，跳过 run 服务集成测试: {exc}")
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def seeded_config(db: Session) -> dict[str, int]:
    """准备一组满足外键的最小配置：数据集版本 + 参数快照 + 模型版本。

    用随机值保证每次都是**新**的配置（哈希不同），从而拿到独立的 fingerprint
    与独立的 eval_user_id —— 否则用例之间会通过 ``uq_runs_active_cfg`` 互相干扰。
    """
    suffix = uuid.uuid4().hex[:8]

    dataset = Dataset(name=f"run-ds-{suffix}")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(
        dataset_id=dataset.id,
        version="v1",
        schema_version=1,
        source="manual",
        content_digest=f"sha256:{uuid.uuid4().hex}",
    )
    db.add(version)
    db.flush()

    params = {
        "vector_store": f"memory-{suffix}",
        "rrf_k": 60,
        "alpha": 0.5,
        "beta": 0.3,
        "recency_half_life_days": 14.0,
        "profile_boost": 0.2,
        "min_confidence": 0.4,
        "inject_max_tokens": 2000,
    }
    snapshot, _ = ParamService(db).get_or_create_param_snapshot(name=f"snap-{suffix}", params=params)
    model, _ = ParamService(db).get_or_create_model_version(
        embedding_model_id=f"emb-{suffix}", reranker_model_id=f"rr-{suffix}"
    )
    db.flush()

    return {
        "dataset_version_id": version.id,
        "param_snapshot_id": snapshot.id,
        "model_version_id": model.id,
    }


def _request(config: dict[str, int], **overrides) -> RunCreateRequest:
    payload = {
        "dataset_version_id": config["dataset_version_id"],
        "param_snapshot_id": config["param_snapshot_id"],
        "model_version_id": config["model_version_id"],
        "mode": "exact",
    }
    payload.update(overrides)
    return RunCreateRequest(**payload)


def _guard_row(db: Session) -> RunGuard:
    row = db.execute(text("SELECT id FROM eval_run_guard WHERE id = 1")).scalar_one_or_none()
    assert row is not None, "eval_run_guard 缺少 id=1 的初始化行——请确认迁移已执行到 head"
    return db.get(RunGuard, 1)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 基本创建
# ---------------------------------------------------------------------------


class TestCreateRun:
    def test_creates_pending_run(self, db: Session, seeded_config: dict[str, int]) -> None:
        run, created = RunService(db).create_run(_request(seeded_config), created_by=None)

        assert created is True
        assert run.status == "pending"
        assert run.mode == "exact"
        assert run.checkpoint["completed_stages"] == []

    def test_eval_user_id_is_derived_and_in_namespace(
        self, db: Session, seeded_config: dict[str, int]
    ) -> None:
        run, _ = RunService(db).create_run(_request(seeded_config), created_by=None)

        lower, upper = eval_namespace_bounds()
        assert lower <= run.eval_user_id < upper

    def test_same_config_always_derives_same_eval_user_id(
        self, db: Session, seeded_config: dict[str, int]
    ) -> None:
        """派生必须稳定：同一配置在任何时刻都落到同一命名空间，否则历史语料对不上。"""
        service = RunService(db)
        first, _ = service.create_run(_request(seeded_config), created_by=None)
        # 把第一个 run 推进到终态，腾出活跃槽位
        first.status = "succeeded"
        db.flush()

        second, _ = service.create_run(_request(seeded_config), created_by=None)
        assert second.eval_user_id == first.eval_user_id
        assert second.config_fingerprint == first.config_fingerprint

    def test_different_config_gets_different_namespace(
        self, db: Session, seeded_config: dict[str, int]
    ) -> None:
        """并行 A/B 的前提：不同配置必须落在不同命名空间，否则会互相 reset 掉语料。"""
        other = dict(seeded_config)
        suffix = uuid.uuid4().hex[:8]
        snapshot, _ = ParamService(db).get_or_create_param_snapshot(
            name=f"snap-{suffix}",
            params={
                "vector_store": f"other-{suffix}",
                "rrf_k": 30,
                "alpha": 0.7,
                "beta": 0.2,
                "recency_half_life_days": 7.0,
                "profile_boost": 0.1,
                "min_confidence": 0.5,
                "inject_max_tokens": 1000,
            },
        )
        other["param_snapshot_id"] = snapshot.id
        db.flush()

        service = RunService(db)
        first, _ = service.create_run(_request(seeded_config), created_by=None)
        second, _ = service.create_run(_request(other), created_by=None)

        assert first.config_fingerprint != second.config_fingerprint
        assert first.eval_user_id != second.eval_user_id

    def test_unknown_experiment_raises(self, db: Session, seeded_config: dict[str, int]) -> None:
        with pytest.raises(ExperimentNotFoundError):
            RunService(db).create_run(
                _request(seeded_config, experiment_id=uuid.uuid4()), created_by=None
            )

    def test_experiment_can_be_attached(self, db: Session, seeded_config: dict[str, int]) -> None:
        service = RunService(db)
        experiment = service.create_experiment(
            ExperimentCreateRequest(name="A/B 融合权重"), created_by=None
        )
        run, _ = service.create_run(
            _request(seeded_config, experiment_id=experiment.id), created_by=None
        )
        assert run.experiment_id == experiment.id


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_same_key_returns_same_run(self, db: Session, seeded_config: dict[str, int]) -> None:
        """**EP-6 核心**：重复提交（网络重试、用户双击）必须返回同一个 run。"""
        service = RunService(db)
        first, created_first = service.create_run(
            _request(seeded_config, idempotency_key="key-1"), created_by=None
        )
        second, created_second = service.create_run(
            _request(seeded_config, idempotency_key="key-1"), created_by=None
        )

        assert created_first is True
        assert created_second is False
        assert first.id == second.id

    def test_different_keys_create_different_runs(
        self, db: Session, seeded_config: dict[str, int]
    ) -> None:
        service = RunService(db)
        first, _ = service.create_run(
            _request(seeded_config, idempotency_key="key-1"), created_by=None
        )
        # 同配置第二个 run 会撞活跃约束，故先推进第一个到终态
        first.status = "succeeded"
        db.flush()

        second, created = service.create_run(
            _request(seeded_config, idempotency_key="key-2"), created_by=None
        )
        assert created is True
        assert second.id != first.id

    def test_null_key_allows_repeated_submission(self, db: Session, seeded_config: dict[str, int]) -> None:
        """幂等键为 NULL 时不参与唯一性——但同配置的活跃约束仍会挡住第二个。

        这里验证的是「NULL 本身不构成冲突」，所以要让两次提交的配置不同。
        """
        suffix = uuid.uuid4().hex[:8]
        snapshot, _ = ParamService(db).get_or_create_param_snapshot(
            name=f"s-{suffix}",
            params={
                "vector_store": f"v-{suffix}",
                "rrf_k": 10,
                "alpha": 0.1,
                "beta": 0.1,
                "recency_half_life_days": 1.0,
                "profile_boost": 0.0,
                "min_confidence": 0.0,
                "inject_max_tokens": 100,
            },
        )
        db.flush()

        service = RunService(db)
        first, _ = service.create_run(_request(seeded_config), created_by=None)
        other = dict(seeded_config, param_snapshot_id=snapshot.id)
        second, created = service.create_run(_request(other), created_by=None)

        assert created is True
        assert first.idempotency_key is None and second.idempotency_key is None
        assert first.id != second.id


# ---------------------------------------------------------------------------
# 活跃约束
# ---------------------------------------------------------------------------


class TestActiveConstraints:
    def test_same_config_cannot_run_concurrently(
        self, db: Session, seeded_config: dict[str, int]
    ) -> None:
        """**EP-6 核心**：同一配置同时只允许一个进行中的 run。"""
        service = RunService(db)
        service.create_run(_request(seeded_config), created_by=None)

        with pytest.raises(RunConflictError) as excinfo:
            service.create_run(_request(seeded_config), created_by=None)

        assert excinfo.value.constraint in ("uq_runs_active_cfg", "uq_runs_active_user")

    def test_terminal_state_frees_the_slot(self, db: Session, seeded_config: dict[str, int]) -> None:
        """partial index 的核心语义：终态 run 不占用槽位，同配置可以重跑。"""
        service = RunService(db)
        first, _ = service.create_run(_request(seeded_config), created_by=None)

        for terminal in ("succeeded", "failed", "cancelled"):
            first.status = terminal
            db.flush()
            run, created = service.create_run(_request(seeded_config), created_by=None)
            assert created is True, f"终态 {terminal} 后应能重新创建"
            first = run

    def test_running_state_still_occupies_slot(self, db: Session, seeded_config: dict[str, int]) -> None:
        service = RunService(db)
        first, _ = service.create_run(_request(seeded_config), created_by=None)
        first.status = "running"
        db.flush()

        with pytest.raises(RunConflictError):
            service.create_run(_request(seeded_config), created_by=None)

    def test_conflict_error_carries_constraint_name(
        self, db: Session, seeded_config: dict[str, int]
    ) -> None:
        """约束名要带出来：排障时需要区分是「同配置」还是「同命名空间」。"""
        service = RunService(db)
        service.create_run(_request(seeded_config), created_by=None)

        with pytest.raises(RunConflictError) as excinfo:
            service.create_run(_request(seeded_config), created_by=None)
        assert excinfo.value.constraint is not None

    def test_conflict_is_translated_not_leaked_as_integrity_error(
        self, db: Session, seeded_config: dict[str, int]
    ) -> None:
        """IntegrityError 必须被翻译成领域错误，且事务要已被 rollback。

        不 rollback 的话 Postgres 会把事务标记为 aborted，后续任何查询都报
        InFailedSqlTransaction——所以冲突之后必须仍能正常查询。
        """
        service = RunService(db)
        service.create_run(_request(seeded_config), created_by=None)
        with pytest.raises(RunConflictError):
            service.create_run(_request(seeded_config), created_by=None)

        # 事务仍可用
        assert service.list_runs(limit=5) is not None


# ---------------------------------------------------------------------------
# 独占守卫
# ---------------------------------------------------------------------------


class TestExclusiveGuard:
    def test_exclusive_run_acquires_guard(self, db: Session, seeded_config: dict[str, int]) -> None:
        run, _ = RunService(db).create_run(_request(seeded_config, exclusive=True), created_by=None)

        guard = _guard_row(db)
        assert guard.exclusive_owner == run.id
        assert guard.acquired_at is not None
        assert guard.heartbeat_at is not None

    def test_second_exclusive_run_is_rejected(
        self, db: Session, seeded_config: dict[str, int]
    ) -> None:
        """**EP-6 核心**：同一时刻全库至多一个独占 run。"""
        service = RunService(db)
        service.create_run(_request(seeded_config, exclusive=True), created_by=None)

        other = dict(seeded_config, param_snapshot_id=seeded_config["param_snapshot_id"])
        suffix = uuid.uuid4().hex[:8]
        snapshot, _ = ParamService(db).get_or_create_param_snapshot(
            name=f"x-{suffix}",
            params={
                "vector_store": f"x-{suffix}",
                "rrf_k": 5,
                "alpha": 0.2,
                "beta": 0.2,
                "recency_half_life_days": 2.0,
                "profile_boost": 0.0,
                "min_confidence": 0.0,
                "inject_max_tokens": 50,
            },
        )
        db.flush()
        other["param_snapshot_id"] = snapshot.id

        with pytest.raises(ExclusiveGuardUnavailableError) as excinfo:
            service.create_run(_request(other, exclusive=True), created_by=None)
        assert excinfo.value.constraint == "exclusive_guard"

    def test_non_exclusive_run_is_not_blocked_by_guard(
        self, db: Session, seeded_config: dict[str, int]
    ) -> None:
        """独占守卫只挡独占 run；普通 run 照常创建（它们由 per-config 命名空间隔离）。"""
        service = RunService(db)
        service.create_run(_request(seeded_config, exclusive=True), created_by=None)

        suffix = uuid.uuid4().hex[:8]
        snapshot, _ = ParamService(db).get_or_create_param_snapshot(
            name=f"n-{suffix}",
            params={
                "vector_store": f"n-{suffix}",
                "rrf_k": 7,
                "alpha": 0.3,
                "beta": 0.3,
                "recency_half_life_days": 3.0,
                "profile_boost": 0.0,
                "min_confidence": 0.0,
                "inject_max_tokens": 70,
            },
        )
        db.flush()

        run, created = service.create_run(
            _request(dict(seeded_config, param_snapshot_id=snapshot.id)), created_by=None
        )
        assert created is True
        assert run.exclusive is False

    def test_stale_guard_is_reclaimed(self, db: Session, seeded_config: dict[str, int]) -> None:
        """持锁 run 心跳过期后应允许接管——否则一个死掉的 run 会永久堵住独占模式。"""
        service = RunService(db)
        service.create_run(_request(seeded_config, exclusive=True), created_by=None)

        # 把心跳推到远早于陈旧阈值
        guard = _guard_row(db)
        guard.heartbeat_at = datetime.now(UTC) - timedelta(hours=2)
        db.flush()

        suffix = uuid.uuid4().hex[:8]
        snapshot, _ = ParamService(db).get_or_create_param_snapshot(
            name=f"r-{suffix}",
            params={
                "vector_store": f"r-{suffix}",
                "rrf_k": 8,
                "alpha": 0.4,
                "beta": 0.4,
                "recency_half_life_days": 4.0,
                "profile_boost": 0.0,
                "min_confidence": 0.0,
                "inject_max_tokens": 80,
            },
        )
        db.flush()

        taken, created = service.create_run(
            _request(dict(seeded_config, param_snapshot_id=snapshot.id), exclusive=True),
            created_by=None,
        )

        assert created is True
        assert _guard_row(db).exclusive_owner == taken.id

    def test_guard_without_heartbeat_is_treated_as_stale(
        self, db: Session, seeded_config: dict[str, int]
    ) -> None:
        """从未报到过的锁无法证明持有者活着，按死锁处理比一直堵着安全。"""
        service = RunService(db)
        service.create_run(_request(seeded_config, exclusive=True), created_by=None)

        guard = _guard_row(db)
        guard.heartbeat_at = None
        db.flush()

        suffix = uuid.uuid4().hex[:8]
        snapshot, _ = ParamService(db).get_or_create_param_snapshot(
            name=f"h-{suffix}",
            params={
                "vector_store": f"h-{suffix}",
                "rrf_k": 9,
                "alpha": 0.6,
                "beta": 0.1,
                "recency_half_life_days": 5.0,
                "profile_boost": 0.0,
                "min_confidence": 0.0,
                "inject_max_tokens": 90,
            },
        )
        db.flush()

        _, created = service.create_run(
            _request(dict(seeded_config, param_snapshot_id=snapshot.id), exclusive=True),
            created_by=None,
        )
        assert created is True

    def test_release_is_cas_on_owner(self, db: Session, seeded_config: dict[str, int]) -> None:
        """只有当前持有者能释放。

        不加 CAS 的话，一个被接管的旧 run 结束时会把**新持有者**的锁清掉，
        让第三个 run 趁虚而入——正是 fencing 要防的脑裂。
        """
        service = RunService(db)
        run, _ = service.create_run(_request(seeded_config, exclusive=True), created_by=None)

        assert service.release_exclusive_guard(uuid.uuid4()) is False, "非持有者不得释放"
        assert _guard_row(db).exclusive_owner == run.id

        assert service.release_exclusive_guard(run.id) is True
        guard = _guard_row(db)
        assert guard.exclusive_owner is None
        assert guard.acquired_at is None

    def test_released_guard_can_be_taken_again(
        self, db: Session, seeded_config: dict[str, int]
    ) -> None:
        service = RunService(db)
        run, _ = service.create_run(_request(seeded_config, exclusive=True), created_by=None)
        service.release_exclusive_guard(run.id)
        run.status = "succeeded"
        db.flush()

        suffix = uuid.uuid4().hex[:8]
        snapshot, _ = ParamService(db).get_or_create_param_snapshot(
            name=f"a-{suffix}",
            params={
                "vector_store": f"a-{suffix}",
                "rrf_k": 11,
                "alpha": 0.8,
                "beta": 0.05,
                "recency_half_life_days": 6.0,
                "profile_boost": 0.0,
                "min_confidence": 0.0,
                "inject_max_tokens": 110,
            },
        )
        db.flush()

        _, created = service.create_run(
            _request(dict(seeded_config, param_snapshot_id=snapshot.id), exclusive=True),
            created_by=None,
        )
        assert created is True


# ---------------------------------------------------------------------------
# 取消与查询
# ---------------------------------------------------------------------------


class TestCancelAndQuery:
    def test_cancel_pending_run(self, db: Session, seeded_config: dict[str, int]) -> None:
        service = RunService(db)
        run, _ = service.create_run(_request(seeded_config), created_by=None)

        cancelled = service.cancel_run(run.id)
        assert cancelled.status == "cancelled"
        assert cancelled.finished_at is not None

    def test_cancel_frees_the_active_slot(self, db: Session, seeded_config: dict[str, int]) -> None:
        service = RunService(db)
        run, _ = service.create_run(_request(seeded_config), created_by=None)
        service.cancel_run(run.id)

        _, created = service.create_run(_request(seeded_config), created_by=None)
        assert created is True

    def test_cannot_cancel_terminal_run(self, db: Session, seeded_config: dict[str, int]) -> None:
        """终态 run 不能再被覆写成 cancelled——那会让已有结果凭空消失。"""
        service = RunService(db)
        run, _ = service.create_run(_request(seeded_config), created_by=None)
        run.status = "succeeded"
        db.flush()

        with pytest.raises(RunStateError):
            service.cancel_run(run.id)

    def test_cancel_unknown_run_raises(self, db: Session) -> None:
        with pytest.raises(RunNotFoundError):
            RunService(db).cancel_run(uuid.uuid4())

    def test_get_unknown_run_raises(self, db: Session) -> None:
        with pytest.raises(RunNotFoundError):
            RunService(db).get_run(uuid.uuid4())

    def test_list_filters_by_status_and_fingerprint(
        self, db: Session, seeded_config: dict[str, int]
    ) -> None:
        """断言一律**限定在本用例自己的 run 上**，不断言「全库没有别的 run」。

        早先这里写的是 `list_runs(status="succeeded") == []`——一条对整库的断言。
        它在单文件跑时成立，但别的测试文件会提交 succeeded 的 run 进来，
        全量跑就红。测试之间共享一个库时，「不存在」类断言天然脆弱。
        """
        service = RunService(db)
        run, _ = service.create_run(_request(seeded_config), created_by=None)

        assert run.id in [r.id for r in service.list_runs(status="pending")]
        assert [r.id for r in service.list_runs(config_fingerprint=run.config_fingerprint)] == [run.id]
        # 刚创建的 run 不该出现在终态列表里
        assert run.id not in [r.id for r in service.list_runs(status="succeeded")]

    def test_state_counts(self, db: Session, seeded_config: dict[str, int]) -> None:
        service = RunService(db)
        service.create_run(_request(seeded_config), created_by=None)
        counts = service.run_state_counts()
        assert counts.get("pending", 0) >= 1

    def test_experiment_crud(self, db: Session) -> None:
        service = RunService(db)
        experiment = service.create_experiment(ExperimentCreateRequest(name="E1"), created_by=None)
        assert service.get_experiment(experiment.id).name == "E1"
        assert experiment.id in [e.id for e in service.list_experiments()]

    def test_unknown_experiment_raises_on_get(self, db: Session) -> None:
        with pytest.raises(ExperimentNotFoundError):
            RunService(db).get_experiment(uuid.uuid4())
