"""``eval_user_id`` 派生的纯单测（不需要数据库）。

核心性质是**纯函数**：同一指纹在任何进程、任何时候都派生出同一个 id。
用随机分配或数据库发号会让「同配置换个进程跑」落到不同命名空间，
历史 run 与语料对不上——这条性质只能用「重复计算结果恒等」来钉。
"""

from __future__ import annotations

import pytest

from app.runs.eval_user import (
    EvalUserIdRangeError,
    derive_eval_user_id,
    eval_namespace_bounds,
    is_in_eval_namespace,
)
from app.settings.config import Settings

# 与 AgentWrite MemoryProperties.Eval 对齐的默认区间
BASE = 9_000_000_000
SPAN = 1_000_000


def _settings(**overrides) -> Settings:
    base = {"eval_user_id_base": BASE, "eval_user_id_range": SPAN}
    base.update(overrides)
    return Settings(**base)


def _fp(seed: str) -> str:
    import hashlib

    return f"sha256:{hashlib.sha256(seed.encode()).hexdigest()}"


class TestDerivation:
    def test_is_deterministic(self) -> None:
        assert derive_eval_user_id(_fp("a"), _settings()) == derive_eval_user_id(
            _fp("a"), _settings()
        )

    def test_always_lands_in_namespace(self) -> None:
        """越界不是「差一点」——Java 侧 validateEvalUserId 会直接以 E0403 拒绝。"""
        lower, upper = eval_namespace_bounds(_settings())
        for index in range(500):
            value = derive_eval_user_id(_fp(f"cfg-{index}"), _settings())
            assert lower <= value < upper, f"越界: {value}"

    def test_different_fingerprints_usually_differ(self) -> None:
        """派生应尽量分散：500 个不同指纹落到 1e6 的区间里，碰撞应极少。

        允许少量碰撞（取模本来就可能撞），但若大面积相同说明取模用错了对象
        （例如误用了恒定的 `sha256:` 前缀）。
        """
        values = {derive_eval_user_id(_fp(f"cfg-{index}"), _settings()) for index in range(500)}
        assert len(values) > 495, f"分布过于集中: 仅 {len(values)} 个不同值"

    def test_distribution_spans_the_range(self) -> None:
        """不是「全挤在区间开头」——那说明取模对象退化了。"""
        values = [derive_eval_user_id(_fp(f"cfg-{index}"), _settings()) for index in range(200)]
        assert max(values) - min(values) > SPAN // 10

    def test_custom_range_is_respected(self) -> None:
        settings = _settings(eval_user_id_base=100, eval_user_id_range=10)
        for index in range(50):
            assert 100 <= derive_eval_user_id(_fp(f"x{index}"), settings) < 110

    def test_is_in_eval_namespace(self) -> None:
        settings = _settings()
        assert is_in_eval_namespace(BASE, settings) is True
        assert is_in_eval_namespace(BASE + SPAN - 1, settings) is True
        # 边界：上界是开区间
        assert is_in_eval_namespace(BASE + SPAN, settings) is False
        assert is_in_eval_namespace(BASE - 1, settings) is False
        # 业务 userId 不能落进来
        assert is_in_eval_namespace(1, settings) is False


class TestValidation:
    def test_non_positive_range_is_rejected(self) -> None:
        with pytest.raises(EvalUserIdRangeError):
            derive_eval_user_id(_fp("a"), _settings(eval_user_id_range=0))

    def test_negative_range_is_rejected(self) -> None:
        with pytest.raises(EvalUserIdRangeError):
            derive_eval_user_id(_fp("a"), _settings(eval_user_id_range=-5))

    def test_negative_base_is_rejected(self) -> None:
        with pytest.raises(EvalUserIdRangeError):
            derive_eval_user_id(_fp("a"), _settings(eval_user_id_base=-1))

    def test_short_digest_is_rejected(self) -> None:
        """指纹格式不对时必须报错，不能拿一个截断值凑合算——那会静默改变派生结果。"""
        with pytest.raises(EvalUserIdRangeError):
            derive_eval_user_id("sha256:abc", _settings())

    def test_non_hex_digest_is_rejected(self) -> None:
        with pytest.raises(EvalUserIdRangeError):
            derive_eval_user_id("sha256:" + "z" * 32, _settings())

    def test_fingerprint_without_prefix_is_accepted(self) -> None:
        """兼容不带 ``sha256:`` 前缀的输入（取模只关心摘要部分）。"""
        import hashlib

        digest = hashlib.sha256(b"x").hexdigest()
        assert derive_eval_user_id(digest, _settings()) == derive_eval_user_id(
            f"sha256:{digest}", _settings()
        )
