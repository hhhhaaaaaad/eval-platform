// 单维度的指标雷达图。
//
// 关于「指标值域差异大」的处理（本任务的关键点）：
// 同一维度的指标里，计数型（case_count / *_count / *_total，值域从个位到上千）
// 与比率型（*_rate / *_at_k / mrr / utilization，值域 0..1）天然混在一起。
// 若把两者画进同一张雷达图，计数型会把坐标轴撑到很大，比率型的 0.x 差异
// 会被压缩成贴着圆心的一条线，一眼看不出差别。
//
// 因此本图**只画比率型指标（0..1）**，计数型指标排除出图、仍保留在结果表格里。
// 判定口径用指标名（含 count/total 结尾即计数型）而非数值大小——因为 case_count
// 也可能恰好等于 1，仅凭数值无法区分。比率型都落在 [0,1]，轴最大值固定为 1，
// 这样各比率指标在同一尺度上可比；若未来出现超过 1 的比率（如超预算的利用率），
// 轴会自动扩大到该值以兜底不截断。

import ReactECharts from 'echarts-for-react';
import type { EChartsOption } from 'echarts-for-react';
import { dimensionLabel, metricLabel } from '../lib/labels';

// 计数型指标名判定：以 count/total 结尾视为计数型。
function isCountMetric(name: string): boolean {
  return /(count|total)$/i.test(name);
}

interface Props {
  dimension: string;
  metrics: Record<string, number>;
}

export default function DimensionChart({ dimension, metrics }: Props) {
  // 按指标名排序，保证雷达轴顺序稳定（后端 metrics 是 dict，JSON 对象键序不可靠）。
  const entries = Object.entries(metrics)
    .filter(([name]) => !isCountMetric(name))
    .sort(([a], [b]) => a.localeCompare(b));

  if (entries.length === 0) {
    // 该维度只有计数型指标，没有可画的比率指标，不渲染空图。
    return null;
  }

  const values = entries.map(([, v]) => (typeof v === 'number' && Number.isFinite(v) ? v : 0));
  const rawMax = Math.max(1, ...values);
  // 比率型 ≤1 时轴最大为 1；一旦有值超过 1，轴扩大到该值（向上取整到 0.1），避免截断。
  const axisMax = rawMax <= 1 ? 1 : Math.ceil(rawMax * 10) / 10;

  const option: EChartsOption = {
    tooltip: { trigger: 'item' },
    radar: {
      indicator: entries.map(([name]) => ({ name: metricLabel(name), max: axisMax })),
      radius: '68%',
      axisName: { color: '#333' },
      splitArea: { areaStyle: { color: ['#fff', '#f5f7fa'] } },
    },
    series: [
      {
        type: 'radar',
        data: [{ value: values, name: dimensionLabel(dimension) }],
        symbol: 'circle',
        symbolSize: 6,
        lineStyle: { color: '#3b82f6' },
        itemStyle: { color: '#3b82f6' },
        areaStyle: { opacity: 0.2, color: '#3b82f6' },
      },
    ],
  };

  return <ReactECharts option={option} style={{ height: 260, width: '100%' }} />;
}
