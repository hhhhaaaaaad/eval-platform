# 记忆系统独立评测平台（Memory Eval Platform）

> **当前进度：核心链路已端到端跑通。** 数据集 / run 编排 / 租约状态机 / 四维指标 /
> 结果查询 API 均已实现，且已在**真实 AgentWrite 上**跑出指标（非 mock）。
> 尚未实现：维度③一致性、结果可视化前端、judge、反哺闭环、备份脚本。
> 见文末「进度与路线图」。

## 项目定位

本平台是面向「记忆系统」的**独立评测工具系统**，与业务系统 AgentWrite 解耦部署。
它负责：

- **管理评测集**：组织评测样本、版本化数据集；
- **编排评测 run**：按参数组合发起评测任务，异步执行、记录过程；
- **计算五维指标**：对评测结果做量化打分（检索 / 注入 / 治理 / 抽取 已实现，
  一致性巡检待实现）；
- **展示结果**：以图表 / 对比视图呈现 run 之间的差异；
- **反哺数据集**：把结论回写为新的评测样本与标注，形成闭环。

它**不实现**记忆的抽取、检索与治理 —— 那些能力属于 AgentWrite，本平台只做评测。

## 与 AgentWrite 的关系

- 通过 **HTTP** 调用 AgentWrite 暴露的 `/api/v1/eval/**`（共 13 个端点）触发记忆评测。
- **绝不直连 AgentWrite 的数据库**（其 MySQL / Redis / Qdrant 均不可访问）。
- 存储**物理隔离**：平台使用自己的 Postgres 与 Redis，端口刻意错开，本地并行开发互不污染。
- 调用 AgentWrite 端点需要携带具备 `ROLE_EVAL` 权限的 JWT（见 `JAVA_EVAL_TOKEN`）。

## 目录结构

```
eval_platform/
├── backend/                 # FastAPI + Celery + Alembic 后端
│   ├── app/
│   │   ├── api/             # HTTP 路由层
│   │   ├── auth/            # 认证 / 授权
│   │   ├── datasets/        # 数据集管理
│   │   ├── runs/            # 评测 run 编排
│   │   ├── engine/          # 评测执行引擎
│   │   ├── judge/           # 评审 / 打分
│   │   ├── results/         # 结果与指标
│   │   ├── feedback/        # 结果反哺数据集
│   │   ├── connector/       # 与 AgentWrite 的 HTTP 连接器
│   │   ├── params/          # 参数定义
│   │   ├── audit/           # 审计
│   │   ├── observability/   # 可观测性（日志 / 指标）
│   │   ├── tasks/           # Celery 任务与 celery_app
│   │   └── settings/        # 配置与日志基础设施
│   ├── alembic/             # 数据库迁移
│   ├── tests/               # 单测
│   └── pyproject.toml
├── frontend/                # Vite + React + ECharts 前端
├── deploy/                  # 部署编排
│   ├── docker-compose.yml   # Postgres / Redis / API / worker / beat / flower
│   ├── env.example          # 环境变量模板（复制为 .env）
│   └── backup/              # 备份目录（脚本在 EP-14 实现）
└── docs/                    # 设计与方案文档
```

## 本地开发

### 1. 后端环境

```bash
cd backend
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# Git Bash:          source .venv/Scripts/activate
pip install -e ".[dev]"
```

### 2. 准备环境变量

```bash
cp deploy/env.example deploy/.env
```

`deploy/.env` 已被 `.gitignore` 忽略；按需修改其中的连接串与密钥
（**切勿提交真实密钥**；`deploy/env.example` 是模板，保持占位值）。

### 3. 启动基础设施（Postgres + Redis）

```bash
cd deploy
docker compose up -d
```

### 4. 启动应用

```bash
# API（默认 8093）
cd backend
uvicorn app.main:app --host 0.0.0.0 --port 8093 --reload

# Celery worker（本地并发数 1）
celery -A app.tasks.celery_app:celery_app worker --loglevel=info --concurrency=1

# Celery beat（周期任务）
celery -A app.tasks.celery_app:celery_app beat --loglevel=info
```

> 也可整体容器化启动：`cd deploy && docker compose up -d` 会一并拉起 API / worker / beat / flower。

### 5. 健康检查

```bash
curl http://localhost:8093/api/v1/health   # 存活探针
curl http://localhost:8093/api/v1/ready    # 就绪探针（依赖 Postgres / Redis）
```

Flower 监控面板：<http://localhost:5555>（默认账号 `eval` / `eval`）。

## 端口对照与隔离

| 组件 | 平台端口 | AgentWrite 端口 | 说明 |
|---|---|---|---|
| Postgres / MySQL | **15432** | 13306（MySQL） | 平台元数据，物理隔离 |
| Redis | **16380** | 16379 | Celery broker + backend，物理隔离 |
| API (FastAPI) | **8093** | — | |
| Flower | **5555** | — | Celery 监控 |
| Java eval | — | 8092 | 平台通过 HTTP 调用 |
| Qdrant | — | 6333 | AgentWrite 向量库，平台不访问 |
| 前端 (Vite dev) | **5173** | — | 开发期 |

所有端口刻意错开，平台与 AgentWrite 可在本机并行运行而互不冲突。

## 进度与路线图

**已完成（EP-0 骨架）**

- 后端包结构与配置基础设施（`app/settings`）；
- 依赖声明（`backend/pyproject.toml`）与 Alembic 迁移目录；
- 本地编排（`deploy/docker-compose.yml`）与环境变量模板（`deploy/env.example`）；
- 前端工程骨架。

**尚未实现（请勿据此使用）**

- 数据集 / run / 指标 / judge / 反馈等业务模块仅有目录占位，**无实际逻辑**；
- `app.main:app`、`app.tasks.celery_app:celery_app` 等入口由后端模块阶段补齐；
- 健康检查端点 `/api/v1/health`、`/api/v1/ready` 待实现；
- 数据备份脚本（计划 EP-14）。
