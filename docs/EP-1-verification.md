# EP-1 验收记录：Postgres schema 与 Alembic 迁移

> 对应《记忆系统独立评测平台 — 独立平台项目工作》§4 的 EP-1。
> 记录日期：2026-09-30。执行环境：Windows 11，Python 3.12.7，SQLAlchemy 2.1.1，Alembic 1.20.0。

## 1. 交付物

| 文件 | 说明 |
|---|---|
| `backend/app/db/base.py` | `Base` + 命名约定 + `TimestampMixin` |
| `backend/app/db/session.py` | 惰性引擎/会话工厂 |
| `backend/app/db/__init__.py` | `import_all_models()`——Alembic 读取 metadata 前必须调用 |
| `backend/app/auth/models.py` | `eval_users` |
| `backend/app/datasets/models.py` | `eval_datasets` / `eval_dataset_versions` / `eval_cases` |
| `backend/app/params/models.py` | `eval_param_snapshots` / `eval_model_versions` |
| `backend/app/runs/models.py` | `eval_experiments` / `eval_runs` / `eval_run_guard` |
| `backend/app/results/models.py` | `eval_run_results` / `eval_case_results` |
| `backend/app/feedback/models.py` | `eval_feedback_samples` |
| `backend/app/judge/models.py` | `eval_judge_jobs` |
| `backend/app/audit/models.py` | `eval_audit_log` |
| `backend/alembic/env.py` | 绑定 `Base.metadata`，调用 `import_all_models()` |
| `backend/alembic/versions/0001_initial_schema.py` | 首次迁移：14 张表 + 索引 + guard 单行初始化 |

共 14 张表，与方案 §5 逐表对应。

## 2. 已验证项（真实命令与输出）

| 验证项 | 命令 | 结果 |
|---|---|---|
| 全量模型注册 | `import_all_models()` + `Base.metadata.tables` | **14 张表**，表名与 §5 一致 |
| 离线 DDL 生成 | `alembic upgrade head --sql` | **exit 0**；`CREATE TABLE` ×15（14 业务表 + `alembic_version`） |
| 主键类型 | DDL 文本 | `BIGSERIAL` ×11（11 张 BIGSERIAL 表 + 2 张 UUID PK + 1 张 INTEGER PK） |
| 时间/JSON 类型 | DDL 文本 | `TIMESTAMP WITH TIME ZONE` ×21、`JSONB` ×17 |
| 三个 partial unique index | DDL 文本 | 名字与 WHERE 条件**逐字**匹配方案 |
| 两个 partial 普通 index | DDL 文本 | `idx_runs_heartbeat` / `idx_runs_pending` |
| `eval_runs` 索引总数 | DDL 文本 | **8 个**，全部名字与方案一致 |
| guard 单行初始化 | DDL 文本 | `INSERT INTO eval_run_guard (id, exclusive_owner) VALUES (1, NULL)` |
| 结果表 upsert 唯一键 | DDL 文本 | `uq_run_dimension_metric` / `uq_run_case_dimension` |
| **模型 ↔ 迁移一致性** | 脚本比对三类约束名 | **FK 20=20、UQ 7=7、CHECK 6=6，全部一致** |
| 回归测试 | `pytest -q` | **3 passed**（EP-0 用例未受影响） |

### 三个核心并发约束（逐字核对）

```sql
CREATE UNIQUE INDEX uq_runs_idempotency  ON eval_runs (idempotency_key)    WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX uq_runs_active_cfg   ON eval_runs (config_fingerprint) WHERE status IN ('pending','running');
CREATE UNIQUE INDEX uq_runs_active_user  ON eval_runs (eval_user_id)       WHERE status IN ('pending','running');
```

## 3. 实施中发现并修复的 3 个缺陷

三者**都只在 SQLAlchemy 真正编译 DDL 时才暴露**——metadata 层检查（表名、列名、约束对象是否存在）
完全看不到它们。这正是「必须跑起来才算验证」的例证。

### 3.1 外键标识符超长（PostgreSQL 63 字符上限）

```
IdentifierError: Identifier
'fk_eval_dataset_versions_parent_version_id_eval_dataset_versions'
exceeds maximum length of 63 characters
```

命名约定 `fk_<表>_<列>_<被引用表>` 在长表名上超限。两处会踩：自引用 FK（64 字符）、
`fk_eval_feedback_samples_archived_dataset_version_id_eval_dataset_versions`（78 字符）。

**修复**：约定改为 `fk_<表>_<列>`（去掉被引用表），一次性解决而非逐个起短名。
改后最长标识符 52 字符，留有余量。迁移中的 20 个 FK 名同步重命名。

### 3.2 CHECK 约束名双重前缀

```
CONSTRAINT ck_eval_run_guard_ck_eval_run_guard_single_row CHECK (id = 1)
```

**根因**：Alembic 的 `op.create_table` 使用 `target_metadata` 的命名约定
（`Operations.metadata` 返回的就是 `target_metadata`）。而 CHECK 模板
`ck_%(table_name)s_%(constraint_name)s` **引用了 `constraint_name`**，
所以在迁移里写全名会被再加一次前缀。

对比：FK 模板不含 `%(constraint_name)s`，因此显式名被保留、不会双重前缀——
这解释了为什么只有 CHECK 出问题。

**修复**：迁移中的 CHECK 改为短名（`role_valid` 等），由约定补 `ck_<表>_` 前缀，
与模型侧声明对齐。

### 3.3 结果表唯一约束名不一致

模型侧为 `uq_run_dimension_metric` / `uq_run_case_dimension`（有意短名，它们是 upsert 的
`ON CONFLICT` 冲突目标），而迁移里写成了约定生成的长名。

**修复**：以模型为准修改迁移。若不一致，将来 `alembic check` 会报漂移。

## 4. 未验证项（诚实标注）

| 项 | 原因 |
|---|---|
| `alembic upgrade head` 真实建表 | **本机无 Postgres**（无 psql、无服务、15432/5432 无监听），Docker 因磁盘满不可用 |
| `alembic check`（模型↔迁移漂移检测） | 需要数据库连接 |
| partial unique index 真能阻止并发插入 | 需真实 Postgres 并发测试 |
| `BIGSERIAL` 序列、`gen_random_uuid()` 的实际行为 | 需真实 PG（PG13+ 内置 `gen_random_uuid`） |
| `server_default` 的回读确认 | 同上 |

**结论**：EP-1 的 DDL 已通过「编译期 + 模型/迁移一致性」双重校验，
但**尚未在真实 Postgres 上执行过**。EP-1 的出口条件「schema 可从空库一键创建」
与「约束能阻止并发」需等数据库可用后补验。

## 5. 数据库可用后应立即执行

```bash
cd E:\java\eval_platform\backend
.\.venv\Scripts\python.exe -m alembic upgrade head     # 建表
.\.venv\Scripts\python.exe -m alembic check            # 模型↔迁移漂移检测，应无差异
.\.venv\Scripts\python.exe -m alembic downgrade base   # 验证 downgrade 可回滚
```

## 6. 下一步（EP-2）

认证、RBAC 与审计：JWT 登录、bcrypt 密码、admin/viewer 两档权限、mutation 审计日志、
当前用户依赖注入。前置条件：数据库可用（当前受阻）。
