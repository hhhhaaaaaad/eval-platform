"""Schema 约束的**真实数据库**行为测试（EP-1 出口条件）。

验证的不是「约束存在」，而是「约束真的会阻止违规写入」：

- `uq_runs_active_cfg`  : 同一 config_fingerprint 同时最多一个进行中的 run
- `uq_runs_active_user` : 同一 eval_user_id 同时最多一个进行中的 run
- `uq_runs_idempotency` : 同 idempotency_key 不重复
- `ck_eval_run_guard_single_row` : guard 表只能有一行（id = 1）

**需要 Postgres**。不可用时整模块跳过（`pytest.skip`），
这样无数据库的环境 `pytest` 仍然全绿——与 EP-0 集成测试的 `@EnabledIf` 门控同理。

每个用例在事务内执行并回滚，不污染库。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.settings.config import get_settings


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    try:
        eng = create_engine(get_settings().database_url)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — 探活失败即跳过，不给噪声
        pytest.skip(f"Postgres 不可用，跳过 schema 约束集成测试: {exc}")
    yield eng
    eng.dispose()


class _Fixture:
    """在事务内准备一组满足外键的前置数据，用完回滚。

    用 `__enter__` 返回一个可直接执行 SQL 的 connection；退出时 rollback，
    保证测试之间互不影响、也不在库里留垃圾。
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self.conn = None
        self._tx = None

    def __enter__(self):
        self.conn = self._engine.connect()
        self._tx = self.conn.begin()
        return self

    def __exit__(self, *exc) -> None:
        self._tx.rollback()
        self.conn.close()

    def seed(self) -> dict[str, object]:
        """建出 run 所需的全部外键前置行，返回一组可用 id。"""
        c = self.conn
        user_id = c.execute(
            text(
                "INSERT INTO eval_users(username, password_hash, role) "
                "VALUES (:u, 'x', 'admin') RETURNING id"
            ),
            {"u": f"t-{uuid.uuid4().hex[:8]}"},
        ).scalar_one()

        dataset_id = c.execute(
            text("INSERT INTO eval_datasets(name, created_by) VALUES ('ds', :u) RETURNING id"),
            {"u": user_id},
        ).scalar_one()

        dv_id = c.execute(
            text(
                "INSERT INTO eval_dataset_versions(dataset_id, version, schema_version, source, content_digest, created_by) "
                "VALUES (:d, 'v1', 1, 'manual', 'digest', :u) RETURNING id"
            ),
            {"d": dataset_id, "u": user_id},
        ).scalar_one()

        ps_id = c.execute(
            text(
                "INSERT INTO eval_param_snapshots(name, params, freeze_config, params_hash, created_by) "
                "VALUES ('p', '{}'::jsonb, '{}'::jsonb, :h, :u) RETURNING id"
            ),
            {"h": uuid.uuid4().hex, "u": user_id},
        ).scalar_one()

        mv_id = c.execute(
            text(
                "INSERT INTO eval_model_versions(embedding_model_id, reranker_model_id, config_hash, config) "
                "VALUES ('emb', 'rr', :h, '{}'::jsonb) RETURNING id"
            ),
            {"h": uuid.uuid4().hex},
        ).scalar_one()

        return {"user_id": user_id, "dv_id": dv_id, "ps_id": ps_id, "mv_id": mv_id}

    def insert_run(
        self,
        ids: dict[str, object],
        *,
        fingerprint: str,
        eval_user_id: int,
        status: str = "pending",
        idempotency_key: str | None = None,
    ) -> None:
        self.conn.execute(
            text(
                """
                INSERT INTO eval_runs(
                    config_fingerprint, idempotency_key, dataset_version_id,
                    param_snapshot_id, model_version_id, eval_user_id, status
                ) VALUES (
                    :fp, :ik, :dv, :ps, :mv, :eu, :st
                )
                """
            ),
            {
                "fp": fingerprint,
                "ik": idempotency_key,
                "dv": ids["dv_id"],
                "ps": ids["ps_id"],
                "mv": ids["mv_id"],
                "eu": eval_user_id,
                "st": status,
            },
        )


def test_active_cfg_blocks_second_pending_run(engine: Engine) -> None:
    """同一 config_fingerprint 已经有 pending run 时，再插一个必须失败。"""
    with _Fixture(engine) as f:
        ids = f.seed()
        f.insert_run(ids, fingerprint="cfg-A", eval_user_id=1)

        with pytest.raises(IntegrityError):
            f.insert_run(ids, fingerprint="cfg-A", eval_user_id=2)


def test_active_user_blocks_second_pending_run(engine: Engine) -> None:
    """同一 eval_user_id 已经有 pending run 时，换 config 也必须失败。"""
    with _Fixture(engine) as f:
        ids = f.seed()
        f.insert_run(ids, fingerprint="cfg-A", eval_user_id=100)

        with pytest.raises(IntegrityError):
            f.insert_run(ids, fingerprint="cfg-B", eval_user_id=100)


def test_terminal_run_frees_the_slot(engine: Engine) -> None:
    """旧 run 进入终态后，同 config 必须能再起新 run（partial index 的核心语义）。"""
    with _Fixture(engine) as f:
        ids = f.seed()
        f.insert_run(ids, fingerprint="cfg-A", eval_user_id=1, status="pending")

        f.conn.execute(
            text("UPDATE eval_runs SET status='succeeded' WHERE config_fingerprint='cfg-A'")
        )

        # 不应抛异常
        f.insert_run(ids, fingerprint="cfg-A", eval_user_id=1, status="pending")


def test_different_configs_run_in_parallel(engine: Engine) -> None:
    """不同 config 的 run 必须能并存——这是并行 A/B 的前提。"""
    with _Fixture(engine) as f:
        ids = f.seed()
        f.insert_run(ids, fingerprint="cfg-A", eval_user_id=11)
        f.insert_run(ids, fingerprint="cfg-B", eval_user_id=12)


def test_idempotency_key_is_unique(engine: Engine) -> None:
    """同 idempotency_key 重复插入必须失败（防双击建两个 run）。"""
    with _Fixture(engine) as f:
        ids = f.seed()
        f.insert_run(ids, fingerprint="cfg-A", eval_user_id=1, idempotency_key="key-1")

        with pytest.raises(IntegrityError):
            f.insert_run(ids, fingerprint="cfg-B", eval_user_id=2, idempotency_key="key-1")


def test_multiple_runs_without_idempotency_key_are_allowed(engine: Engine) -> None:
    """idempotency_key 为 NULL 时不应被唯一约束限制（NULL 不参与唯一性）。"""
    with _Fixture(engine) as f:
        ids = f.seed()
        f.insert_run(ids, fingerprint="cfg-A", eval_user_id=1, idempotency_key=None)
        f.insert_run(ids, fingerprint="cfg-B", eval_user_id=2, idempotency_key=None)


def test_run_guard_rejects_second_row(engine: Engine) -> None:
    """guard 表只允许 id=1 这一行（单行互斥占位）。"""
    with _Fixture(engine) as f, pytest.raises(IntegrityError):
        f.conn.execute(text("INSERT INTO eval_run_guard (id) VALUES (2)"))


def test_run_result_upsert_conflict_target(engine: Engine) -> None:
    """结果表唯一键必须是 upsert 的可用冲突目标，且同键重复插入被拒。"""
    with _Fixture(engine) as f:
        ids = f.seed()
        f.insert_run(ids, fingerprint="cfg-A", eval_user_id=1)
        run_id = f.conn.execute(text("SELECT id FROM eval_runs LIMIT 1")).scalar_one()

        params = {"r": run_id, "d": "retrieval", "m": "recall_at_5"}
        f.conn.execute(
            text(
                "INSERT INTO eval_run_results(run_id, dimension, metric_name, metric_value) "
                "VALUES (:r, :d, :m, 0.5)"
            ),
            params,
        )

        with pytest.raises(IntegrityError):
            f.conn.execute(
                text(
                    "INSERT INTO eval_run_results(run_id, dimension, metric_name, metric_value) "
                    "VALUES (:r, :d, :m, 0.9)"
                ),
                params,
            )
