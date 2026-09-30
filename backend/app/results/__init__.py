"""结果持久化与查询。

- :mod:`app.results.models` —— ``eval_run_results``（维度级聚合）与
  ``eval_case_results``（逐 case 明细）两张表的 ORM 定义；
- :mod:`app.results.service` —— 幂等 upsert 写入入口。

指标计算本身在 :mod:`app.engine`，本包只负责「把算出来的东西存好、取出来」。
"""

from app.results.service import (
    DIMENSION_CONSISTENCY,
    DIMENSION_EXTRACTION,
    DIMENSION_GOVERNANCE,
    DIMENSION_INJECTION,
    DIMENSION_RETRIEVAL,
    ResultReader,
    ResultWriter,
)

__all__ = [
    "DIMENSION_CONSISTENCY",
    "DIMENSION_EXTRACTION",
    "DIMENSION_GOVERNANCE",
    "DIMENSION_INJECTION",
    "DIMENSION_RETRIEVAL",
    "ResultReader",
    "ResultWriter",
]
