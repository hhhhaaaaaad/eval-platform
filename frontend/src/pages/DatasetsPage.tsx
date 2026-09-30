// 数据集列表页：只读展示 GET /datasets 的返回。写操作（新建/归档/导入版本）
// 属 admin 功能，不在本期前端范围，故仅做列表。

import { useEffect, useState } from 'react';
import { apiGet } from '../api/client';
import type { DatasetResponse } from '../api/types';
import { describeError, formatDate } from '../lib/format';
import { cardStyle, tableStyle, tdStyle, thStyle } from '../lib/styles';

export default function DatasetsPage() {
  const [datasets, setDatasets] = useState<DatasetResponse[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const data = await apiGet<DatasetResponse[]>('/datasets');
        if (!cancelled) {
          setDatasets(data);
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
      <h2>数据集</h2>
      {error && <p style={{ color: '#c0392b' }}>加载失败：{error}</p>}
      <div style={cardStyle}>
        {loading ? (
          <p>正在加载…</p>
        ) : datasets.length === 0 ? (
          <p>暂无数据集。</p>
        ) : (
          <table style={tableStyle}>
            <thead>
              <tr>
                <th style={thStyle}>ID</th>
                <th style={thStyle}>名称</th>
                <th style={thStyle}>描述</th>
                <th style={thStyle}>创建时间</th>
                <th style={thStyle}>已归档</th>
              </tr>
            </thead>
            <tbody>
              {datasets.map((d) => (
                <tr key={d.id}>
                  <td style={tdStyle}>{d.id}</td>
                  <td style={tdStyle}>{d.name}</td>
                  <td style={tdStyle}>{d.description ?? '—'}</td>
                  <td style={tdStyle}>{formatDate(d.created_at)}</td>
                  <td style={tdStyle}>{d.is_archived ? '是' : '否'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
