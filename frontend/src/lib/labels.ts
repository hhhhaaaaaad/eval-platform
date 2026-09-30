// 维度 / 指标 / 阶段的中文标签映射。
// 为什么只覆盖已知项并回退原文：这些 key 由后端在运行期产出，前端无法枚举穷尽；
// 未知 key 直接显示原文，保证「新维度/新指标上线时前端不会白屏或显示空白」。

const DIMENSION_LABELS: Record<string, string> = {
  retrieval: '检索质量',
  injection: '注入效果',
  governance: '治理效果',
  extraction: '提取质量',
  // 一致性是对**整个命名空间**的一次巡检，不像另外四个那样逐 case；
  // 它衡量的是「系统自己把多少条记忆判为重复/冲突/过期残留」，与准确度无关。
  // 标签里点出「巡检」二字，避免看的人把它误当成又一个准确度指标。
  consistency: '一致性巡检',
};

const METRIC_LABELS: Record<string, string> = {
  case_count: '用例数',
  case_count_scored: '计分用例数',
  case_count_total: '总用例数',
  hit_at_1: '首位命中率',
  mrr: '平均倒数排名 (MRR)',
  ndcg_at_k: 'NDCG@k',
  precision_at_k: '精度 @k',
  recall_at_k: '召回 @k',
  reciprocal_rank: '倒数排名',
  token_utilization: 'Token 利用率',
  over_budget_rate: '超预算率',
  irrelevant_injection_rate: '无关注入率',
  // 一致性巡检（维度③）：都是「被系统判为某类问题的记忆占比」。
  // 注意它们衡量的是系统行为而非准确度——没有 ground truth 参与。
  duplicate_rate: '重复率',
  conflict_rate: '冲突率',
  consistency_rate: '一致率',
  expired_residue_rate: '过期残留率',
  quarantine_rate: '隔离率',
  duplicates_count: '重复条数',
  expired_count: '过期条数',
  hallucination_count: '幻觉条数',
  wrong_merge_rate: '误合并率',
  false_action_rate: '误动作率',
  missed_action_rate: '漏动作率',
  wrong_archive_rate: '误归档率',
  wrong_quarantine_rate: '误隔离率',
};

const STAGE_LABELS: Record<string, string> = {
  fencing: '互斥锁',
  reset: '重置',
  seed: '种子数据',
  vector_ready: '向量就绪',
  search: '检索',
  injection: '注入',
  governance: '治理',
  extraction: '提取',
  metrics: '指标计算',
  finalize: '收尾',
};

export function dimensionLabel(dimension: string): string {
  return DIMENSION_LABELS[dimension] ?? dimension;
}

export function metricLabel(metric: string): string {
  return METRIC_LABELS[metric] ?? metric;
}

export function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage;
}
