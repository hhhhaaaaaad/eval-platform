// run 状态的徽标：状态值 → 中文标签 + 配色。状态值来自后端 RunStatus 字面量。
// 颜色遵循「绿=成功 / 红=失败 / 蓝=进行中 / 黄=排队 / 灰=取消与未知」的直觉。

import type { CSSProperties } from 'react';

interface StatusMeta {
  label: string;
  color: string;
  background: string;
}

const STATUS_META: Record<string, StatusMeta> = {
  pending: { label: '排队中', color: '#92400e', background: '#fef3c7' },
  running: { label: '运行中', color: '#1d4ed8', background: '#dbeafe' },
  succeeded: { label: '成功', color: '#166534', background: '#dcfce7' },
  failed: { label: '失败', color: '#991b1b', background: '#fee2e2' },
  cancelled: { label: '已取消', color: '#475569', background: '#e2e8f0' },
};

const FALLBACK_META: StatusMeta = { label: '未知', color: '#475569', background: '#e2e8f0' };

// 状态 → 中文标签。用于计数概览等处（不依赖徽标配色）。
export function statusLabel(status: string): string {
  return STATUS_META[status]?.label ?? status;
}

export default function StatusBadge({ status }: { status: string }) {
  const meta = STATUS_META[status] ?? FALLBACK_META;
  const style: CSSProperties = {
    display: 'inline-block',
    padding: '2px 8px',
    borderRadius: 999,
    fontSize: 12,
    color: meta.color,
    background: meta.background,
    whiteSpace: 'nowrap',
  };
  return <span style={style}>{meta.label}</span>;
}
