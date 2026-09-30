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
from app.params.service import ParamService
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
    db.rollback()
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
        return httpx.Response(
            200, json=_envelope(data={"inserted": 2, "existed": 0, "contentToId": {}})
        )

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
    assert outcome.metrics["recall_at_k"] == pytest.approx(1.0)
    assert outcome.metrics["case_count"] == 2.0

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
