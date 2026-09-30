// 参数页：参数快照 + 模型版本两个只读列表。都是 GET 端点，读放行 viewer。
// 快照的 params 是动态键，这里只展示哈希与名称，不展开明细以免表格过宽。

import { useEffect, useState } from 'react';
import { apiGet } from '../api/client';
import type { ModelVersionResponse, ParamSnapshotResponse } from '../api/types';
import { describeError, formatDate, truncate } from '../lib/format';
import { cardStyle, tableStyle, tdStyle, thStyle } from '../lib/styles';

export default function ParamsPage() {
  const [snapshots, setSnapshots] = useState<ParamSnapshotResponse[]>([]);
  const [models, setModels] = useState<ModelVersionResponse[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [snapData, modelData] = await Promise.all([
          apiGet<ParamSnapshotResponse[]>('/params/snapshots'),
          apiGet<ModelVersionResponse[]>('/params/model-versions'),
        ]);
        if (!cancelled) {
          setSnapshots(snapData);
          setModels(modelData);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setError(describeError(err));
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div>
      <h2>参数</h2>
      {error && <p style={{ color: '#c0392b' }}>加载失败：{error}</p>}

      <div style={cardStyle}>
        <h3 style={{ marginTop: 0 }}>参数快照</h3>
        {loading ? (
          <p>正在加载…</p>
        ) : snapshots.length === 0 ? (
          <p>暂无参数快照。</p>
        ) : (
          <table style={tableStyle}>
            <thead>
              <tr>
                <th style={thStyle}>ID</th>
                <th style={thStyle}>名称</th>
                <th style={thStyle}>params_hash</th>
                <th style={thStyle}>描述</th>
                <th style={thStyle}>创建时间</th>
              </tr>
            </thead>
            <tbody>
              {snapshots.map((s) => (
                <tr key={s.id}>
                  <td style={tdStyle}>{s.id}</td>
                  <td style={tdStyle}>{s.name}</td>
                  <td style={tdStyle}>
                    <code title={s.params_hash}>{truncate(s.params_hash, 12, 12)}</code>
                  </td>
                  <td style={tdStyle}>{s.description ?? '—'}</td>
                  <td style={tdStyle}>{formatDate(s.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div style={cardStyle}>
        <h3 style={{ marginTop: 0 }}>模型版本</h3>
        {loading ? (
          <p>正在加载…</p>
        ) : models.length === 0 ? (
          <p>暂无模型版本。</p>
        ) : (
          <table style={tableStyle}>
            <thead>
              <tr>
                <th style={thStyle}>ID</th>
                <th style={thStyle}>embedding_model_id</th>
                <th style={thStyle}>reranker_model_id</th>
                <th style={thStyle}>config_hash</th>
              </tr>
            </thead>
            <tbody>
              {models.map((m) => (
                <tr key={m.id}>
                  <td style={tdStyle}>{m.id}</td>
                  <td style={tdStyle}>{m.embedding_model_id}</td>
                  <td style={tdStyle}>{m.reranker_model_id}</td>
                  <td style={tdStyle}>
                    <code title={m.config_hash}>{truncate(m.config_hash, 12, 12)}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
