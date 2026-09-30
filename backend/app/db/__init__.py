"""数据库层：声明式基类、会话工厂、模型注册。

`Base.metadata` 只包含**已被导入**的模型。Alembic 的 `env.py` 会在读取
metadata 前调用 `import_all_models()`，确保迁移能看到全部表。
"""

from app.db.base import Base, TimestampMixin
from app.db.session import get_engine, get_session_factory, reset_engine

__all__ = [
    "Base",
    "TimestampMixin",
    "get_engine",
    "get_session_factory",
    "import_all_models",
    "reset_engine",
]


def import_all_models() -> None:
    """导入全部模型模块，把表注册进 `Base.metadata`。

    新增模型包时必须在此登记，否则 Alembic 会漏表。
    """
    # 各领域模型模块（随 EP 推进逐步登记）
    from app.audit import models as _audit_models  # noqa: F401
    from app.auth import models as _auth_models  # noqa: F401
    from app.datasets import models as _datasets_models  # noqa: F401
    from app.feedback import models as _feedback_models  # noqa: F401
    from app.judge import models as _judge_models  # noqa: F401
    from app.params import models as _params_models  # noqa: F401
    from app.results import models as _results_models  # noqa: F401
    from app.runs import models as _runs_models  # noqa: F401
