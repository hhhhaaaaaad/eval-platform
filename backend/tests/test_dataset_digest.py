"""哈希与摘要工具的纯单测（不需要数据库，永远会跑）。

这批用例守的是「同一份评测集在任何环境得到同一指纹」这条性质。指纹一旦不稳定，
版本去重会失效、跨环境指标无法对比，而且**故障是静默的**——不会报错，
只会让两个本该相同的版本被判为不同。所以这里用穷举式断言把它钉死。
"""

from __future__ import annotations

import pytest

from app.datasets.digest import (
    CASE_TYPE_CONVERSATION_TO_MEMORY,
    CASE_TYPE_QUERY_TO_MEMORY,
    canonical_json,
    case_content_hash,
    memory_content_hash,
    memory_content_hashes,
    version_content_digest,
)


def _case(
    *,
    case_type: str = CASE_TYPE_QUERY_TO_MEMORY,
    group_key: str = "g1",
    payload: dict | None = None,
    ground_truth: dict | None = None,
) -> dict:
    payload = payload if payload is not None else {"query_id": "q1", "query": "咖啡"}
    ground_truth = ground_truth if ground_truth is not None else {"relevant_memory_ids": [1]}
    return {
        "case_type": case_type,
        "group_key": group_key,
        "content_hash": case_content_hash(case_type, payload),
        "payload": payload,
        "ground_truth": ground_truth,
    }


# ---------------------------------------------------------------------------
# canonical_json：跨环境稳定性
# ---------------------------------------------------------------------------


class TestCanonicalJson:
    def test_key_order_does_not_matter(self) -> None:
        """Python dict 保插入序；不同来源构造的同义对象键序可能不同，必须归一。"""
        assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})

    def test_no_incidental_whitespace(self) -> None:
        assert canonical_json({"a": 1, "b": [1, 2]}) == '{"a":1,"b":[1,2]}'

    def test_non_ascii_is_not_escaped(self) -> None:
        """中文不能被转义成 \\uXXXX——不同库的转义大小写习惯不一致，会破坏稳定性。"""
        assert canonical_json({"k": "咖啡"}) == '{"k":"咖啡"}'

    def test_nested_structures_are_normalized_recursively(self) -> None:
        assert canonical_json({"x": {"b": 1, "a": 2}}) == canonical_json({"x": {"a": 2, "b": 1}})

    def test_nan_is_rejected(self) -> None:
        """NaN 不是合法 JSON，且 NaN != NaN——含它的 payload 永远算不出稳定指纹。"""
        with pytest.raises(ValueError):
            canonical_json({"v": float("nan")})

    def test_infinity_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            canonical_json({"v": float("inf")})

    def test_float_representation_is_stable(self) -> None:
        assert canonical_json({"v": 0.1}) == canonical_json({"v": 1e-1})


# ---------------------------------------------------------------------------
# case_content_hash
# ---------------------------------------------------------------------------


class TestCaseContentHash:
    def test_is_deterministic(self) -> None:
        payload = {"query_id": "q1", "query": "咖啡"}
        assert case_content_hash(CASE_TYPE_QUERY_TO_MEMORY, payload) == case_content_hash(
            CASE_TYPE_QUERY_TO_MEMORY, payload
        )

    def test_payload_key_order_does_not_change_hash(self) -> None:
        a = case_content_hash(CASE_TYPE_QUERY_TO_MEMORY, {"query": "x", "query_id": "q1"})
        b = case_content_hash(CASE_TYPE_QUERY_TO_MEMORY, {"query_id": "q1", "query": "x"})
        assert a == b

    def test_payload_change_changes_hash(self) -> None:
        a = case_content_hash(CASE_TYPE_QUERY_TO_MEMORY, {"query_id": "q1", "query": "咖啡"})
        b = case_content_hash(CASE_TYPE_QUERY_TO_MEMORY, {"query_id": "q1", "query": "茶"})
        assert a != b

    def test_same_payload_different_case_type_differs(self) -> None:
        """同一个 query 在「检索」与「注入」两个维度是两条 case，若哈希相同会被误去重。"""
        payload = {"query_id": "q1", "query": "咖啡"}
        assert case_content_hash(CASE_TYPE_QUERY_TO_MEMORY, payload) != case_content_hash(
            CASE_TYPE_CONVERSATION_TO_MEMORY, payload
        )

    def test_is_algorithm_prefixed(self) -> None:
        """前缀算法名，将来换算法时新旧值不会撞在一起。"""
        assert case_content_hash(CASE_TYPE_QUERY_TO_MEMORY, {"a": 1}).startswith("sha256:")


# ---------------------------------------------------------------------------
# version_content_digest
# ---------------------------------------------------------------------------


class TestVersionContentDigest:
    def test_ground_truth_change_changes_digest(self) -> None:
        """**EP-4 验收条款**：只改标注（不动输入）也必须改变 digest。

        否则「同一批 query、不同答案」的两个版本会被判为同一版，指标对比失去意义。
        """
        before = [_case(ground_truth={"relevant_memory_ids": [1, 2]})]
        after = [_case(ground_truth={"relevant_memory_ids": [1, 3]})]

        assert version_content_digest(before) != version_content_digest(after)

    def test_payload_change_changes_digest(self) -> None:
        a = [_case(payload={"query_id": "q1", "query": "咖啡"})]
        b = [_case(payload={"query_id": "q1", "query": "茶"})]
        assert version_content_digest(a) != version_content_digest(b)

    def test_order_of_cases_does_not_matter(self) -> None:
        """导入时按文件顺序还是按分组顺序处理，不应产出不同指纹。"""
        first = _case(payload={"query_id": "q1", "query": "a"})
        second = _case(payload={"query_id": "q2", "query": "b"})

        assert version_content_digest([first, second]) == version_content_digest([second, first])

    def test_group_key_change_changes_digest(self) -> None:
        """group_key 不参与 content_hash（否则换个标签就能重复导入），但要进 digest。"""
        a = [_case(group_key="g1")]
        b = [_case(group_key="g2")]
        assert version_content_digest(a) != version_content_digest(b)

    def test_case_type_change_changes_digest(self) -> None:
        payload = {"query_id": "q1", "query": "a"}
        a = [_case(case_type=CASE_TYPE_QUERY_TO_MEMORY, payload=payload)]
        b = [_case(case_type=CASE_TYPE_CONVERSATION_TO_MEMORY, payload=payload)]
        assert version_content_digest(a) != version_content_digest(b)

    def test_empty_version_is_stable(self) -> None:
        """空版本不常见，但行为要确定（服务层另有 min_length=1 拦截）。"""
        assert version_content_digest([]) == version_content_digest([])

    def test_duplicate_case_does_not_collapse(self) -> None:
        """同一 case 出现两次必须改变 digest——否则重复导入检测会漏掉规模虚高。"""
        single = [_case()]
        doubled = [_case(), _case()]
        assert version_content_digest(single) != version_content_digest(doubled)

    def test_is_algorithm_prefixed(self) -> None:
        assert version_content_digest([]).startswith("sha256:")


# ---------------------------------------------------------------------------
# 记忆级内容哈希
# ---------------------------------------------------------------------------


class TestMemoryContentHash:
    def test_matches_md5_of_utf8(self) -> None:
        """必须与 AgentWrite 侧 contentHash 同算法（md5(UTF-8)）才能直接对账。"""
        import hashlib

        assert memory_content_hash("咖啡") == hashlib.md5("咖啡".encode()).hexdigest()

    def test_strips_surrounding_whitespace(self) -> None:
        assert memory_content_hash("  咖啡  ") == memory_content_hash("咖啡")

    def test_is_case_sensitive(self) -> None:
        """不做大小写折叠：把「Java 17」和「java 17」判为同一条会虚高 Recall。"""
        assert memory_content_hash("Java") != memory_content_hash("java")

    def test_different_content_differs(self) -> None:
        assert memory_content_hash("用户用 Java") != memory_content_hash("用户偏好 Java")

    def test_batch_preserves_order(self) -> None:
        """顺序对 NDCG 有意义，批量接口不能顺手排序。"""
        contents = ["a", "b", "c"]
        assert memory_content_hashes(contents) == [memory_content_hash(c) for c in contents]

    def test_empty_batch(self) -> None:
        assert memory_content_hashes([]) == []
