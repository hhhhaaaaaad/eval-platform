"""五维指标引擎的各维度实现（EP-9）。

每个维度一个模块，各自负责「把该维度的原始观测转成 case 级结果与维度级指标」。
共享的指标公式都在 :mod:`app.engine.metrics`，此处只做维度特有的对齐与聚合。

已实现：

- :mod:`app.engine.dimensions.retrieval` —— 维度② 检索质量

维度①③④⑤（抽取 / 一致性 / 注入 / 治理）尚未实现；其中 ③ 一致性无需人工标注，
靠离线巡检扫库统计，与其余三个的实现路径不同。
"""

from app.engine.dimensions.retrieval import (
    MATCH_BY_CONTENT,
    MATCH_BY_ID,
    MATCH_NONE,
    RetrievalCaseResult,
    RetrievalEvaluator,
    RetrievedItem,
)

__all__ = [
    "MATCH_BY_CONTENT",
    "MATCH_BY_ID",
    "MATCH_NONE",
    "RetrievalCaseResult",
    "RetrievalEvaluator",
    "RetrievedItem",
]
