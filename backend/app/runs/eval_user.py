"""从 ``config_fingerprint`` 派生 ``eval_user_id``（EP-6 的 per-config 命名空间）。

**为什么需要它**：并行 A/B 两个配置时，两个 run 都要往 AgentWrite 里 seed 同一份语料。
若它们共用同一个 ``eval_user_id``，后跑的那个会 ``reset`` 掉前一个刚 seed 的记忆，
两个 run 互相破坏，指标全废。所以**每个配置指纹独占一个评测命名空间**。

派生必须是**纯函数**——同一个 fingerprint 在任何进程、任何时候都得到同一个
``eval_user_id``。用随机分配或数据库发号都不行：那会让「同一配置换个进程跑」
落到不同命名空间，历史 run 与语料对不上。

.. warning::

   本模块的派生规则与 ``uq_runs_active_cfg`` / ``uq_runs_active_user`` 两个
   partial unique index 叠加后，**两条约束在语义上重叠**：既然 eval_user_id 由
   fingerprint 唯一决定，「同一 eval_user_id 至多一个活跃 run」就蕴含了
   「同一 fingerprint 至多一个活跃 run」。两个索引因此是**纵深防御**（任一失效
   另一个仍能挡住重复提交），而不是两条彼此独立的业务规则。若将来把
   eval_user_id 改为按 (fingerprint, 数据集) 或随机分配，两条约束才真正解耦——
   届时需要重新审视这段说明。
"""

from __future__ import annotations

from app.settings.config import Settings, get_settings

#: 从 fingerprint 摘要里取多少位十六进制做取模。16 位十六进制 = 64 bit，
#: 对 1e6 的区间而言分布足够均匀，且碰撞（两个配置落到同一命名空间）
#: 的概率远低于「同一配置跑两次」的实际风险。
_TAKE_HEX_CHARS = 16


class EvalUserIdRangeError(ValueError):
    """配置的命名空间区间非法。"""


def derive_eval_user_id(config_fingerprint: str, settings: Settings | None = None) -> int:
    """把配置指纹映射到 AgentWrite 评测命名空间内的一个稳定 id。

    取值落在 ``[base, base + range)``——这是 Java 侧 ``validateEvalUserId`` 的
    白名单区间，落在区间外一律以 E0403 拒绝（IDOR 防护），所以越界不是「差一点」
    而是直接不可用。

    算法：取指纹摘要的十六进制前缀转整数后对 ``range`` 取模，再加 ``base``。
    用摘要而非原始字符串，是因为指纹本身已带 ``sha256:`` 前缀且长度固定，
    直接参与取模会因前缀恒定而损失分布质量。
    """
    settings = settings or get_settings()

    base = settings.eval_user_id_base
    span = settings.eval_user_id_range
    if span <= 0:
        raise EvalUserIdRangeError(f"eval_user_id_range 必须为正数，实际: {span}")
    if base < 0:
        raise EvalUserIdRangeError(f"eval_user_id_base 不能为负数，实际: {base}")

    # 指纹形如 "sha256:<hex>"；只取摘要部分参与取模。
    digest = config_fingerprint.split(":", 1)[-1]
    prefix = digest[:_TAKE_HEX_CHARS]
    if len(prefix) < _TAKE_HEX_CHARS:
        raise EvalUserIdRangeError(
            f"config_fingerprint 摘要长度不足（需 ≥{_TAKE_HEX_CHARS} 位十六进制）: {config_fingerprint!r}"
        )

    try:
        bucket = int(prefix, 16)
    except ValueError as exc:
        raise EvalUserIdRangeError(f"config_fingerprint 摘要不是十六进制: {config_fingerprint!r}") from exc

    return base + bucket % span


def eval_namespace_bounds(settings: Settings | None = None) -> tuple[int, int]:
    """返回命名空间区间 ``[下界, 上界)``，供校验与测试使用。"""
    settings = settings or get_settings()
    return settings.eval_user_id_base, settings.eval_user_id_base + settings.eval_user_id_range


def is_in_eval_namespace(eval_user_id: int, settings: Settings | None = None) -> bool:
    """判断某 id 是否落在评测命名空间内（与 Java 侧同口径）。"""
    lower, upper = eval_namespace_bounds(settings)
    return lower <= eval_user_id < upper
