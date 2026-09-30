"""认证底层原语：bcrypt 口令哈希与 JWT 签发/校验。

本模块只做「纯函数式」的安全计算，不依赖数据库、FastAPI、也不 import
``app.auth.models``——这样它可以被 API 层、任务 worker、测试独立复用，也便于
单独做单元测试。

两条通行原则：
* 口令侧只存 bcrypt 哈希，永远不落明文；
* Token 侧只放鉴权必需字段（sub/role/exp/iat），不放敏感业务数据。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.auth.schemas import TokenPayload
from app.settings.config import get_settings

# bcrypt 算法本身只取口令的前 72 字节参与运算（Blowfish 密钥长度上限）。
# 超过该长度的输入必须显式处理，否则行为会因后端实现而异：bcrypt<5 会静默
# 截断，bcrypt>=5 会直接抛 ValueError。我们统一在此截断，保证跨版本一致。
_BCRYPT_MAX_BYTES = 72


class InvalidTokenError(Exception):
    """Token 无法解析、签名不符、已过期或缺少必要声明时抛出。

    对外是统一的失败信号：API 层只需捕获这一个异常即可映射为 401，无需感知
    PyJWT 的具体异常类型（ExpiredSignatureError / DecodeError / ...）。
    """


def _truncate_for_bcrypt(plain: str) -> bytes:
    """把口令按 UTF-8 字节截断到 bcrypt 支持的 72 字节以内，返回 bytes。

    为什么不能直接 ``plain[:72]``：那是按「字符」截断，中文等多字节字符会超限；
    为什么截断后要 ``decode(errors="ignore")`` 再编码：直接切字节可能把一个多字节
    字符拦腰截断，产生非法 UTF-8 序列。先解码并忽略残缺尾部，再交给 bcrypt 编码，
    可确保截断点始终落在字符边界上、且结果确定可复现。
    """
    raw = plain.encode("utf-8")
    if len(raw) <= _BCRYPT_MAX_BYTES:
        return raw
    safe = raw[:_BCRYPT_MAX_BYTES].decode("utf-8", errors="ignore")
    return safe.encode("utf-8")


def hash_password(plain: str) -> str:
    """对明文口令做 bcrypt 哈希，返回可存库的字符串。

    每次调用都生成新盐（``gensalt``），因此同一口令两次哈希结果不同——这是防
    彩虹表的预期行为。
    """
    hashed = bcrypt.hashpw(_truncate_for_bcrypt(plain), bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文口令与 bcrypt 哈希是否匹配。

    任何异常（哈希串格式非法、盐无效、类型错误等）都吞掉并返回 ``False``。理由：
    库中若混入历史脏数据或非 bcrypt 格式的哈希，``bcrypt.checkpw`` 会抛
    ``ValueError``；若任由其冒泡，登录接口会直接 500 而非干净的「密码错误」。
    认证失败应当是普通的布尔结果，不该是服务器错误。
    """
    try:
        return bcrypt.checkpw(_truncate_for_bcrypt(plain), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(
    *,
    subject: str,
    role: str,
    expires_minutes: int | None = None,
) -> str:
    """签发一个 HS256 的 JWT，返回紧凑字符串（`header.payload.signature`）。

    ``subject`` 强制为 ``str``：JWT 规范要求 ``sub`` 是字符串，且部分校验库会
    据此做类型断言，用 int 会在下游解析时报错。``expires_minutes`` 为 ``None``
    时回退到全局配置的 ``jwt_expire_minutes``。

    载荷只含 sub/role/exp/iat 四项——token 是签名而非加密的，塞入任何敏感字段
    等于明文广播。
    """
    settings = get_settings()
    minutes = settings.jwt_expire_minutes if expires_minutes is None else expires_minutes

    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "sub": str(subject),
        "role": role,
        "exp": now + timedelta(minutes=minutes),
        "iat": now,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> TokenPayload:
    """校验并解析 JWT，成功返回 ``TokenPayload``，任何失败抛 ``InvalidTokenError``。

    失败被归一化为单一异常类型：签名不符、已过期（``ExpiredSignatureError`` 是
    ``PyJWTError`` 子类）、结构损坏、以及「签名有效但缺 sub/role/exp 声明」都归为
    认证失败，调用方无需逐一区分。
    """
    settings = get_settings()
    try:
        raw = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.PyJWTError as exc:  # 含 ExpiredSignatureError / DecodeError / InvalidSignatureError
        raise InvalidTokenError("token 校验失败") from exc

    # 签名通过不代表字段齐全（例如手工构造或密钥泄露后被裁剪的 token），
    # 缺任一必需声明都视为无效，避免下游拿到 None 字段继续执行。
    missing = [name for name in ("sub", "role", "exp") if name not in raw]
    if missing:
        raise InvalidTokenError(f"token 缺少必要字段: {', '.join(missing)}")

    try:
        return TokenPayload(sub=str(raw["sub"]), role=str(raw["role"]), exp=int(raw["exp"]))
    except (TypeError, ValueError) as exc:
        raise InvalidTokenError("token 字段类型非法") from exc
