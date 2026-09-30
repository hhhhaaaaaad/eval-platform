"""参数快照哈希与 ``config_fingerprint`` 组合（EP-5）。

**为什么需要 fingerprint**：评测最怕的不是指标低，而是「指标不可比」。
两次 run 的 Recall 差 3 个点，是因为改了融合权重（真提升）还是因为换了 embedding 模型
（换了赛道）？没有 fingerprint 就无法回答，所有跨 run 对比都是空谈。

因此 ``config_fingerprint`` 必须覆盖**全部会改变指标语义的输入**，四类缺一不可：

============  ==========================  ==================================
输入           来源                        变化后意味着
============  ==========================  ==================================
参数           param_snapshot.params_hash  融合权重、注入预算变了
模型           model_version.config_hash   embedding / reranker 换了
ground truth   dataset_version.           标注变了，答案标准变了
              content_digest
mode           run.mode（exact / hnsw）     检索算法换了，召回率不可直接比
============  ==========================  ==================================

**为什么用组成部分的哈希而不是原始 JSON**：组成部分已是稳定摘要，拼装结果同样稳定，
且不必把三份大对象（尤其 params）都搬进来。代价是必须保证各部分哈希算法稳定——
这由 :mod:`app.datasets.digest` 的 ``canonical_json`` 统一保证。

**跨环境稳定性**来自 canonical_json 的四条约束（见其 docstring）。同一份输入的
fingerprint 在本地、CI、生产必须逐字相同，否则「同配置的另一台机器上跑一次做对照」
这件事根本不成立。
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.datasets.digest import canonical_json

#: 参与 params_hash 的默认键集合——即 AgentWrite ``/api/v1/eval/params`` 返回的全部字段。
#: 它们是检索与注入的语义参数，任何一个变化都会改变指标含义。
DEFAULT_FROZEN_KEYS: tuple[str, ...] = (
    "vector_store",
    "rrf_k",
    "alpha",
    "beta",
    "recency_half_life_days",
    "profile_boost",
    "min_confidence",
    "inject_max_tokens",
)

#: run 的检索模式，进 fingerprint（见模块 docstring 表格）。
RETRIEVAL_MODES: tuple[str, ...] = ("exact", "hnsw")


class FrozenKeyError(ValueError):
    """``frozen_keys`` 里出现了参数中不存在的键。

    单列异常类型而不是抛裸 ``KeyError``：调用方（API 层）要把它映射成 422
    ——请求内容不合法，重试无用。裸 ``KeyError`` 会被当成未预期异常返回 500，
    把「用户拼错了键名」误报成服务端故障。
    """


def _digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()}"


def params_hash(params: dict[str, Any], frozen_keys: list[str] | None = None) -> str:
    """参数快照哈希。

    ``frozen_keys`` 决定哪些键参与哈希（默认全部）。**键列表本身也进哈希**——
    否则「把某个键移出冻结集」不会改变 hash，而它其实改变了指纹语义，
    会让两次本来不可比的 run 撞到同一个 params_hash。

    ``frozen_keys`` 里出现 params 中不存在的键时**报错而非跳过**：静默跳过意味着
    一个拼错的键名会让该参数永远不参与指纹，是典型的「看起来在冻结、其实没冻」。
    """
    keys = sorted(frozen_keys if frozen_keys is not None else DEFAULT_FROZEN_KEYS)

    missing = [key for key in keys if key not in params]
    if missing:
        raise FrozenKeyError(
            f"冻结键在参数中不存在: {missing}（拼错的键名会让该参数静默不参与指纹）"
        )

    material = {
        "frozen_keys": keys,
        "values": {key: params[key] for key in keys},
    }
    return _digest(material)


def model_config_hash(
    embedding_model_id: str,
    reranker_model_id: str,
    config: dict[str, Any] | None = None,
) -> str:
    """模型版本配置哈希：``(embedding, reranker, config)`` 三元组。"""
    return _digest(
        {
            "embedding_model_id": embedding_model_id,
            "reranker_model_id": reranker_model_id,
            "config": config or {},
        }
    )


def config_fingerprint(
    *,
    params_digest: str,
    model_digest: str,
    dataset_digest: str,
    mode: str,
) -> str:
    """组合出 ``config_fingerprint``。

    入参刻意用**已计算好的四个摘要**而非对象：这样调用方必须显式拿出四项，
    少一项就编译不过（而不是拿到一个「只哈希了参数」的错误指纹）。

    ``mode`` 做白名单校验：拼错的 mode（如 ``"HNSW"``）若不拦，会生成一个
    与 ``"hnsw"`` 不同的指纹，把同一份配置拆成两个不可比的分组。
    """
    if mode not in RETRIEVAL_MODES:
        raise ValueError(f"mode 必须是 {RETRIEVAL_MODES} 之一，实际: {mode!r}")

    return _digest(
        {
            "params_hash": params_digest,
            "model_config_hash": model_digest,
            "dataset_content_digest": dataset_digest,
            "mode": mode,
        }
    )


def fingerprint_from_components(
    *,
    params: dict[str, Any],
    embedding_model_id: str,
    reranker_model_id: str,
    model_config: dict[str, Any] | None,
    dataset_content_digest: str,
    mode: str,
    frozen_keys: list[str] | None = None,
) -> str:
    """便捷入口：从原始组成部分一路算到 fingerprint。

    适合「已知全部配置、想直接得到指纹」的场景（如 API 预览端点）；
    run 落库路径应改用 :func:`config_fingerprint` 走存下来的三个摘要，
    保证指纹基于**库里那份**配置而非请求里的临时值。
    """
    return config_fingerprint(
        params_digest=params_hash(params, frozen_keys),
        model_digest=model_config_hash(embedding_model_id, reranker_model_id, model_config),
        dataset_digest=dataset_content_digest,
        mode=mode,
    )
