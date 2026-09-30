"""哈希与摘要工具：canonical JSON、case content_hash、版本 content_digest、内容哈希。

这里是「同一份评测集在任何环境都得到同一个指纹」的唯一实现点。三个概念必须分清，
它们的用途完全不同，混用会导致指标不可复现或去重失效：

============  ==========================  ==========================================
名称           哈希对象                     用途
============  ==========================  ==========================================
content_hash  单条 case 的 (case_type,    版本内去重 + 唯一索引 ``(dataset_version_id,
              payload)                     content_hash)``
content_digest 整个版本的**全部** case   版本指纹，导入去重与审计追溯
              （含 ground_truth）
content_hash   单条记忆的 content 字符串    指标计算时把召回结果与 ground truth 对齐
（记忆级）      （md5）
============  ==========================  ==========================================

**为什么 content_digest 必须包含 ground_truth**：否则只改标注（不动输入）不会改变指纹，
平台会把「同一批 query、不同答案」的两个版本判为同一版，指标对比失去意义。
这是 EP-4 的验收条款之一。

**为什么记忆级用 md5 而不是 sha256**：它不承担任何安全职责，只做「两条记忆是不是同一句」
的等价判定；与 AgentWrite 侧 ``contentHash`` 保持同算法（同为 md5(UTF-8)）才能直接对账。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: 参与 case 指纹的字段。**顺序即语义**：改动这里等于改变所有历史 content_hash，
#: 属破坏性变更，需配合数据迁移。
_CASE_HASH_FIELDS = ("case_type", "payload")

#: 版本摘要里每条 case 贡献的字段。ground_truth 必须在列。
_DIGEST_CASE_FIELDS = ("case_type", "group_key", "payload", "ground_truth")

#: 支持的 case 来源类型（EP-4 的「三源样本支持」）。
CASE_TYPE_CONVERSATION_TO_MEMORY = "conversation_to_memory"
CASE_TYPE_QUERY_TO_MEMORY = "query_to_memory"
CASE_TYPE_GOVERNANCE = "governance"

CASE_TYPES = frozenset(
    {
        CASE_TYPE_CONVERSATION_TO_MEMORY,
        CASE_TYPE_QUERY_TO_MEMORY,
        CASE_TYPE_GOVERNANCE,
    }
)


def canonical_json(value: Any) -> str:
    """把任意 JSON 值序列化成**跨环境稳定**的字符串。

    稳定性来自四条约束，缺一条就会出现「本地和 CI 算出不同指纹」：

    - ``sort_keys=True``：Python dict 保插入序，不同来源构造的同义对象键序可能不同；
    - ``separators`` 去空白：``", "`` / ``": "`` 会随默认参数变化；
    - ``ensure_ascii=False``：中文若被转成 ``\\uXXXX``，不同库的转义大小写习惯不一致；
    - ``allow_nan=False``：``NaN``/``Infinity`` 不是合法 JSON，且各语言表示不同，
      更要紧的是 ``NaN != NaN``，含 NaN 的 payload 永远算不出稳定指纹——宁可报错。

    ``float`` 用 Python 的最短往返表示（``repr``），在受支持的版本间稳定。
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def case_content_hash(case_type: str, payload: dict[str, Any]) -> str:
    """单条 case 的 content_hash。

    把 ``case_type`` 纳入哈希：同一个 query 在「检索质量」与「注入质量」两个维度下
    是两条不同的 case（指标口径不同），若只哈希 payload 会被去重掉一条。

    ``group_key`` **不参与**：它只是分组统计标签，参与的话同一 case 换个标签就能重复导入，
    去重形同虚设。
    """
    # _CASE_HASH_FIELDS 与这里的取值一一对应；strict 让两者长度不一致时立刻报错，
    # 而不是静默漏掉一个字段（那会让所有历史 content_hash 悄然变化）。
    material = dict(zip(_CASE_HASH_FIELDS, (case_type, payload), strict=True))
    # 前缀算法名，使将来换算法时新旧值不会撞在一起，且一眼可辨。
    return f"sha256:{_sha256_hex(canonical_json(material))}"


def version_content_digest(cases: list[dict[str, Any]]) -> str:
    """整个版本的 content_digest。

    先按 ``content_hash`` **排序再哈希**，使 digest 与 case 的排列顺序无关——
    导入时按文件顺序还是按分组顺序处理，不应产出不同指纹。

    每条 case 贡献 :data:`_DIGEST_CASE_FIELDS` 全部字段，**含 ground_truth**。
    """
    normalized: list[dict[str, Any]] = []
    for case in cases:
        entry = {field: case.get(field) for field in _DIGEST_CASE_FIELDS}
        entry["content_hash"] = case["content_hash"]
        normalized.append(entry)

    normalized.sort(key=lambda item: item["content_hash"])
    return f"sha256:{_sha256_hex(canonical_json(normalized))}"


def memory_content_hash(content: str) -> str:
    """单条记忆内容的哈希，用于把召回结果与 ground truth 对齐。

    与 AgentWrite 侧 ``contentHash`` 保持 **md5(content UTF-8)** 同算法：平台的
    ground truth 可以直接与该值比对，不需要额外映射表。

    归一化只做「首尾空白剥离」这一步：评测要求的是**逐字匹配**，额外的
    大小写折叠 / 全半角转换会掩盖真实的抽取偏差（把「Java 17」和「java 17」
    判为同一条，会虚高 Recall），不是我们想要的宽容度。
    """
    return hashlib.md5(content.strip().encode("utf-8")).hexdigest()


def memory_content_hashes(contents: list[str]) -> list[str]:
    """批量内容哈希，保持输入顺序（顺序对 NDCG 有意义）。"""
    return [memory_content_hash(content) for content in contents]
