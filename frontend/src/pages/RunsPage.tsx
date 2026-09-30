// Run 列表页：状态计数概览 + 状态筛选 + 表格 + 手动刷新。
//
// 轮询策略：仅当列表里存在 running/pending 的 run 时才启动定时器（5s），
// 一旦全部进入终态（succeeded/failed/cancelled）定时器自动停止；
// 组件卸载时 cleanup 清掉定时器，切到别的页面不会泄漏。

import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiGet } from '../api/client';
import type { RunResponse } from '../api/types';
import { StatusBadge, statusLabel } from '../components';
import { describeError, formatDate, formatDuration, truncate } from '../lib/format';
import { buttonStyle, cardStyle, inputStyle, tableStyle, tdStyle, thStyle } from '../lib/styles';

// 计数概览的展示顺序：未知状态排到最后。
const STATUS_ORDER = ['pending', 'running', 'succeeded', 'failed', 'cancelled'];

async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // 剪贴板不可用（如非 https）时静默降级，不打断交互。
  }
}

export default function RunsPage() {
  const [statusFilter, setStatusFilter] = useState('');
  const [runs, setRuns] = useState<RunResponse[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const navigate = useNavigate();

  const load = useCallback(async () => {
    try {
      const query = statusFilter ? `?status=${encodeURIComponent(statusFilter)}` : '';
      // 列表与计数并行拉取，减少一次往返等待。
      const [runsData, countsData] = await Promise.all([
        apiGet<RunResponse[]>(`/runs${query}`),
        apiGet<Record<string, number>>('/runs/summary/counts'),
      ]);
      setRuns(runsData);
      setCounts(countsData);
      setError(null);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  // 有活跃 run 才轮询；hasActive 变化会自动重建/销毁定时器，天然防泄漏。
  const hasActive = runs.some((r) => r.status === 'running' || r.status === 'pending');
  useEffect(() => {
    if (!hasActive) {
      return;
    }
    const timer = window.setInterval(() => {
      void load();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [hasActive, load]);

  const knownStatuses = STATUS_ORDER.filter((s) => counts[s] !== undefined);
  const unknownStatuses = Object.keys(counts).filter((s) => !STATUS_ORDER.includes(s));

  return (
    <div>
      <h2>Run 列表</h2>

      {/* 状态计数概览 */}
      <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', marginBottom: 16 }}>
        {[...knownStatuses, ...unknownStatuses].map((s) => (
          <span key={s} style={{ fontSize: 13 }}>
            {statusLabel(s)}：<strong>{counts[s]}</strong>
          </span>
        ))}
        {loading && Object.keys(counts).length === 0 && <span>计数加载中…</span>}
      </div>

      {/* 筛选 + 刷新 */}
      <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginBottom: 16 }}>
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          style={inputStyle}
        >
          <option value="">全部状态</option>
          {STATUS_ORDER.map((s) => (
            <option key={s} value={s}>
              {statusLabel(s)}
            </option>
          ))}
        </select>
        <button onClick={() => void load()} style={buttonStyle}>
          刷新
        </button>
        {hasActive && <span style={{ fontSize: 12, color: '#666' }}>存在活跃 run，自动每 5s 刷新</span>}
      </div>

      {error && <p style={{ color: '#c0392b' }}>加载失败：{error}</p>}

      <div style={cardStyle}>
        {loading ? (
          <p>正在加载…</p>
        ) : runs.length === 0 ? (
          <p>暂无 run。</p>
        ) : (
          <table style={tableStyle}>
            <thead>
              <tr>
                <th style={thStyle}>ID</th>
                <th style={thStyle}>状态</th>
                <th style={thStyle}>mode</th>
                <th style={thStyle}>当前阶段</th>
                <th style={thStyle}>创建时间</th>
                <th style={thStyle}>耗时</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr
                  key={run.id}
                  onClick={() => navigate(`/runs/${run.id}`)}
                  style={{ cursor: 'pointer' }}
                >
                  <td style={tdStyle}>
                    <code title={run.id}>{truncate(run.id)}</code>{' '}
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        void copyText(run.id);
                      }}
                      style={{ ...buttonStyle, padding: '0 6px', fontSize: 12 }}
                      title="复制完整 ID"
                    >
                      复制
                    </button>
                  </td>
                  <td style={tdStyle}>
                    <StatusBadge status={run.status} />
                  </td>
                  <td style={tdStyle}>{run.mode}</td>
                  <td style={tdStyle}>{run.current_stage ?? '—'}</td>
                  <td style={tdStyle}>{formatDate(run.created_at)}</td>
                  <td style={tdStyle}>{formatDuration(run.started_at, run.finished_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
