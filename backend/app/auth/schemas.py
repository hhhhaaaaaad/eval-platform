"""认证相关 Pydantic 数据契约。

把这些模型集中在一处，是为了让「进入/离开认证层的 JSON 长什么样」有唯一权威
定义：API 层（登录接口）、安全层（JWT 载荷解析）与后续的权限依赖都复用同一
批模型，避免各写各的字段名导致前后端契约漂移。

全部用 ``BaseModel``（纯校验、非 ORM），因为这里只描述传输结构，不持久化。
"""

from __future__ import annotations

from pydantic import BaseModel


class TokenPayload(BaseModel):
    """JWT 解码后的载荷。

    只承载鉴权必需的三要素：``sub``（用户 id）、``role``（角色）、``exp``（过期
    时间戳）。刻意不放用户名、邮箱等可识别信息——JWT 仅签名不加密，载荷对持有
    者明文可见，放越多个人信息泄露面越大。
    """

    sub: str  # user id（字符串形式；JWT 规范要求 sub 为字符串）
    role: str  # "admin" | "viewer"
    exp: int  # Unix 时间戳（秒）


class LoginRequest(BaseModel):
    """客户端登录请求体。"""

    username: str
    password: str


class LoginResponse(BaseModel):
    """登录成功响应。

    ``expires_in`` 用「剩余秒数」而非绝对时间戳：客户端无需与服务端对表即可据此
    计算本地过期时刻，避免机器时钟偏差引发的误判。
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int  # 秒
    user_id: int
    username: str
    role: str
