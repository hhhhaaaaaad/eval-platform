import { useEffect, useState } from 'react';
import { ApiError, apiGet } from '../api/client';
import type { HealthResponse, ReadyResponse } from '../api/types';

// 页面状态机：用可辨识联合表达「加载中 / 失败 / 成功」，避免多个布尔标志互相打架。
// ready 单独判空：/ready 可能返回 503，此时 health 仍应正常展示，故把 ready 失败降级为局部错误信息。
type LoadState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'success'; health: HealthResponse; ready: ReadyResponse | null; readyError: string | null };

// 把任意抛出物转成可展示文案，兼容 ApiError / 原生 Error / 其它未知值。
function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    return err.message;
  }
  if (err instanceof Error) {
    return err.message;
  }
  return String(err);
}

export default function HealthPage() {
  const [state, setState] = useState<LoadState>({ kind: 'loading' });

  useEffect(() => {
    // cancelled 用于组件卸载后阻止 setState，避免「更新已卸载组件」告警。
    let cancelled = false;

    async function load() {
      try {
        const health = await apiGet<HealthResponse>('/health');

        let ready: ReadyResponse | null = null;
        let readyError: string | null = null;
        try {
          ready = await apiGet<ReadyResponse>('/ready');
        } catch (err) {
          // /ready 失败（常见为 503 未就绪）不阻断主信息展示，仅记录原因。
          readyError = describeError(err);
        }

        if (!cancelled) {
          setState({ kind: 'success', health, ready, readyError });
        }
      } catch (err) {
        if (!cancelled) {
          setState({ kind: 'error', message: describeError(err) });
        }
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  if (state.kind === 'loading') {
    return <p>正在加载健康状态…</p>;
  }

  if (state.kind === 'error') {
    return (
      <div>
        <h2>健康检查</h2>
        <p style={{ color: '#c0392b' }}>加载失败：{state.message}</p>
      </div>
    );
  }

  const { health, ready, readyError } = state;

  return (
    <div>
      <h2>健康检查</h2>
      <ul>
        <li>服务名：{health.service}</li>
        <li>环境：{health.environment}</li>
        <li>服务状态：{health.status}</li>
      </ul>

      <h3>就绪状态</h3>
      {ready ? (
        <>
          <p>整体：{ready.status === 'ready' ? '就绪' : '未就绪'}</p>
          <ul>
            <li>Postgres：{ready.checks.postgres.ok ? 'ok' : '不可用'}</li>
            <li>Redis：{ready.checks.redis.ok ? 'ok' : '不可用'}</li>
          </ul>
        </>
      ) : (
        <p style={{ color: '#c0392b' }}>就绪检查失败：{readyError}</p>
      )}
    </div>
  );
}
