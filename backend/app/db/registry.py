"""模型注册表：把全部 ORM 模型导入，使 ``Base.metadata`` 完整。

**为什么单独成模块**：``import_all_models`` 原本只被 Alembic 的 ``env.py`` 调用，
于是只有迁移路径能得到完整 metadata；运行时若某个进程只导入了部分模型
（例如 Celery worker 只 import ``app.datasets``），访问带外键的模型会抛
``NoReferencedTableError``——SQLAlchemy 解析 FK 时需要被引用表已注册。

现在由 :func:`app.db.session.get_session_factory` 在创建会话工厂前调用，
覆盖所有 ORM 使用路径。放在 ``app/db/__init__.py`` 会与 ``session.py`` 形成循环导入，
故独立成模块：``__init__`` 与 ``session`` 都从这里取。
"""

from __future__ import annotations

_registered = False


def import_all_models() -> None:
    """导入全部模型模块，把表注册进 ``Base.metadata``。

    幂等：Python 的模块缓存保证重复调用不会重复执行模块体，这里再加一个标志
    省掉函数调用与属性查找的开销（会话工厂会频繁调用）。

    新增模型包时**必须在此登记**，否则 Alembic 会漏表，运行时 FK 也会解析失败。
    """
    global _registered
    if _registered:
        return

    from app.audit import models as _audit_models  # noqa: F401
    from app.auth import models as _auth_models  # noqa: F401
    from app.datasets import models as _datasets_models  # noqa: F401
    from app.feedback import models as _feedback_models  # noqa: F401
    from app.judge import models as _judge_models  # noqa: F401
    from app.params import models as _params_models  # noqa: F401
    from app.results import models as _results_models  # noqa: F401
    from app.runs import models as _runs_models  # noqa: F401

    _registered = True
