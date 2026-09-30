"""fingerprint 组合的纯单测（EP-5 出口条件，不需要数据库）。

EP-5 验收条款是「参数变化、模型变化、ground truth 变化、mode 变化都会产生不同
fingerprint」，这里对四个输入逐一做**控制变量**验证：每次只改一个输入，
断言指纹必须变——若某个输入被漏进组合逻辑，对应那条用例就会红。
"""

from __future__ import annotations

import pytest

from app.datasets.digest import canonical_json
from app.params.fingerprint import (
    DEFAULT_FROZEN_KEYS,
    FrozenKeyError,
    config_fingerprint,
    fingerprint_from_components,
    model_config_hash,
    params_hash,
)

PARAMS = {
    "vector_store": "memory",
    "rrf_k": 60,
    "alpha": 0.5,
    "beta": 0.3,
    "recency_half_life_days": 14.0,
    "profile_boost": 0.2,
    "min_confidence": 0.4,
    "inject_max_tokens": 2000,
}


def _fingerprint(**overrides) -> str:
    base: dict = {
        "params": PARAMS,
        "embedding_model_id": "text-embedding-3-large",
        "reranker_model_id": "bge-reranker-v2",
        "model_config": {"dim": 3072},
        "dataset_content_digest": "sha256:aaa",
        "mode": "exact",
    }
    base.update(overrides)
    return fingerprint_from_components(**base)


# ---------------------------------------------------------------------------
# params_hash
# ---------------------------------------------------------------------------


class TestParamsHash:
    def test_is_deterministic(self) -> None:
        assert params_hash(PARAMS) == params_hash(PARAMS)

    def test_is_key_order_insensitive(self) -> None:
        """参数字典的构造顺序不该影响哈希——重排键是常见且无意义的差异。"""
        reordered = {key: PARAMS[key] for key in reversed(list(PARAMS))}
        assert params_hash(PARAMS) == params_hash(reordered)

    def test_any_frozen_value_change_changes_hash(self) -> None:
        """**验收条款**：参数变化必须改变哈希。逐个字段改，一个都不能漏。"""
        for key in DEFAULT_FROZEN_KEYS:
            mutated = dict(PARAMS)
            original = mutated[key]
            mutated[key] = original + 1 if isinstance(original, (int, float)) else "changed"
            assert params_hash(mutated) != params_hash(PARAMS), f"字段 {key} 未参与 params_hash"

    def test_non_frozen_field_does_not_change_hash(self) -> None:
        """冻结集之外的字段不参与——这是 ``frozen_keys`` 存在的意义。"""
        noisy = {**PARAMS, "collected_at": "2026-09-30T00:00:00Z"}
        assert params_hash(noisy) == params_hash(PARAMS)

    def test_explicit_frozen_keys_are_respected(self) -> None:
        subset = ["alpha", "beta"]
        a = params_hash(PARAMS, subset)
        mutated = {**PARAMS, "alpha": 0.9}
        assert params_hash(mutated, subset) != a
        # rrf_k 不在冻结集内，改它不影响
        assert params_hash({**PARAMS, "rrf_k": 99}, subset) == a

    def test_frozen_key_set_itself_changes_hash(self) -> None:
        """把某个键移出冻结集改变了指纹语义，必须反映在哈希里。

        否则两次本来不可比的 run 会撞到同一个 params_hash。
        """
        assert params_hash(PARAMS, ["alpha", "beta"]) != params_hash(PARAMS, ["alpha"])

    def test_unknown_frozen_key_raises(self) -> None:
        """拼错的键名必须报错而不是跳过——跳过意味着「看起来在冻结、其实没冻」。"""
        with pytest.raises(FrozenKeyError):
            params_hash(PARAMS, ["alpha", "alpah"])


# ---------------------------------------------------------------------------
# model_config_hash
# ---------------------------------------------------------------------------


class TestModelConfigHash:
    def test_is_deterministic(self) -> None:
        assert model_config_hash("e", "r", {"a": 1}) == model_config_hash("e", "r", {"a": 1})

    def test_none_config_equals_empty_config(self) -> None:
        assert model_config_hash("e", "r", None) == model_config_hash("e", "r", {})

    def test_embedding_change_changes_hash(self) -> None:
        assert model_config_hash("e1", "r", {}) != model_config_hash("e2", "r", {})

    def test_reranker_change_changes_hash(self) -> None:
        assert model_config_hash("e", "r1", {}) != model_config_hash("e", "r2", {})

    def test_config_change_changes_hash(self) -> None:
        assert model_config_hash("e", "r", {"dim": 1}) != model_config_hash("e", "r", {"dim": 2})

    def test_config_key_order_insensitive(self) -> None:
        assert model_config_hash("e", "r", {"a": 1, "b": 2}) == model_config_hash(
            "e", "r", {"b": 2, "a": 1}
        )


# ---------------------------------------------------------------------------
# config_fingerprint：四类输入的控制变量验证
# ---------------------------------------------------------------------------


class TestConfigFingerprint:
    def test_is_deterministic(self) -> None:
        assert _fingerprint() == _fingerprint()

    def test_params_change_changes_fingerprint(self) -> None:
        """**验收条款 1/4**：参数变化。"""
        assert _fingerprint(params={**PARAMS, "alpha": 0.9}) != _fingerprint()

    def test_model_change_changes_fingerprint(self) -> None:
        """**验收条款 2/4**：模型变化（embedding 与 reranker 各验一次）。"""
        assert _fingerprint(embedding_model_id="other-embed") != _fingerprint()
        assert _fingerprint(reranker_model_id="other-rerank") != _fingerprint()
        assert _fingerprint(model_config={"dim": 1536}) != _fingerprint()

    def test_ground_truth_change_changes_fingerprint(self) -> None:
        """**验收条款 3/4**：ground truth 变化（体现为 dataset content_digest 变化）。"""
        assert _fingerprint(dataset_content_digest="sha256:bbb") != _fingerprint()

    def test_mode_change_changes_fingerprint(self) -> None:
        """**验收条款 4/4**：mode 变化。

        exact 与 hnsw 的召回率不可直接比较，必须是不同指纹、不同可比分层。
        """
        assert _fingerprint(mode="hnsw") != _fingerprint(mode="exact")

    def test_rejects_invalid_mode(self) -> None:
        """拼错的 mode 若不拦，会生成一个与正确值不同的指纹，把同一配置拆成两组。"""
        with pytest.raises(ValueError):
            _fingerprint(mode="HNSW")

    def test_is_algorithm_prefixed(self) -> None:
        assert _fingerprint().startswith("sha256:")

    def test_component_hashes_are_reproducible_from_parts(self) -> None:
        """指纹应能由三个组成部分的哈希完全重建（可对账、可跨环境复算）。"""
        expected = config_fingerprint(
            params_digest=params_hash(PARAMS),
            model_digest=model_config_hash("text-embedding-3-large", "bge-reranker-v2", {"dim": 3072}),
            dataset_digest="sha256:aaa",
            mode="exact",
        )
        assert _fingerprint() == expected


# ---------------------------------------------------------------------------
# 跨环境稳定性
# ---------------------------------------------------------------------------


class TestCrossEnvironmentStability:
    def test_same_input_yields_same_hash_across_processes(self) -> None:
        """验收条款「相同 canonical_json 在不同环境 hash 一致」。

        这里无法真的换一台机器，但可以钉住导致跨环境漂移的三个来源：
        字典键序（PYTHONHASHSEED 随进程变化）、浮点表示、非 ASCII 转义。
        三者都被 canonical_json 归一，故同一份输入在任何进程里都得到同一哈希。
        若哪天有人给 canonical_json 去掉 sort_keys / 打开 ensure_ascii，这条会红。
        """
        # 键序不同（模拟不同进程里 dict 构造顺序不同）
        reordered_a = {"alpha": 0.5, "beta": 0.3, "rrf_k": 60}
        reordered_b = {"rrf_k": 60, "beta": 0.3, "alpha": 0.5}
        assert canonical_json(reordered_a) == canonical_json(reordered_b)

        # 浮点：0.1 与 1e-1 是同一个值的两种写法
        assert canonical_json({"x": 0.1}) == canonical_json({"x": 1e-1})

        # 非 ASCII 不被转义（不同 JSON 库的转义大小写习惯不一致）
        assert "\\u" not in canonical_json({"note": "中文标签"})

    def test_fingerprint_stable_across_repeated_computation(self) -> None:
        """同一进程内重复计算 100 次结果逐字相同——排除哈希随机化等因素。"""
        digests = {_fingerprint() for _ in range(100)}
        assert len(digests) == 1
