# EP-0 验收记录：项目骨架、依赖和本地环境

> 对应《记忆系统独立评测平台 — 独立平台项目工作》§4 的 EP-0。
> 记录日期：2026-09-30。执行环境：Windows 11，Python 3.12.7，Node v22.12.0。

## 1. 交付物

| 区域 | 文件 | 说明 |
|---|---|---|
| 后端 | `backend/pyproject.toml` | hatchling；12 运行时依赖 + 4 dev 依赖；pytest/ruff 配置 |
| 后端 | `backend/app/main.py` | FastAPI app 工厂 + lifespan |
| 后端 | `backend/app/settings/config.py` | pydantic-settings，`get_settings()` 单例 |
| 后端 | `backend/app/settings/logging.py` | JSON 日志 + request id 中间件 |
| 后端 | `backend/app/api/health.py` | `/health` 与 `/ready` |
| 后端 | `backend/app/tasks/celery_app.py` | Celery 实例 + `ping` 任务 |
| 后端 | `backend/alembic.ini` + `alembic/env.py` | 迁移脚手架（URL 取自 settings） |
| 后端 | `backend/tests/test_health.py` | 3 个 health 用例 |
| 后端 | `backend/Dockerfile` + `.dockerignore` | 镜像构建 |
| 前端 | `frontend/` 14 文件 | Vite + React + TS，路由骨架 + API client + 健康页 |
| 部署 | `deploy/docker-compose.yml` 等 5 文件 | 6 服务编排 + env 模板 + README + .gitignore |

## 2. 已验证项（有真实命令与输出）

| 验证项 | 命令 | 结果 |
|---|---|---|
| 后端单元测试 | `pytest -q` | **3 passed** |
| API 启动 | `uvicorn app.main:app` | `Application startup complete` |
| 健康检查 | `GET /api/v1/health` | **200** `{"status":"ok","service":"memory-eval-platform","environment":"local"}` |
| 就绪探针（依赖缺失） | `GET /api/v1/ready` | **503** + 逐项明细（Postgres 超时 / Redis 拒绝），符合预期 |
| request id 贯穿 | 响应头 + 日志比对 | `x-request-id` 与日志行中 `request_id` 一致 |
| 结构化日志 | 日志文件按 UTF-8 解码 | JSON 合法，中文正确渲染，无 `U+FFFD` |
| Celery 装配 | `celery_app.main` | `memory_eval_platform`；broker `redis://localhost:16380/0`；`acks_late=True`、`reject_on_worker_lost=True`；`ping` 任务已注册 |
| Alembic 配置 | `alembic heads` | 通过（`versions/` 为空，无 revision） |
| 前端类型检查 | `tsc --noEmit` | **0 errors** |

## 3. 未验证项（诚实标注）

| 项 | 原因 |
|---|---|
| `docker compose up` 全链路 | **本机 Docker 守护进程不可用**（C: 盘 99% / D: 盘 98% 满，虚拟磁盘被挂为只读） |
| Celery worker / beat 实际运行 | 需要 Redis（16380），同上未启动 |
| `/ready` 的 200 分支 | 需要 Postgres + Redis 同时可用 |
| `alembic upgrade` | 无 revision（EP-1 才建表），且需要 Postgres |

EP-0 原定出口「`docker compose up` 后 API health、worker health、Postgres、Redis 均可用」
**只完成了前半段**（API 与健康检查已实测），后半段受环境阻塞。

## 4. 实施中发现并修复的缺陷

这三处都是**只有把代码真正跑起来才会暴露**的问题，语法检查与静态审阅均无法发现。

### 4.1 日志中文乱码

`JsonFormatter` 用 `ensure_ascii=False` 保留可读中文，但 Windows 控制台按 GBK 编码输出，
中文被替换成乱码。

修复：`configure_logging` 中把 `sys.stdout` 与 `sys.stderr` 都 reconfigure 为 UTF-8，
并让 `StreamHandler` **显式绑定 `sys.stdout`**。

> 注意：`logging.StreamHandler()` 无参时写入的是 `sys.stderr` 而非 `stdout`。
> 只修 stdout 会让日志依然乱码——这是第一版修复失败的原因。

### 4.2 `alembic.ini` 含中文导致所有 alembic 命令崩溃

`configparser` 以系统 locale 编码（zh-CN Windows 为 GBK）读取 `.ini`，
文件里的 UTF-8 中文注释触发 `UnicodeDecodeError`，`alembic` 在解析配置阶段即失败。

修复：`alembic.ini` 改为**纯 ASCII**，并加注释说明必须保持 ASCII。

> 对比：`pyproject.toml` 的非 ASCII 内容**不是**问题——TOML 规范要求按 UTF-8 解码，
> 与 locale 无关。只有 `configparser` 读取的文件有这个约束。

### 4.3 缺失的构建件

- `backend/Dockerfile` 缺失 —— compose 的 `build: ../backend` 会直接失败
- `backend/.dockerignore` 缺失 —— 若无此文件，`COPY . .` 会把本地 `.venv`（100 MB+）打进镜像

## 5. 已知设计决策

**compose 内覆盖 `DATABASE_URL` / `REDIS_URL` 为容器服务名**（`eval-postgres:5432`、
`eval-redis:6379`）。原因：容器内的 `localhost` 无法指向兄弟容器；宿主可见端口仍严格为
15432 / 16380。`deploy/env.example` 保留 `localhost` 形态，供「宿主机直跑」场景使用。

## 6. 下一步（EP-1）

Postgres schema 与 Alembic 迁移：用户、数据集、版本、case、参数快照、模型版本、
experiment/run/guard、run result/case result、feedback/judge job/audit，
以及 partial unique index（active config / active user / idempotency）。

前置条件：需要 Postgres 可用（当前 Docker 受阻，可考虑本机直装或先释放磁盘）。
