# EP-2 验收记录：认证、RBAC 与审计

> 对应《记忆系统独立评测平台 — 独立平台项目工作》§4 的 EP-2。
> 记录日期：2026-09-30。执行环境：Windows 11，Python 3.12.7，FastAPI 0.142，PyJWT 2.15，bcrypt 5.0。

## 1. 交付物

| 文件 | 说明 |
|---|---|
| `backend/app/auth/security.py` | bcrypt 哈希/校验、JWT 签发/解析、`InvalidTokenError` |
| `backend/app/auth/schemas.py` | `TokenPayload` / `LoginRequest` / `LoginResponse` |
| `backend/app/auth/deps.py` | `get_db` / `get_current_user` / `require_role` / `AdminUser` |
| `backend/app/auth/api.py` | `POST /api/v1/auth/login` |
| `backend/app/audit/service.py` | `write_audit()`——append-only 审计写入 |
| `backend/scripts/create_user.py` | 用户初始化/运维脚本 |
| `backend/app/main.py` | 挂载 auth 路由 |
| 4 个测试文件 | `test_auth_security.py`(15) / `test_audit_service.py`(9) / `test_auth_api.py`(12) / `test_health.py`(3) |

## 2. 已验证项（真实命令与输出）

```
pytest -q                      -> 39 passed
ruff check app tests scripts alembic -> All checks passed!
alembic upgrade head --sql     -> CREATE TABLE 15（EP-1 未受影响）
```

### 关键安全性质（每条都有对应用例）

| 性质 | 验证方式 | 结果 |
|---|---|---|
| **角色以数据库为准，不信 token 声明** | token 写 `admin`、库里是 `viewer` → 断言 403 | ✅ |
| 用户名不可枚举 | 用户不存在 与 密码错误 返回**同一文案** + 都执行 bcrypt | ✅ |
| 停用用户的有效 token 立即失效 | `is_active=False` + 未过期 token → 401 | ✅ |
| 伪造 `sub`（非数字）不触发 500 | `sub="not-a-number"` → 401 | ✅ |
| 过期 / 篡改 / 异 secret 的 token 拒绝 | 三类用例 | ✅ |
| 无 `Authorization` 头 → 401 且带 `WWW-Authenticate` | HTTP 头断言 | ✅ |
| 审计与业务写同事务（不 commit） | mock 断言 `commit` 未被调用 | ✅ |
| 登录成功/失败 **都**写审计并提交 | 断言 `added` 中含 `auth.login` / `auth.login_failed` | ✅ |
| bcrypt 72 字节上限 | 100 字符密码 + 中文（按字符边界截断）往返一致 | ✅ |

### 集成测试手法

`test_auth_api.py` 用 **FastAPI `dependency_overrides`** 把 `get_db` 换成假 Session，
从而在**无 Postgres** 的环境下端到端验证「HTTP 请求 → 路由 → 认证 → 响应」全链路，
而非只做函数级单元测试。

## 3. 实施中的关键发现

### 3.1 bcrypt 5.0.0 对超长密码抛异常（非静默截断）

旧版 bcrypt 会静默截断到 72 字节；**5.0.0 直接抛 `ValueError`**。
若不处理，超长密码会让登录接口 500。

修复：按 **UTF-8 字节**截断到 72，并 `decode("utf-8", errors="ignore")`
保证不切断多字节字符；`verify_password` 捕获 `(ValueError, TypeError)` 返回 `False`
（非法 hash 串不应抛异常）。

### 3.2 默认 JWT 密钥长度不足

默认值 `"dev-only-change-me"` 仅 18 字节，低于 HS256 推荐的 32 字节，
PyJWT 每次签发/解析都发 `InsecureKeyLengthWarning`（RFC 7518 §3.2）。

修复：默认值改为 60 字节占位串，并注明生产必须用环境变量覆盖。
已用 `warnings.simplefilter("error")` 验证警告消失。

### 3.3 模块导入期做 bcrypt（自行发现）

`api.py` 初版在模块级计算防时序攻击用的假 hash，导致每次 import 都付约 100ms 的 bcrypt 成本。
改为惰性生成（首次登录失败路径才计算）。

## 4. 未验证项（诚实标注）

| 项 | 原因 |
|---|---|
| 真实用户查库、`eval_users` 读写 | 本机无 Postgres，测试用假 Session |
| 审计行真实落库（JSONB、自增 id、FK） | 同上 |
| 「业务回滚时审计一并回滚」 | 由「同事务不 commit」的设计推出，未端到端验证 |
| `scripts/create_user.py` 实跑 | 需要真实数据库 |
| 密码策略强度（长度/复杂度） | 当前仅校验 ≥8 位，未做复杂度要求 |

## 5. 数据库可用后应立即执行

```bash
cd E:\java\eval_platform\backend
.\.venv\Scripts\python.exe -m alembic upgrade head
# 建一个管理员（密码走环境变量，避免进 shell history）
EVAL_BOOTSTRAP_PASSWORD=... .\.venv\Scripts\python.exe scripts/create_user.py --username admin --role admin
# 起服务后验证登录
curl -X POST http://localhost:8093/api/v1/auth/login -H "Content-Type: application/json" \
     -d '{"username":"admin","password":"..."}'
# 确认审计行落库
psql ... -c "SELECT action, resource_type, actor_user_id FROM eval_audit_log ORDER BY id DESC LIMIT 5;"
```

## 6. 下一步（EP-3）

Java connector 与契约测试：httpx 客户端、超时/重试/限流/熔断、9 类 DTO、
统一错误映射（403/409/422/5xx）、respx mock 契约测试。

**注意**：EP-3 依赖 AgentWrite 的真实 eval 端点契约，而该契约已在 Java 侧冻结
（13 个端点，均有契约测试锁定）。respx mock 可在**无 Java 实例**的前提下完成大部分验证，
但端到端联通仍需 Java eval 实例运行（需 `MEMORY_EMBEDDING_API_KEY` 等凭据）。
