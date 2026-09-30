"""端到端冒烟脚本：走真实 HTTP + 真实 Celery broker + 真实 Postgres。

**它补的是自动化测试覆盖不到的那一段**：测试里 Java 侧被 respx 拦截、
任务从没经过真实 broker，于是「投递是否真的到了 worker」「worker 里的
session 生命周期是否正常」「API 与 worker 对同一行的可见性是否一致」
这些从来没有被验证过。本脚本用真实进程跑一遍。

用法（需要 API 与 worker 都已启动）：

    cd E:\\java\\eval-platform\\backend
    .\\.venv\\Scripts\\python.exe scripts\\smoke_e2e.py --username e2e-admin

参数：

- ``--base``：平台 API 基址，默认 ``http://127.0.0.1:8093``
- ``--username`` / ``--password``：登录凭据；密码优先取环境变量
  ``EVAL_SMOKE_PASSWORD``，避免进入 shell history
- ``--expect``：期望的 run 终态。默认 ``failed``——**本脚本的设计场景是
  「AgentWrite 未启动时，平台能否干净地失败」**，而不是「评测能否成功」。
  要验证成功路径需先起 AgentWrite 并把 ``--expect`` 设为 ``succeeded``。

退出码 0 表示冒烟通过（拿到期望终态且 run 未被卡在 running）。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_BASE = "http://127.0.0.1:8093"
DEFAULT_TIMEOUT = 60.0


def _fail(message: str) -> None:
    print(f"[FAIL] {message}", file=sys.stderr)
    raise SystemExit(1)


def _step(message: str) -> None:
    print(f"[ .. ] {message}")


def _ok(message: str) -> None:
    print(f"[ OK ] {message}")


def login(client: httpx.Client, username: str, password: str) -> str:
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    if resp.status_code != 200:
        _fail(f"登录失败 HTTP {resp.status_code}: {resp.text[:300]}")
    token = resp.json()["access_token"]
    _ok(f"登录成功 role={resp.json()['role']}")
    return token


def create_dataset_with_cases(client: httpx.Client, headers: dict[str, str]) -> int:
    """建一个只含两条 query case 的数据集版本，返回 version_id。"""
    name = f"smoke-{uuid.uuid4().hex[:8]}"
    resp = client.post("/api/v1/datasets", json={"name": name}, headers=headers)
    if resp.status_code != 201:
        _fail(f"建数据集失败 HTTP {resp.status_code}: {resp.text[:300]}")
    dataset_id = resp.json()["id"]

    cases = [
        {
            "case_type": "query_to_memory",
            "group_key": "smoke",
            "payload": {"query_id": f"q{index}", "query": query, "task_type": "LEGACY"},
            "ground_truth": {
                "relevant_memory_contents": ["用户用 Java 17", "用户偏好美式咖啡"]
            },
        }
        for index, query in enumerate(("用户用什么技术栈？", "用户喜欢喝什么？"))
    ]
    resp = client.post(
        f"/api/v1/datasets/{dataset_id}/versions",
        json={"version": "v1", "source": "manual", "cases": cases},
        headers=headers,
    )
    if resp.status_code != 201:
        _fail(f"导入版本失败 HTTP {resp.status_code}: {resp.text[:300]}")
    version_id = resp.json()["id"]
    _ok(f"数据集就绪 dataset_id={dataset_id} version_id={version_id} digest={resp.json()['content_digest'][:20]}…")
    return version_id


def create_config(client: httpx.Client, headers: dict[str, str]) -> tuple[int, int]:
    """建参数快照与模型版本，返回 (param_snapshot_id, model_version_id)。"""
    suffix = uuid.uuid4().hex[:8]
    resp = client.post(
        "/api/v1/params/snapshots",
        json={
            "name": f"smoke-snap-{suffix}",
            "params": {
                "vector_store": f"smoke-{suffix}",
                "rrf_k": 60,
                "alpha": 0.5,
                "beta": 0.3,
                "recency_half_life_days": 14.0,
                "profile_boost": 0.2,
                "min_confidence": 0.4,
                "inject_max_tokens": 2000,
            },
        },
        headers=headers,
    )
    if resp.status_code != 201:
        _fail(f"建参数快照失败 HTTP {resp.status_code}: {resp.text[:300]}")
    snapshot_id = resp.json()["id"]

    resp = client.post(
        "/api/v1/params/model-versions",
        json={
            "embedding_model_id": f"smoke-emb-{suffix}",
            "reranker_model_id": f"smoke-rr-{suffix}",
            "config": {},
        },
        headers=headers,
    )
    if resp.status_code != 201:
        _fail(f"建模型版本失败 HTTP {resp.status_code}: {resp.text[:300]}")
    _ok(f"配置就绪 snapshot_id={snapshot_id} model_version_id={resp.json()['id']}")
    return snapshot_id, resp.json()["id"]


def poll_run(
    client: httpx.Client, headers: dict[str, str], run_id: str, *, timeout: float
) -> dict:
    """轮询 run 直到离开 pending/running，或超时。"""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/api/v1/runs/{run_id}", headers=headers)
        if resp.status_code != 200:
            _fail(f"查询 run 失败 HTTP {resp.status_code}")
        last = resp.json()
        if last["status"] not in ("pending", "running"):
            return last
        time.sleep(1.0)
    return last


def cleanup(
    *,
    dataset_version_id: int,
    param_snapshot_id: int,
    model_version_id: int,
) -> None:
    """删除本次冒烟造出的数据。

    **必须显式清理**：这些接口都会 commit，没有回滚可依赖。不清理的话每次冒烟
    都往库里堆一份数据集+run，跑几十次后库就很难看了，还会让别处
    「全库没有 XX」这类断言莫名变红（这个坑已在测试里踩过两次）。

    只删本次冒烟造出的行，按外键依赖倒序：run → 用例/版本 → 数据集 → 配置。
    """
    from sqlalchemy import text

    from app.db.session import get_session_factory

    session = get_session_factory()()
    try:
        # 先取出 dataset_id——版本行一删就查不到它了，顺序反了会删不掉数据集。
        dataset_id = session.execute(
            text("SELECT dataset_id FROM eval_dataset_versions WHERE id = :v"),
            {"v": dataset_version_id},
        ).scalar_one_or_none()

        run_rows = session.execute(
            text("SELECT id FROM eval_runs WHERE dataset_version_id = :v"),
            {"v": dataset_version_id},
        ).scalars().all()
        for run_id in run_rows:
            # 独占守卫可能仍指着这个 run
            session.execute(
                text("UPDATE eval_run_guard SET exclusive_owner = NULL, owner_run_id = NULL, "
                     "acquired_at = NULL, heartbeat_at = NULL WHERE exclusive_owner = :r"),
                {"r": run_id},
            )
        session.execute(
            text("DELETE FROM eval_runs WHERE dataset_version_id = :v"), {"v": dataset_version_id}
        )
        session.execute(
            text("DELETE FROM eval_cases WHERE dataset_version_id = :v"), {"v": dataset_version_id}
        )
        session.execute(
            text("DELETE FROM eval_dataset_versions WHERE id = :v"), {"v": dataset_version_id}
        )
        if dataset_id is not None:
            session.execute(text("DELETE FROM eval_datasets WHERE id = :d"), {"d": dataset_id})
        session.execute(
            text("DELETE FROM eval_param_snapshots WHERE id = :s"), {"s": param_snapshot_id}
        )
        session.execute(
            text("DELETE FROM eval_model_versions WHERE id = :m"), {"m": model_version_id}
        )
        session.commit()
        print(f"[ OK ] 已清理本次冒烟数据（run {len(run_rows)} 个）")
    except Exception as exc:  # noqa: BLE001 — 清理失败不该掩盖冒烟结论
        session.rollback()
        print(f"[WARN] 清理未完成（不影响冒烟结论）: {exc}", file=sys.stderr)
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="评测平台端到端冒烟")
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--username", default="e2e-admin")
    parser.add_argument("--password", default=os.environ.get("EVAL_SMOKE_PASSWORD", ""))
    parser.add_argument("--expect", default="failed", choices=["failed", "succeeded"])
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--keep",
        action="store_true",
        help="保留本次冒烟造出的数据（默认成功时清理，失败时一律保留现场供排查）",
    )
    args = parser.parse_args()

    if not args.password:
        _fail("未提供密码：设置环境变量 EVAL_SMOKE_PASSWORD 或传 --password")

    with httpx.Client(base_url=args.base, timeout=30.0) as client:
        _step(f"探测平台 API {args.base}")
        try:
            ready = client.get("/api/v1/ready")
        except httpx.HTTPError as exc:
            _fail(f"平台 API 不可达: {exc}")
        if ready.status_code != 200:
            _fail(f"/ready 返回 HTTP {ready.status_code}")
        _ok(f"API 就绪: {ready.json()['checks']}")

        headers = {"Authorization": f"Bearer {login(client, args.username, args.password)}"}

        version_id = create_dataset_with_cases(client, headers)
        snapshot_id, model_id = create_config(client, headers)

        _step("创建 run（真实投递到 Celery broker）")
        resp = client.post(
            "/api/v1/runs",
            json={
                "dataset_version_id": version_id,
                "param_snapshot_id": snapshot_id,
                "model_version_id": model_id,
                "mode": "exact",
            },
            headers=headers,
        )
        if resp.status_code != 201:
            _fail(f"创建 run 失败 HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        run_id = body["id"]
        _ok(
            f"run 已创建 id={run_id} created={body['created']} "
            f"enqueued={body['enqueued']} eval_user_id={body['eval_user_id']}"
        )
        if not body["enqueued"]:
            _fail("任务未被投递（enqueued=False）——broker 或配置有问题")

        _step(f"等待 worker 消费并推进到终态（期望 {args.expect}）")
        final = poll_run(client, headers, run_id, timeout=args.timeout)

        status = final.get("status")
        print(f"       终态 status={status}")
        print(f"       current_stage={final.get('current_stage')}")
        print(f"       error_message={final.get('error_message')}")
        print(f"       checkpoint={final.get('checkpoint')}")

        if status in ("pending", "running"):
            _fail(f"run 卡在 {status} —— 任务可能没被消费，或 worker 崩了")
        if status != args.expect:
            _fail(f"终态 {status} 与期望 {args.expect} 不符")

        # 清理放在最后，且**只在成功时做**：失败时保留现场，run/checkpoint/error_message
        # 都是排查线索，删掉就等于把证据扔了。
        if args.keep:
            print(f"[ .. ] 按 --keep 保留数据：run_id={run_id} dataset_version_id={version_id}")
        else:
            cleanup(
                dataset_version_id=version_id,
                param_snapshot_id=snapshot_id,
                model_version_id=model_id,
            )

        _ok("冒烟通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
