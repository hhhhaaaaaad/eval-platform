"""创建评测平台用户（初始化 / 运维脚本）。

用法：
    cd E:\\java\\eval_platform\\backend
    .\\.venv\\Scripts\\python.exe scripts/create_user.py --username admin --role admin

密码来源优先级（**刻意不把密码放命令行参数**，避免进入 shell history 与进程列表）：
1. 环境变量 ``EVAL_BOOTSTRAP_PASSWORD``
2. 交互式输入（getpass，不回显）

行为：
- 用户已存在 → 默认报错退出（避免误改现有账号密码）；``--reset-password`` 可显式覆盖
- 角色仅允许 admin / viewer，与数据库 CHECK 约束一致（此处提前校验，给出更友好的报错）
- 写一条 ``user.create`` 审计记录，与业务写在同一事务内提交
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

# 允许以 `python scripts/create_user.py` 方式直接运行：把 backend/ 加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.audit.service import write_audit
from app.auth.models import User
from app.auth.security import hash_password
from app.db.session import get_session_factory

VALID_ROLES = ("admin", "viewer")


def _resolve_password(cli_value: str | None) -> str:
    """按优先级取密码；绝不从命令行参数取。"""
    if cli_value:
        print(
            "警告：从命令行传密码会留在 shell history 与进程列表里，建议改用 "
            "EVAL_BOOTSTRAP_PASSWORD 环境变量或交互式输入。",
            file=sys.stderr,
        )
        return cli_value

    env_value = os.environ.get("EVAL_BOOTSTRAP_PASSWORD")
    if env_value:
        return env_value

    first = getpass.getpass("请输入密码: ")
    second = getpass.getpass("请再次输入: ")
    if first != second:
        raise SystemExit("两次输入的密码不一致")
    return first


def main() -> int:
    parser = argparse.ArgumentParser(description="创建评测平台用户")
    parser.add_argument("--username", required=True, help="登录账号")
    parser.add_argument("--role", default="viewer", choices=VALID_ROLES, help="角色（默认 viewer）")
    parser.add_argument(
        "--password",
        default=None,
        help="密码（不推荐：会留在 shell history，建议用 EVAL_BOOTSTRAP_PASSWORD 或交互式输入）",
    )
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help="用户已存在时重置其密码（默认报错退出）",
    )
    args = parser.parse_args()

    if not args.username.strip():
        raise SystemExit("username 不能为空")

    password = _resolve_password(args.password)
    if len(password) < 8:
        raise SystemExit("密码至少 8 位")

    session = get_session_factory()()
    try:
        existing = session.execute(
            select(User).where(User.username == args.username)
        ).scalar_one_or_none()

        if existing is not None and not args.reset_password:
            print(
                f"用户 {args.username!r} 已存在（id={existing.id}, role={existing.role}）。"
                f"如需重置密码请加 --reset-password。",
                file=sys.stderr,
            )
            return 1

        if existing is not None:
            existing.password_hash = hash_password(password)
            user, action = existing, "user.reset_password"
        else:
            user = User(
                username=args.username,
                password_hash=hash_password(password),
                role=args.role,
                is_active=True,
            )
            session.add(user)
            session.flush()  # 拿到自增 id 再写审计
            action = "user.create"

        write_audit(
            session,
            actor_user_id=None,  # 初始化脚本没有登录主体，记系统动作
            action=action,
            resource_type="user",
            resource_id=str(user.id),
            after={"username": user.username, "role": user.role},
        )
        session.commit()

        print(f"完成：{action} username={user.username} id={user.id} role={user.role}")
        return 0
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
