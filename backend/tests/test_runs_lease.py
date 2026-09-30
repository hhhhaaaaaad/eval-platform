"""run 租约状态机的**真实 Postgres** 集成测试（EP-7 核心）。

租约的正确性全在「单条 CAS UPDATE + WHERE 条件」上，脱离数据库就无从验证：
并发领取是否互斥、心跳能否充当取消检查、被回收的 worker 能否写出结果——
这些都取决于 WHERE 子句的原子性。

做法：把 ``heartbeat_at`` 直接改成过去的时刻来模拟「worker 已死」，
无需真的等待租约超时。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.params.service import ParamService
from app.runs.lease import LeaseManager
from app.runs.models import RunGuard
from app.runs.schemas import RunCreateRequest
from app.runs.service import RunService
from app.settings.config import get_settings


@pytest.fixture
def db() -> Iterator[Session]:
    try:
        session = get_session_factory()()
        session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — 探活失败即跳过
        pytest.skip(f"Postgres 不可用，跳过租约集成测试: {exc}")
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def run_id(db: Session) -> uuid.UUID:
    """建出一个 pending run，作为租约操作的载体。"""
    suffix = uuid.uuid4().hex[:8]
    dataset_id = db.execute(
        text("INSERT INTO eval_datasets(name) VALUES (:n) RETURNING id"), {"n": f"lease-{suffix}"}
    ).scalar_one()
    version_id = db.execute(
        text(
            "INSERT INTO eval_dataset_versions(dataset_id, version, schema_version, source, content_digest) "
            "VALUES (:d, 'v1', 1, 'manual', :digest) RETURNING id"
        ),
        {"d": dataset_id, "digest": f"sha256:{uuid.uuid4().hex}"},
    ).scalar_one()
    snapshot, _ = ParamService(db).get_or_create_param_snapshot(
        name=f"lease-snap-{suffix}",
        params={
            "vector_store": f"lease-{suffix}",
            "rrf_k": 1,
            "alpha": 0.1,
            "beta": 0.1,
            "recency_half_life_days": 1.0,
            "profile_boost": 0.0,
            "min_confidence": 0.0,
            "inject_max_tokens": 10,
        },
    )
    model, _ = ParamService(db).get_or_create_model_version(
        embedding_model_id=f"lease-emb-{suffix}", reranker_model_id=f"lease-rr-{suffix}"
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
    return run.id


def _expire_heartbeat(db: Session, run_id: uuid.UUID, *, seconds_ago: int = 7200) -> None:
    """把心跳推到过去，模拟 worker 已死（免去真实等待租约超时）。"""
    db.execute(
        text(
            "UPDATE eval_runs SET heartbeat_at = now() - make_interval(secs => :s) WHERE id = :id"
        ),
        {"s": seconds_ago, "id": str(run_id)},
    )
    db.flush()


def _reload(db: Session, run_id: uuid.UUID):
    from app.runs.models import Run

    db.expire_all()
    return db.get(Run, run_id)


# ---------------------------------------------------------------------------
# 领取
# ---------------------------------------------------------------------------


class TestClaim:
    def test_claims_pending_run(self, db: Session, run_id: uuid.UUID) -> None:
        result = LeaseManager(db).claim(run_id, "worker-1")

        assert result.claimed is True
        assert result.fencing_version == 1

        run = _reload(db, run_id)
        assert run.status == "running"
        assert run.lease_owner == "worker-1"
        assert run.started_at is not None
        assert run.heartbeat_at is not None

    def test_second_claim_is_rejected_while_heartbeat_fresh(
        self, db: Session, run_id: uuid.UUID
    ) -> None:
        """**互斥**：心跳新鲜时别人抢不走。"""
        lease = LeaseManager(db)
        assert lease.claim(run_id, "worker-1").claimed is True
        assert lease.claim(run_id, "worker-2").claimed is False

    def test_fencing_version_increments_on_each_claim(
        self, db: Session, run_id: uuid.UUID
    ) -> None:
        """每领取一次自增，供旧 worker 的写入被拒（防脑裂第二道闩）。"""
        lease = LeaseManager(db)
        first = lease.claim(run_id, "worker-1")
        _expire_heartbeat(db, run_id)
        second = lease.claim(run_id, "worker-2")

        assert first.fencing_version == 1
        assert second.fencing_version == 2

    def test_stale_lease_can_be_taken_over(self, db: Session, run_id: uuid.UUID) -> None:
        """前任心跳过期 → 换一个 worker 接着跑，无需人工介入。"""
        lease = LeaseManager(db)
        lease.claim(run_id, "worker-1")
        _expire_heartbeat(db, run_id)

        result = lease.claim(run_id, "worker-2")
        assert result.claimed is True
        assert _reload(db, run_id).lease_owner == "worker-2"

    def test_cancelled_run_cannot_be_claimed(self, db: Session, run_id: uuid.UUID) -> None:
        RunService(db).cancel_run(run_id)
        assert LeaseManager(db).claim(run_id, "worker-1").claimed is False

    def test_finished_run_cannot_be_claimed(self, db: Session, run_id: uuid.UUID) -> None:
        run = _reload(db, run_id)
        run.status = "succeeded"
        db.flush()
        assert LeaseManager(db).claim(run_id, "worker-1").claimed is False


# ---------------------------------------------------------------------------
# 心跳：同时充当取消检查
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_owner_heartbeat_succeeds(self, db: Session, run_id: uuid.UUID) -> None:
        lease = LeaseManager(db)
        lease.claim(run_id, "worker-1")
        assert lease.heartbeat(run_id, "worker-1") is True

    def test_non_owner_heartbeat_fails(self, db: Session, run_id: uuid.UUID) -> None:
        lease = LeaseManager(db)
        lease.claim(run_id, "worker-1")
        assert lease.heartbeat(run_id, "worker-2") is False

    def test_heartbeat_fails_after_cancellation(self, db: Session, run_id: uuid.UUID) -> None:
        """**关键性质**：run 被取消后心跳立刻失败，worker 据此自行停止。

        不需要额外查一次状态，也不可能出现「查到还在跑、写完却已被取消」的窗口。
        """
        lease = LeaseManager(db)
        lease.claim(run_id, "worker-1")
        RunService(db).cancel_run(run_id)

        assert lease.heartbeat(run_id, "worker-1") is False

    def test_heartbeat_fails_after_takeover(self, db: Session, run_id: uuid.UUID) -> None:
        """被接管后，旧 worker 的心跳必须失败——否则两个 worker 会同时干活。"""
        lease = LeaseManager(db)
        lease.claim(run_id, "worker-1")
        _expire_heartbeat(db, run_id)
        lease.claim(run_id, "worker-2")

        assert lease.heartbeat(run_id, "worker-1") is False
        assert lease.heartbeat(run_id, "worker-2") is True

    def test_heartbeat_refreshes_timestamp(self, db: Session, run_id: uuid.UUID) -> None:
        lease = LeaseManager(db)
        lease.claim(run_id, "worker-1")
        _expire_heartbeat(db, run_id)

        lease.heartbeat(run_id, "worker-1")
        # 心跳后不再算过期：再领一次应当失败
        assert lease.claim(run_id, "worker-2").claimed is False


# ---------------------------------------------------------------------------
# 结束
# ---------------------------------------------------------------------------


class TestFinish:
    def test_owner_can_finish_succeeded(self, db: Session, run_id: uuid.UUID) -> None:
        lease = LeaseManager(db)
        lease.claim(run_id, "worker-1")

        assert lease.finish(run_id, "worker-1", status="succeeded", result_summary={"recall": 0.9}) is True

        run = _reload(db, run_id)
        assert run.status == "succeeded"
        assert run.finished_at is not None
        assert run.lease_owner is None
        assert float(run.progress) == 100.0
        assert run.result_summary == {"recall": 0.9}

    def test_non_owner_cannot_finish(self, db: Session, run_id: uuid.UUID) -> None:
        lease = LeaseManager(db)
        lease.claim(run_id, "worker-1")
        assert lease.finish(run_id, "worker-2", status="succeeded") is False

    def test_reclaimed_worker_cannot_write_result(self, db: Session, run_id: uuid.UUID) -> None:
        """**防脑裂**：卡了很久的 worker 醒来后不能覆盖别人已跑完的结果。"""
        lease = LeaseManager(db)
        lease.claim(run_id, "worker-1")
        _expire_heartbeat(db, run_id)
        lease.reap_stale_runs()
        lease.claim(run_id, "worker-2")

        # 旧 worker 现在才来写结果 → 必须被拒
        assert lease.finish(run_id, "worker-1", status="succeeded") is False
        assert _reload(db, run_id).lease_owner == "worker-2"

    def test_cancelled_run_cannot_be_marked_succeeded(
        self, db: Session, run_id: uuid.UUID
    ) -> None:
        """取消优先于 worker 的完成——否则「取消了却还是 succeeded」会让用户困惑。"""
        lease = LeaseManager(db)
        lease.claim(run_id, "worker-1")
        RunService(db).cancel_run(run_id)

        assert lease.finish(run_id, "worker-1", status="succeeded") is False
        assert _reload(db, run_id).status == "cancelled"

    def test_failed_status_records_error(self, db: Session, run_id: uuid.UUID) -> None:
        lease = LeaseManager(db)
        lease.claim(run_id, "worker-1")
        lease.finish(run_id, "worker-1", status="failed", error_message="Java 端点 500")

        run = _reload(db, run_id)
        assert run.status == "failed"
        assert "500" in run.error_message

    def test_finish_rejects_non_terminal_status(self, db: Session, run_id: uuid.UUID) -> None:
        """只接受终态：传 running 会把 run 卡在一个「没有 holder 的 running」状态。"""
        lease = LeaseManager(db)
        lease.claim(run_id, "worker-1")
        with pytest.raises(ValueError):
            lease.finish(run_id, "worker-1", status="running")


# ---------------------------------------------------------------------------
# 僵尸回收
# ---------------------------------------------------------------------------


class TestReaper:
    def test_requeues_stale_run_with_retries_left(self, db: Session, run_id: uuid.UUID) -> None:
        """worker 崩溃多半是偶发的，重跑一次常常就好了 → 放回 pending 而非直接失败。"""
        lease = LeaseManager(db)
        lease.claim(run_id, "worker-1")
        _expire_heartbeat(db, run_id)

        result = lease.reap_stale_runs()

        assert run_id in result.requeued
        run = _reload(db, run_id)
        assert run.status == "pending"
        assert run.retry_count == 1
        assert run.lease_owner is None
        assert run.heartbeat_at is None

    def test_fails_stale_run_when_retries_exhausted(self, db: Session, run_id: uuid.UUID) -> None:
        """重试耗尽的置 failed，避免必然失败的 run 无限占用调度与命名空间。"""
        run = _reload(db, run_id)
        run.retry_count = get_settings().run_max_retries
        db.flush()

        lease = LeaseManager(db)
        lease.claim(run_id, "worker-1")
        _expire_heartbeat(db, run_id)

        result = lease.reap_stale_runs()

        assert run_id in result.failed
        run = _reload(db, run_id)
        assert run.status == "failed"
        assert run.finished_at is not None

    def test_does_not_touch_fresh_run(self, db: Session, run_id: uuid.UUID) -> None:
        """心跳新鲜的 run 不能被回收——否则正常跑着的任务会被中途打断。

        断言**只针对本用例自己的 run**，不断言 ``result.total == 0``：
        reaper 按设计扫全库，而库里可能还有别的会话/别的测试留下的僵尸行。
        「全库没有僵尸」这类断言天然依赖库的洁净度，是脆弱测试的典型来源。
        """
        LeaseManager(db).claim(run_id, "worker-1")

        result = LeaseManager(db).reap_stale_runs()

        assert run_id not in result.requeued
        assert run_id not in result.failed
        assert _reload(db, run_id).status == "running"

    def test_does_not_touch_pending_run(self, db: Session, run_id: uuid.UUID) -> None:
        """pending 的 run 没有租约，不是「僵尸」。"""
        result = LeaseManager(db).reap_stale_runs()
        assert run_id not in result.requeued
        assert run_id not in result.failed

    def test_released_run_can_be_claimed_again(self, db: Session, run_id: uuid.UUID) -> None:
        """回收后应能立刻被重新调度——这是自愈闭环的最后一环。"""
        lease = LeaseManager(db)
        lease.claim(run_id, "worker-1")
        _expire_heartbeat(db, run_id)
        lease.reap_stale_runs()

        assert lease.claim(run_id, "worker-2").claimed is True

    def test_reaper_releases_exclusive_guard(self, db: Session) -> None:
        """被回收的独占 run 必须释放守卫。

        漏掉这一步的后果很具体：守卫仍指着那个死掉的 run，所有后续独占 run
        都会被拒，直到守卫自己超时——故障恢复时间被无谓地拉长一个周期。
        """
        suffix = uuid.uuid4().hex[:8]
        dataset_id = db.execute(
            text("INSERT INTO eval_datasets(name) VALUES (:n) RETURNING id"), {"n": f"ex-{suffix}"}
        ).scalar_one()
        version_id = db.execute(
            text(
                "INSERT INTO eval_dataset_versions(dataset_id, version, schema_version, source, content_digest) "
                "VALUES (:d, 'v1', 1, 'manual', :digest) RETURNING id"
            ),
            {"d": dataset_id, "digest": f"sha256:{uuid.uuid4().hex}"},
        ).scalar_one()
        snapshot, _ = ParamService(db).get_or_create_param_snapshot(
            name=f"ex-snap-{suffix}",
            params={
                "vector_store": f"ex-{suffix}",
                "rrf_k": 2,
                "alpha": 0.2,
                "beta": 0.2,
                "recency_half_life_days": 2.0,
                "profile_boost": 0.0,
                "min_confidence": 0.0,
                "inject_max_tokens": 20,
            },
        )
        model, _ = ParamService(db).get_or_create_model_version(
            embedding_model_id=f"ex-emb-{suffix}", reranker_model_id=f"ex-rr-{suffix}"
        )
        exclusive_run, _ = RunService(db).create_run(
            RunCreateRequest(
                dataset_version_id=version_id,
                param_snapshot_id=snapshot.id,
                model_version_id=model.id,
                mode="exact",
                exclusive=True,
            ),
            created_by=None,
        )
        assert db.get(RunGuard, 1).exclusive_owner == exclusive_run.id

        lease = LeaseManager(db)
        lease.claim(exclusive_run.id, "worker-1")
        _expire_heartbeat(db, exclusive_run.id)
        lease.reap_stale_runs()

        db.expire_all()
        assert db.get(RunGuard, 1).exclusive_owner is None


# ---------------------------------------------------------------------------
# 只读辅助
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_is_owned_by(self, db: Session, run_id: uuid.UUID) -> None:
        lease = LeaseManager(db)
        assert lease.is_owned_by(run_id, "worker-1") is False
        lease.claim(run_id, "worker-1")
        assert lease.is_owned_by(run_id, "worker-1") is True
        assert lease.is_owned_by(run_id, "worker-2") is False

    def test_get_run_returns_none_for_unknown(self, db: Session) -> None:
        assert LeaseManager(db).get_run(uuid.uuid4()) is None


# ---------------------------------------------------------------------------
# pending 补投（P1-A4）
#
# 这一组的存在理由：`dispatch.py` 的 docstring 长期承诺「run 留在 pending 等待
# 调度器补投」，而调度器并不存在——reaper 只处理 status='running'。
# broker 丢消息时，run 会永久 pending **并占住 uq_runs_active_cfg 的槽位**，
# 那套配置从此起不了新 run。下面用「把 created_at 推到过去」来模拟滞留，
# 不必真的等宽限期。
# ---------------------------------------------------------------------------


def _age_run(db: Session, run_id: uuid.UUID, *, seconds_ago: int) -> None:
    """把创建时间推到过去，模拟 run 已经滞留了这么久。"""
    db.execute(
        text("UPDATE eval_runs SET created_at = now() - make_interval(secs => :s) WHERE id = :id"),
        {"s": seconds_ago, "id": str(run_id)},
    )
    db.flush()


# 注意：补投调度器**按设计就是全库范围的**（它扫的是「所有滞留在 pending 的 run」，
# 不是某一个用例的 run）。因此这里的断言一律用「本用例的 run 是否在其中」，
# 而不是断言整个返回值等于某个元组——后者在开发库里存有历史遗留 pending run 时
# 必然失败，且失败原因与被测逻辑毫无关系。


class TestPendingRedispatch:
    def test_fresh_pending_is_not_selected(self, db: Session, run_id: uuid.UUID) -> None:
        """刚落库的 pending 不该被补投——那会和创建时那次正常投递抢跑。"""
        selected = LeaseManager(db).select_dispatchable_pending_runs()
        assert run_id not in selected

    def test_pending_past_grace_is_selected(self, db: Session, run_id: uuid.UUID) -> None:
        _age_run(db, run_id, seconds_ago=120)

        selected = LeaseManager(db).select_dispatchable_pending_runs(
            grace_seconds=60, max_age_seconds=3600
        )
        assert run_id in selected

    def test_running_run_is_never_selected(self, db: Session, run_id: uuid.UUID) -> None:
        """补投只针对 pending：已跑起来的归 reaper 管（它有租约可以判断死亡）。"""
        lease = LeaseManager(db)
        _age_run(db, run_id, seconds_ago=120)
        lease.claim(run_id, "worker-1")
        db.flush()

        assert run_id not in lease.select_dispatchable_pending_runs(grace_seconds=60)

    def test_expired_pending_is_failed_with_reason(self, db: Session, run_id: uuid.UUID) -> None:
        """超龄的 pending 要有了断，否则它会永远占着并发槽位。"""
        _age_run(db, run_id, seconds_ago=7200)

        expired = LeaseManager(db).fail_expired_pending_runs(max_age_seconds=3600)

        assert run_id in expired
        row = _reload(db, run_id)
        assert row.status == "failed"
        assert row.finished_at is not None
        assert "3600" in (row.error_message or ""), "错误信息要能说明是按哪个阈值判的"

    def test_expired_pending_frees_the_config_slot(
        self, db: Session, run_id: uuid.UUID
    ) -> None:
        """**这是补投调度器最重要的作用**：把并发槽位还回去。

        滞留的 pending run 会一直占着 ``uq_runs_active_cfg``，同配置的新 run 全部 409。
        不给了断的话，那套配置就永久废了，而且没人知道原因。
        """
        run = _reload(db, run_id)
        _age_run(db, run_id, seconds_ago=7200)
        LeaseManager(db).fail_expired_pending_runs(max_age_seconds=3600)
        db.flush()

        # 同一个配置指纹 + 同一个 case_limit，此时应当能再建一个 run。
        again, created = RunService(db).create_run(
            RunCreateRequest(
                dataset_version_id=run.dataset_version_id,
                param_snapshot_id=run.param_snapshot_id,
                model_version_id=run.model_version_id,
                mode=run.mode,
                case_limit=run.case_limit,
            ),
            created_by=None,
        )
        assert created is True
        assert again.id != run_id

    def test_selection_is_capped_by_batch(self, db: Session, run_id: uuid.UUID) -> None:
        """有上限——故障恢复时积压上千个 run，一次全投会把 broker 再打垮一次。

        为了造出第二个**可以并存**的 pending run，这里给它一个不同的 case_limit：
        case_limit 参与 config_fingerprint，因此两个 run 不撞 ``uq_runs_active_cfg``。
        （这一点本身也说明那个指纹改动是必要的——否则连这种测试都写不出来。）
        """
        run = _reload(db, run_id)
        second, _ = RunService(db).create_run(
            RunCreateRequest(
                dataset_version_id=run.dataset_version_id,
                param_snapshot_id=run.param_snapshot_id,
                model_version_id=run.model_version_id,
                mode=run.mode,
                case_limit=1,
            ),
            created_by=None,
        )
        _age_run(db, run_id, seconds_ago=120)
        _age_run(db, second.id, seconds_ago=120)
        db.flush()

        lease = LeaseManager(db)
        both = lease.select_dispatchable_pending_runs(grace_seconds=60, batch=100)
        assert {run_id, second.id} <= set(both)

        # LIMIT 必须生效：无论全库有多少个可投的 run，batch=1 都只能返回一条。
        assert len(lease.select_dispatchable_pending_runs(grace_seconds=60, batch=1)) == 1
