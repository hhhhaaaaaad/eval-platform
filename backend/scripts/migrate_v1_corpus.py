"""把 AgentWrite 项目里的「V1 评测语料」迁移为平台的一个数据集版本。

**为什么存在**：平台建好后一直没有自己的评测集——计划的 Phase2 item12（导入首份
真实评测语料）从未执行，导致 runs 只能跑冒烟脚本里那两条玩具 case。本脚本把 Java
侧 ``MemoryEvaluationTest`` 里已经人工标注好的语料搬进来，作为第一份可复现的评测集。

**语料真源在哪**：本脚本**不硬编码语料内容**，而是直接解析 AgentWrite 仓库里的测试
源文件（默认 ``DEFAULT_SOURCE``），逐字取出 ``initData()`` 里的 50 条 ``add(...)``
语料与 20 条 ``q(...)`` 查询。这样迁移结果与 Java 侧永远一致，不存在「抄错一个字」的
可能——contentHash 匹配要求逐字一致，任何转写/润色都会让整批标注失配。

**为什么 ground_truth 用 contents 而不是 ids**：Java 侧的语料 id 是 ``add(..., id)``
里的种子编号，平台运行时这些 id 由 seed 阶段重新产生、``reset`` 后会变，同一份评测集
跑两次拿到的 id 不同。按内容标注（``relevant_memory_contents``）后，运行时用 seed 返回
的 ``contentToId`` 解析成当次 run 的真实 id，指标才可复现。

**为什么 corpus 要单独放 config**：seed 阶段要把**全部 50 条**语料灌进评测命名空间，
检索时才有真实的干扰项。若只把「正确答案」放进 ground_truth，检索随便返回什么都很容易
命中，Recall/MRR 会虚高。版本级 ``config.corpus`` 承载这份完整语料（含与任何 query 都
无关的干扰项），供 seed 使用。

用法（需 API 已启动）：

    cd E:\\java\\eval-platform\\backend
    .\\.venv\\Scripts\\python.exe scripts\\migrate_v1_corpus.py --username <管理员> [--base http://127.0.0.1:8093] [--dry-run] [--force]

密码从环境变量 ``EVAL_BOOTSTRAP_PASSWORD`` 取，避免进入 shell history；缺失时（非
dry-run）报错退出。``--dry-run`` 只解析并打印统计，不调用任何 API。

幂等：数据集名固定为 ``v1-corpus``，先查同名数据集——已存在则打印提示并退出 0，
除非传 ``--force`` 再造一份（以带随机后缀的新名创建）。版本还有 content_digest 去重，
重复导入同一 digest 会被 API 以 409 拒绝，本脚本把 409 当成「已存在，跳过」处理而非报错。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from collections import Counter
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_BASE = "http://127.0.0.1:8093"
DEFAULT_SOURCE = (
    "E:/java/AgentWrite/sutone-agent-bok-app/src/test/java/"
    "cn/sutone/ai/test/domain/agent/service/memory/MemoryEvaluationTest.java"
)
DATASET_NAME = "v1-corpus"
VERSION = "v1"
PASSWORD_ENV = "EVAL_BOOTSTRAP_PASSWORD"

EXPECTED_CORPUS = 50
EXPECTED_QUERIES = 20
VALID_TYPES = frozenset({"fact", "preference", "knowledge", "event"})

#: ``add("<type>", "<content>", <id>)`` —— 语料三元组。内容不含双引号，逐字捕获。
_ADD_RE = re.compile(r'add\(\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,\s*(\d+)\s*\)')
#: ``q("<query>", List.of(<ids>), "<group>", <num>)`` —— 查询四元组。
_QUERY_RE = re.compile(r'q\(\s*"([^"]*)"\s*,\s*List\.of\(([^)]*)\)\s*,\s*"([^"]*)"\s*,\s*(\d+)\s*\)')


def _fail(message: str) -> None:
    print(f"[FAIL] {message}", file=sys.stderr)
    raise SystemExit(1)


def _step(message: str) -> None:
    print(f"[ .. ] {message}")


def _ok(message: str) -> None:
    print(f"[ OK ] {message}")


def parse_java(source_path: Path) -> tuple[list[dict], list[dict]]:
    """解析 Java 源文件，返回 (语料列表, 查询列表)。逐字读取，不做任何改写。"""
    text = source_path.read_text(encoding="utf-8-sig")

    corpus: list[dict] = []
    for match in _ADD_RE.finditer(text):
        corpus.append(
            {"id": int(match.group(3)), "type": match.group(1), "content": match.group(2)}
        )

    queries: list[dict] = []
    for match in _QUERY_RE.finditer(text):
        ids = [int(part) for part in match.group(2).split(",") if part.strip()]
        queries.append(
            {
                "num": int(match.group(4)),
                "query": match.group(1),
                "relevant_ids": ids,
                "group": match.group(3),
            }
        )

    return corpus, queries


def self_check(corpus: list[dict], queries: list[dict]) -> dict[int, str]:
    """解析后自检：数量、id 唯一性、type 合法性、relevantIds 可解析、query_id 无重复。

    任何一项不过就报错退出，绝不把脏数据发给 API。返回 ``id -> content`` 映射供后续使用。
    """
    errors: list[str] = []

    if len(corpus) != EXPECTED_CORPUS:
        errors.append(
            f"语料解析数量错误：期望 {EXPECTED_CORPUS} 条，实际解析到 {len(corpus)} 条"
        )
    if len(queries) != EXPECTED_QUERIES:
        errors.append(
            f"query 解析数量错误：期望 {EXPECTED_QUERIES} 条，实际解析到 {len(queries)} 条"
        )

    seen_corpus_ids: set[int] = set()
    for entry in corpus:
        if entry["id"] in seen_corpus_ids:
            errors.append(f"语料 id 重复: {entry['id']}")
        seen_corpus_ids.add(entry["id"])
        if entry["type"] not in VALID_TYPES:
            errors.append(f"语料 id={entry['id']} 的 type 非法: {entry['type']!r}")
        if not entry["content"].strip():
            errors.append(f"语料 id={entry['id']} 的 content 为空")

    seen_query_nums: set[int] = set()
    for query in queries:
        if query["num"] in seen_query_nums:
            errors.append(f"query num 重复: {query['num']}（query_id 会撞车）")
        seen_query_nums.add(query["num"])

    id_to_content = {entry["id"]: entry["content"] for entry in corpus}
    for query in queries:
        for relevant_id in query["relevant_ids"]:
            if relevant_id not in id_to_content:
                errors.append(
                    f"query num={query['num']}（{query['query']!r}）引用了不存在的语料 id={relevant_id}"
                )

    if errors:
        for error in errors:
            print(f"[FAIL] {error}", file=sys.stderr)
        raise SystemExit(1)

    return id_to_content


def build_cases(queries: list[dict], id_to_content: dict[int, str]) -> list[dict]:
    """把每条 query 展开成一个 query_to_memory case，ground_truth 按内容标注。"""
    cases: list[dict] = []
    for query in queries:
        cases.append(
            {
                "case_type": "query_to_memory",
                "group_key": query["group"],
                "payload": {
                    "query_id": f"q{query['num']}",
                    "query": query["query"],
                    "task_type": "LEGACY",
                },
                "ground_truth": {
                    "relevant_memory_contents": [
                        id_to_content[relevant_id] for relevant_id in query["relevant_ids"]
                    ],
                },
            }
        )
    return cases


def build_config(corpus: list[dict]) -> dict:
    """版本级 config：完整 50 条语料（含干扰项），供 seed 阶段灌入评测命名空间。"""
    return {"corpus": [{"content": entry["content"], "type": entry["type"]} for entry in corpus]}


def print_dry_run(corpus: list[dict], queries: list[dict]) -> None:
    """dry-run：只打印统计，不调用任何 API。"""
    type_counts = Counter(entry["type"] for entry in corpus)
    group_counts = Counter(query["group"] for query in queries)

    print("=== dry-run：只解析并统计，不调用任何 API ===")
    print(f"语料: {len(corpus)} 条")
    for memory_type in ("fact", "preference", "knowledge", "event"):
        print(f"  {memory_type:<12}: {type_counts.get(memory_type, 0)}")
    print(f"query: {len(queries)} 条")
    seen_groups: list[str] = []
    for query in queries:
        if query["group"] not in seen_groups:
            seen_groups.append(query["group"])
    for group in seen_groups:
        print(f"  {group:<12}: {group_counts[group]}")


def login(client: httpx.Client, username: str, password: str) -> str:
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    if resp.status_code != 200:
        _fail(f"登录失败 HTTP {resp.status_code}: {resp.text[:300]}")
    token = resp.json()["access_token"]
    _ok(f"登录成功 username={username}")
    return token


def find_dataset_by_name(client: httpx.Client, headers: dict[str, str], name: str) -> dict | None:
    resp = client.get("/api/v1/datasets", headers=headers)
    if resp.status_code != 200:
        _fail(f"查询数据集列表失败 HTTP {resp.status_code}: {resp.text[:300]}")
    for dataset in resp.json():
        if dataset.get("name") == name:
            return dataset
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="把 AgentWrite 的 V1 评测语料迁移为平台数据集版本")
    parser.add_argument("--username", default=None, help="管理员账号（非 dry-run 必填）")
    parser.add_argument("--base", default=DEFAULT_BASE, help="平台 API 基址")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="Java 评测测试源文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只解析并打印统计，不调用任何 API")
    parser.add_argument("--force", action="store_true", help="已存在同名数据集时再造一份（带后缀新名）")
    args = parser.parse_args()

    source_path = Path(args.source)
    if not source_path.is_file():
        _fail(f"语料真源不存在: {source_path}")

    _step(f"解析语料真源: {source_path}")
    corpus, queries = parse_java(source_path)
    id_to_content = self_check(corpus, queries)
    _ok(f"自检通过：{len(corpus)} 条语料 / {len(queries)} 条 query，relevantIds 全部可解析")

    cases = build_cases(queries, id_to_content)
    config = build_config(corpus)

    if args.dry_run:
        print_dry_run(corpus, queries)
        return 0

    if not args.username:
        _fail("非 dry-run 时必须提供 --username")
    password = os.environ.get(PASSWORD_ENV, "")
    if not password:
        _fail(f"未提供密码：设置环境变量 {PASSWORD_ENV} 后再运行")

    with httpx.Client(base_url=args.base, timeout=30.0) as client:
        headers = {"Authorization": f"Bearer {login(client, args.username, password)}"}

        existing = find_dataset_by_name(client, headers, DATASET_NAME)
        if existing is not None and not args.force:
            print(f"[ SKIP ] 数据集 '{DATASET_NAME}' 已存在（id={existing['id']}），幂等跳过。")
            print("         如需再造一份，请加 --force（会以带随机后缀的新名创建）。")
            return 0

        dataset_name = DATASET_NAME
        if existing is not None:
            dataset_name = f"{DATASET_NAME}-{uuid.uuid4().hex[:8]}"
            _step(f"检测到同名数据集，按 --force 以新名 '{dataset_name}' 再造一份")

        resp = client.post(
            "/api/v1/datasets",
            json={
                "name": dataset_name,
                "description": (
                    "V1 评测语料（迁移自 AgentWrite MemoryEvaluationTest.initData）："
                    "50 条语料 + 20 条 query，6 种能力分组。"
                ),
            },
            headers=headers,
        )
        if resp.status_code != 201:
            _fail(f"建数据集失败 HTTP {resp.status_code}: {resp.text[:300]}")
        dataset_id = resp.json()["id"]

        _step(f"导入版本 version={VERSION}（{len(cases)} case + config.corpus {len(corpus)} 条）")
        resp = client.post(
            f"/api/v1/datasets/{dataset_id}/versions",
            json={"version": VERSION, "source": "manual", "config": config, "cases": cases},
            headers=headers,
        )
        if resp.status_code == 201:
            body = resp.json()
            _ok(
                f"导入成功 dataset_id={dataset_id} version_id={body['id']} "
                f"digest={body['content_digest'][:20]}…"
            )
        elif resp.status_code == 409:
            # 版本号已存在 / content_digest 重复：都属于「已经导入过」，幂等跳过而非报错。
            print(f"[ SKIP ] 已存在，跳过（HTTP 409）: {resp.text[:300]}")
        else:
            _fail(f"导入版本失败 HTTP {resp.status_code}: {resp.text[:300]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
