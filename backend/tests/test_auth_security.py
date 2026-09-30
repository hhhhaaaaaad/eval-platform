"""``app.auth.security`` 单元测试：bcrypt 口令哈希 + JWT 签发/校验。

只测纯计算逻辑，不碰数据库、不碰 FastAPI——这样测试快且与 EP-3+ 的 ORM 变更解耦。

配置隔离：``get_settings`` 是 ``lru_cache`` 单例，一旦构造就会缓存整个进程。这里
用 autouse fixture 在每个用例前 ``cache_clear()`` + 注入确定的 ``JWT_SECRET`` 环境
变量，确保 (a) 不依赖仓库里可能存在的真实 ``.env``，(b) 用例之间不互相污染。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.auth.security import (
    InvalidTokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.settings.config import get_settings

_TEST_SECRET = "unit-test-secret-not-for-production"


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例都在干净的 settings 单例 + 固定 secret 下运行。"""
    monkeypatch.setenv("JWT_SECRET", _TEST_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# bcrypt
# --------------------------------------------------------------------------- #


def test_hash_then_verify_succeeds() -> None:
    """1. 哈希后用原密码校验通过。"""
    hashed = hash_password("correct-horse-battery-staple")
    assert hashed != "correct-horse-battery-staple"
    assert verify_password("correct-horse-battery-staple", hashed) is True


def test_verify_wrong_password_fails() -> None:
    """2. 错误密码校验失败。"""
    hashed = hash_password("right-password")
    assert verify_password("wrong-password", hashed) is False


def test_same_password_hashes_differ_but_both_verify() -> None:
    """3. 同一密码两次哈希结果不同（盐不同），但都能校验通过。"""
    first = hash_password("same-password")
    second = hash_password("same-password")
    assert first != second
    assert verify_password("same-password", first) is True
    assert verify_password("same-password", second) is True


def test_verify_invalid_hash_returns_false_without_raising() -> None:
    """4. 非法哈希串返回 False 而不抛异常（否则登录接口会 500）。"""
    assert verify_password("whatever", "not-a-valid-bcrypt-hash") is False
    assert verify_password("whatever", "") is False


def test_long_password_roundtrip_via_72_byte_truncation() -> None:
    """5. 超长（100 字符）密码哈希+校验仍一致，验证 72 字节截断策略。"""
    long_password = "p" * 100
    hashed = hash_password(long_password)
    assert verify_password(long_password, hashed) is True
    # 截断后前 72 字节相同即等价，而第 73 字节起被丢弃。
    assert verify_password("p" * 72, hashed) is True
    assert verify_password("p" * 71 + "x", hashed) is False


def test_long_multibyte_password_truncates_on_char_boundary() -> None:
    """5b. 多字节（中文）超长口令按 UTF-8 字节截断且不在字符中间切断。"""
    # 每个汉字 3 字节，30 个即 90 字节，超过 72。
    long_password = "密" * 30
    hashed = hash_password(long_password)
    assert verify_password(long_password, hashed) is True


# --------------------------------------------------------------------------- #
# JWT
# --------------------------------------------------------------------------- #


def test_create_then_decode_roundtrip() -> None:
    """6. create → decode 往返，sub/role 一致。"""
    token = create_access_token(subject="42", role="admin")
    payload = decode_access_token(token)
    assert payload.sub == "42"
    assert payload.role == "admin"
    assert payload.exp > int(datetime.now(UTC).timestamp())


def test_expired_token_raises_invalid_token_error() -> None:
    """7. 过期 token → InvalidTokenError（expires_minutes=-1 造过期）。"""
    token = create_access_token(subject="1", role="viewer", expires_minutes=-1)
    with pytest.raises(InvalidTokenError):
        decode_access_token(token)


def test_tampered_signature_raises_invalid_token_error() -> None:
    """8. 签名被篡改 → InvalidTokenError。"""
    token = create_access_token(subject="1", role="viewer")
    header, body, signature = token.split(".")
    tampered = f"{header}.{body}.{signature[:-2]}xx"
    with pytest.raises(InvalidTokenError):
        decode_access_token(tampered)


def test_token_signed_with_other_secret_raises() -> None:
    """9. 用不同 secret 签的 token → InvalidTokenError。"""
    settings = get_settings()
    now = datetime.now(UTC)
    forged = jwt.encode(
        {
            "sub": "1",
            "role": "admin",
            "exp": now + timedelta(minutes=5),
        },
        "a-completely-different-secret-of-sufficient-length",
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(forged)


def test_garbage_string_raises_invalid_token_error() -> None:
    """10. 完全瞎写的字符串 → InvalidTokenError。"""
    with pytest.raises(InvalidTokenError):
        decode_access_token("this.is.not.a.jwt")


def test_token_missing_role_raises_invalid_token_error() -> None:
    """11. 签名有效但缺 role 的 token → InvalidTokenError（手工构造）。"""
    settings = get_settings()
    now = datetime.now(UTC)
    incomplete = jwt.encode(
        {"sub": "1", "exp": now + timedelta(minutes=5)},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(incomplete)


def test_token_missing_sub_raises_invalid_token_error() -> None:
    """11b. 缺 sub 的 token 同样无效。"""
    settings = get_settings()
    now = datetime.now(UTC)
    incomplete = jwt.encode(
        {"role": "viewer", "exp": now + timedelta(minutes=5)},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(incomplete)


def test_exp_matches_requested_expires_minutes() -> None:
    """12. expires_minutes=5 时 exp ≈ now + 300（允许数秒执行偏差）。"""
    before = int(datetime.now(UTC).timestamp())
    token = create_access_token(subject="7", role="viewer", expires_minutes=5)
    payload = decode_access_token(token)
    after = int(datetime.now(UTC).timestamp())
    assert before + 300 <= payload.exp <= after + 300


def test_expires_minutes_defaults_to_settings() -> None:
    """12b. 不传 expires_minutes 时回退到 settings.jwt_expire_minutes。"""
    default_minutes = get_settings().jwt_expire_minutes
    before = int(datetime.now(UTC).timestamp())
    payload = decode_access_token(create_access_token(subject="9", role="admin"))
    after = int(datetime.now(UTC).timestamp())
    assert before + default_minutes * 60 <= payload.exp <= after + default_minutes * 60
