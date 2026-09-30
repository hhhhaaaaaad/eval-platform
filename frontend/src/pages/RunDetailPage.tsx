// Run 详情页（核心页面）：基本信息 + 按维度分组的结果（图表 + 表格）+ 逐 case 明细。
//
// 轮询：run 处于 running/pending 时每 5s 刷新基本信息与结果，进入终态后自动停止；
// runId 变化或组件卸载时 cleanup，不泄漏。结果区的「空结果」是正常态（还没跑完/失败），
// 后端返回 200 + dimensions: []，这里按提示文案展示而非当错误。

import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { apiGet, apiPost } from '../api/client';
import type {
  CaseResultResponse,
  RunCasesResponse,
  RunResponse,
  RunResultsResponse,
} from '../api/types';
import { DimensionChart } from '../charts';
import { StatusBadge } from '../components';
import { describeError, formatDate, formatDuration, formatMetric, truncate } from '../lib/format';
import { dimensionLabel, metricLabel, stageLabel } from '../lib/labels';
import { buttonStyle, cardStyle, inputStyle, tableStyle, tdStyle, thStyle } from '../lib/styles';

const CASE_LIMIT = 50;

async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // 剪贴板不可用时静默降级。
  }
}

// 把动态键的对象渲染成「key = value」行，键值集合随维度而变，这里不假设任何固定键。
function formatValue(v: unknown): string {
  if (typeof v === 'number') {
    return formatMetric(v);
  }
  if (typeof v === 'string') {
    return v.length > 60 ? `${v.slice(0, 60)}…` : v;
  }
  if (typeof v === 'boolean') {
    return String(v);
  }
  if (v === null) {
    return 'null';
  }
  const s = JSON.stringify(v);
  return s.length > 80 ? `${s.slice(0, 80)}…` : s;
}

function KeyValueList({ data }: { data: Record<string, unknown> }) {
  const entries = Object.entries(data);
  if (entries.length === 0) {
    return <span style={{ color: '#999' }}>—</span>;
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 12 }}>
      {entries.map(([k, v]) => (
        <span key={k}>
          <code>{k}</code> = {formatValue(v)}
        </span>
      ))}
    </div>
  );
}

export default function RunDetailPage() {
  const { runId } = useParams<{ runId: string }>();
  const navigate = useNavigate();

  const [run, setRun] = useState<RunResponse | null>(null);
  const [results, setResults] = useState<RunResultsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [cases, setCases] = useState<RunCasesResponse | null>(null);
  const [casesError, setCasesError] = useState<string | null>(null);
  const [caseDimension, setCaseDimension] = useState('');
  const [offset, setOffset] = useState(0);

  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);

  const loadRun = useCallback(async () => {
    if (!runId) {
      return;
    }
    try {
      const [runData, resultsData] = await Promise.all([
        apiGet<RunResponse>(`/runs/${runId}`),
        apiGet<RunResultsResponse>(`/runs/${runId}/results`),
      ]);
      setRun(runData);
      setResults(resultsData);
      setError(null);
    } catch (err) {
      setError(describeError(err));
    }
  }, [runId]);

  // runId 变化时重置所有页面态再加载，避免复用旧 run 的残留数据。
  useEffect(() => {
    setRun(null);
    setResults(null);
    setCases(null);
    setError(null);
    setCasesError(null);
    setCaseDimension('');
    setOffset(0);
    void loadRun();
  }, [loadRun]);

  const isActive = run?.status === 'running' || run?.status === 'pending';
  useEffect(() => {
    if (!isActive) {
      return;
    }
    const timer = window.setInterval(() => {
      void loadRun();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [isActive, loadRun]);

  const loadCases = useCallback(async () => {
    if (!runId) {
      return;
    }
    try {
      const params = new URLSearchParams();
      params.set('limit', String(CASE_LIMIT));
      params.set('offset', String(offset));
      if (caseDimension) {
        params.set('dimension', caseDimension);
      }
      const data = await apiGet<RunCasesResponse>(`/runs/${runId}/cases?${params.toString()}`);
      setCases(data);
      setCasesError(null);
    } catch (err) {
      setCasesError(describeError(err));
    }
  }, [runId, caseDimension, offset]);

  useEffect(() => {
    void loadCases();
  }, [loadCases]);

  async function handleCancel() {
    if (!runId || cancelling) {
      return;
    }
    setCancelling(true);
    setCancelError(null);
    try {
      const updated = await apiPost<RunResponse>(`/runs/${runId}/cancel`, {});
      setRun(updated);
    } catch (err) {
      setCancelError(describeError(err));
    } finally {
      setCancelling(false);
    }
  }

  if (error) {
    return (
      <div>
        <button onClick={() => navigate('/runs')} style={buttonStyle}>
          ← 返回列表
        </button>
        <p style={{ color: '#c0392b' }}>加载失败：{error}</p>
      </div>
    );
  }

  if (!run) {
    return <p>正在加载 run 详情…</p>;
  }

  const completedStages = run.checkpoint.completed_stages ?? [];
  const inProgressStage =
    run.current_stage && !completedStages.includes(run.current_stage) ? run.current_stage : null;

  const dimensions = results?.dimensions ?? [];

  const casesTotal = cases?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(casesTotal / CASE_LIMIT));
  const currentPage = Math.floor(offset / CASE_LIMIT) + 1;

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16 }}>
        <button onClick={() => navigate('/runs')} style={buttonStyle}>
          ← 返回列表
        </button>
        <h2 style={{ margin: 0 }}>
          <code title={run.id}>{truncate(run.id, 12, 12)}</code>
        </h2>
        <StatusBadge status={run.status} />
        <button
          onClick={(e) => {
            e.stopPropagation();
            void copyText(run.id);
          }}
          style={{ ...buttonStyle, fontSize: 12 }}
        >
          复制 ID
        </button>
      </div>

      {/* 基本信息 */}
      <div style={cardStyle}>
        <h3 style={{ marginTop: 0 }}>基本信息</h3>
        <table style={tableStyle}>
          <tbody>
            <tr>
              <td style={{ ...tdStyle, width: 160, color: '#666' }}>状态</td>
              <td style={tdStyle}>
                <StatusBadge status={run.status} />
              </td>
            </tr>
            <tr>
              <td style={{ ...tdStyle, color: '#666' }}>mode</td>
              <td style={tdStyle}>{run.mode}</td>
            </tr>
            <tr>
              <td style={{ ...tdStyle, color: '#666' }}>eval_user_id</td>
              <td style={tdStyle}>{run.eval_user_id}</td>
            </tr>
            <tr>
              <td style={{ ...tdStyle, color: '#666' }}>config_fingerprint</td>
              <td style={tdStyle}>
                <code title={run.config_fingerprint}>{truncate(run.config_fingerprint, 20, 20)}</code>{' '}
                <button
                  onClick={() => void copyText(run.config_fingerprint)}
                  style={{ ...buttonStyle, padding: '0 6px', fontSize: 12 }}
                >
                  复制
                </button>
              </td>
            </tr>
            <tr>
              <td style={{ ...tdStyle, color: '#666' }}>创建 / 开始 / 结束</td>
              <td style={tdStyle}>
                {formatDate(run.created_at)} / {formatDate(run.started_at)} / {formatDate(run.finished_at)}
              </td>
            </tr>
            <tr>
              <td style={{ ...tdStyle, color: '#666' }}>耗时</td>
              <td style={tdStyle}>{formatDuration(run.started_at, run.finished_at)}</td>
            </tr>
            {run.status === 'failed' && run.error_message && (
              <tr>
                <td style={{ ...tdStyle, color: '#666' }}>错误信息</td>
                <td style={tdStyle}>
                  <pre style={{ whiteSpace: 'pre-wrap', margin: 0, color: '#991b1b' }}>{run.error_message}</pre>
                </td>
              </tr>
            )}
            <tr>
              <td style={{ ...tdStyle, color: '#666' }}>执行进度</td>
              <td style={tdStyle}>
                {/* 已完成阶段用绿色标签，当前进行中的阶段用蓝色标签。 */}
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  {completedStages.map((stage) => (
                    <span
                      key={stage}
                      style={{
                        padding: '2px 8px',
                        borderRadius: 999,
                        fontSize: 12,
                        background: '#dcfce7',
                        color: '#166534',
                      }}
                    >
                      {stageLabel(stage)}
                    </span>
                  ))}
                  {inProgressStage && (
                    <span
                      style={{
                        padding: '2px 8px',
                        borderRadius: 999,
                        fontSize: 12,
                        background: '#dbeafe',
                        color: '#1d4ed8',
                      }}
                    >
                      {stageLabel(inProgressStage)}（进行中）
                    </span>
                  )}
                  {completedStages.length === 0 && !inProgressStage && <span style={{ color: '#999' }}>—</span>}
                </div>
              </td>
            </tr>
          </tbody>
        </table>

        {(run.status === 'pending' || run.status === 'running') && (
          <div style={{ marginTop: 12 }}>
            <button onClick={() => void handleCancel()} disabled={cancelling} style={buttonStyle}>
              {cancelling ? '取消中…' : '取消 run'}
            </button>
            {cancelError && <span style={{ color: '#c0392b', marginLeft: 8 }}>{cancelError}</span>}
          </div>
        )}
      </div>

      {/* 结果区 */}
      <div style={cardStyle}>
        <h3 style={{ marginTop: 0 }}>评测结果</h3>
        {!results ? (
          <p>正在加载结果…</p>
        ) : dimensions.length === 0 ? (
          <p>该 run 尚未产出结果（可能仍在执行或执行失败）。</p>
        ) : (
          dimensions.map(({ dimension, metrics }) => {
            const sortedMetrics = Object.entries(metrics).sort(([a], [b]) => a.localeCompare(b));
            return (
              <div key={dimension} style={{ border: '1px solid #eee', borderRadius: 8, padding: 12, marginBottom: 12 }}>
                <h4 style={{ marginTop: 0 }}>
                  {dimensionLabel(dimension)} <span style={{ fontWeight: 400, color: '#999' }}>({dimension})</span>
                </h4>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 16 }}>
                  <div style={{ flex: '0 1 340px', minWidth: 280 }}>
                    <DimensionChart dimension={dimension} metrics={metrics} />
                  </div>
                  <table style={{ ...tableStyle, flex: '1 1 320px', alignSelf: 'flex-start' }}>
                    <thead>
                      <tr>
                        <th style={thStyle}>指标</th>
                        <th style={thStyle}>说明</th>
                        <th style={thStyle}>值</th>
                      </tr>
                    </thead>
                    <tbody>
                      {sortedMetrics.map(([name, value]) => (
                        <tr key={name}>
                          <td style={tdStyle}>
                            <code>{name}</code>
                          </td>
                          <td style={tdStyle}>{metricLabel(name)}</td>
                          <td style={tdStyle}>{formatMetric(value)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            );
          })
        )}
      </div>

      {/* 逐 case 明细 */}
      <div style={cardStyle}>
        <h3 style={{ marginTop: 0 }}>逐 case 明细</h3>
        <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginBottom: 12 }}>
          <label style={{ fontSize: 13 }}>
            维度：
            <select
              value={caseDimension}
              onChange={(e) => {
                setCaseDimension(e.target.value);
                setOffset(0);
              }}
              style={inputStyle}
            >
              <option value="">全部</option>
              {dimensions.map(({ dimension }) => (
                <option key={dimension} value={dimension}>
                  {dimensionLabel(dimension)}
                </option>
              ))}
            </select>
          </label>
          <span style={{ fontSize: 12, color: '#666' }}>共 {casesTotal} 条</span>
        </div>

        {casesError && <p style={{ color: '#c0392b' }}>case 加载失败：{casesError}</p>}

        {!cases ? (
          <p>正在加载 case…</p>
        ) : cases.cases.length === 0 ? (
          <p>暂无 case 明细。</p>
        ) : (
          <table style={tableStyle}>
            <thead>
              <tr>
                <th style={thStyle}>case_id</th>
                <th style={thStyle}>维度</th>
                <th style={thStyle}>指标值</th>
                <th style={thStyle}>detail</th>
              </tr>
            </thead>
            <tbody>
              {cases.cases.map((c: CaseResultResponse) => (
                <tr key={`${c.case_id}-${c.dimension}`}>
                  <td style={tdStyle}>{c.case_id}</td>
                  <td style={tdStyle}>{dimensionLabel(c.dimension)}</td>
                  <td style={tdStyle}>
                    <KeyValueList data={c.metric_values} />
                  </td>
                  <td style={tdStyle}>
                    <KeyValueList data={c.detail} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginTop: 12 }}>
          <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - CASE_LIMIT))} style={buttonStyle}>
            上一页
          </button>
          <span style={{ fontSize: 13 }}>
            第 {currentPage} / {pageCount} 页
          </span>
          <button
            disabled={offset + CASE_LIMIT >= casesTotal}
            onClick={() => setOffset(offset + CASE_LIMIT)}
            style={buttonStyle}
          >
            下一页
          </button>
        </div>
      </div>
    </div>
  );
}
