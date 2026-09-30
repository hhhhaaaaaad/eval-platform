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


#: ``case_limit=None``（不限量）在指纹里的表示。
#: 不用 JSON 的 ``null``：拼进摘要的字面量越显眼，「这个 run 没限量」就越不容易
#: 被与「case_limit 忘了传」混为一谈。
_UNLIMITED = "unlimited"


def config_fingerprint(
    *,
    params_digest: str,
    model_digest: str,
    dataset_digest: str,
    mode: str,
    case_limit: int | None = None,
) -> str:
    """组合出 ``config_fingerprint``。

    入参刻意用**已计算好的摘要**而非对象：这样调用方必须显式拿出每一项，
    少一项就编译不过（而不是拿到一个「只哈希了参数」的错误指纹）。

    ``mode`` 做白名单校验：拼错的 mode（如 ``"HNSW"``）若不拦，会生成一个
    与 ``"hnsw"`` 不同的指纹，把同一份配置拆成两个不可比的分组。

    **``case_limit`` 必须参与指纹**，这不是洁癖，有两个具体后果：

    1. **可比性**：限量 10 条与跑满 100 条算出的是两个不同总体的指标
       （小样本的 Recall 波动天然更大）。同指纹会让趋势查询把它们并成一条曲线。
    2. **并发阻塞**：``uq_runs_active_cfg`` 限制「同一指纹同时只能有一个进行中的
       run」——不含 case_limit 时，一个 10 条的冒烟 run 会把同配置的正式跑批
       挡在门外，而这恰恰是最常见的操作顺序（先小样本试水、再跑全量）。
       含它之后两者落到不同的评测命名空间，互不阻塞。
    """
    if mode not in RETRIEVAL_MODES:
        raise ValueError(f"mode 必须是 {RETRIEVAL_MODES} 之一，实际: {mode!r}")
    if case_limit is not None and case_limit <= 0:
        # 0 或负数在语义上不是「不限量」而是调用方算错了。放行会生成一个
        # 实际一条 case 都跑不到、却与正常配置不同的指纹，极难排查。
        raise ValueError(f"case_limit 必须为正整数或 None，实际: {case_limit!r}")

    return _digest(
        {
            "params_hash": params_digest,
            "model_config_hash": model_digest,
            "dataset_content_digest": dataset_digest,
            "mode": mode,
            "case_limit": _UNLIMITED if case_limit is None else case_limit,
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
    case_limit: int | None = None,
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
        case_limit=case_limit,
    )
