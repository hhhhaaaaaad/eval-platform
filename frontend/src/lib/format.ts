// 展示层格式化工具：把后端原始值转成可读文案。
// 集中在这是为了多个页面（列表 / 详情 / 表格）口径一致。

import { ApiError } from '../api/client';

// 把任意抛出物转成可展示文案，兼容 ApiError / 原生 Error / 其它未知值。
export function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    return err.message;
  }
  if (err instanceof Error) {
    return err.message;
  }
  return String(err);
}

// ISO 时间串 → 本地可读时间；空值/非法值降级为占位符，不抛异常。
export function formatDate(iso: string | null | undefined): string {
  if (!iso) {
    return '—';
  }
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) {
    return iso;
  }
  return d.toLocaleString();
}

// 两个 ISO 时间戳之间的耗时，形如「3m 12s」；缺任一端返回占位符。
export function formatDuration(
  startIso: string | null | undefined,
  endIso: string | null | undefined,
): string {
  if (!startIso || !endIso) {
    return '—';
  }
  const start = new Date(startIso).getTime();
  const end = new Date(endIso).getTime();
  if (Number.isNaN(start) || Number.isNaN(end)) {
    return '—';
  }
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  if (seconds < 60) {
    return `${seconds}s`;
  }
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${minutes}m ${rest}s`;
}

// 指标值展示：整数不带小数点，小数保留最多 4 位并去掉尾随 0；
// 非数值（理论上不会）原样 String 化兜底。
export function formatMetric(value: unknown): string {
  if (typeof value !== 'number') {
    return String(value);
  }
  if (!Number.isFinite(value)) {
    return String(value);
  }
  if (Number.isInteger(value)) {
    return String(value);
  }
  return value.toFixed(4).replace(/\.?0+$/, '');
}

// 长字符串（如 run id / fingerprint）截断成「头…尾」，用于表格里压缩显示。
export function truncate(s: string, head = 8, tail = 8): string {
  if (s.length <= head + tail + 1) {
    return s;
  }
  return `${s.slice(0, head)}…${s.slice(-tail)}`;
}
