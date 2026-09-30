// 极简登录页：用户名 + 密码 + 错误提示。
// 为什么不做「记住我」等：平台本地仅一个管理员账号，登录态已持久化在 localStorage，
// 额外的持久化选项没有实际价值。

import type { FormEvent } from 'react';
import { useState } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import { login } from '../api/auth';
import { isAuthenticated } from '../api/session';
import { describeError } from '../lib/format';
import { buttonStyle, cardStyle, inputStyle } from '../lib/styles';

export default function LoginPage() {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();

  // 已登录时访问 /login 直接回主页面，避免重复登录。
  if (isAuthenticated()) {
    return <Navigate to="/runs" replace />;
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) {
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      await login(username, password);
      // 登录成功后回跳来源页（路由守卫记录的 from），否则进 Run 列表。
      const from = (location.state as { from?: { pathname?: string } } | null)?.from?.pathname;
      navigate(from ?? '/runs', { replace: true });
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div style={{ maxWidth: 360, margin: '48px auto' }}>
      <div style={cardStyle}>
        <h2 style={{ marginTop: 0 }}>登录</h2>
        <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            用户名
            <input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              style={inputStyle}
              autoFocus
            />
          </label>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            密码
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              style={inputStyle}
            />
          </label>
          {error && <p style={{ color: '#c0392b', margin: 0 }}>{error}</p>}
          <button type="submit" disabled={submitting || !username || !password} style={buttonStyle}>
            {submitting ? '登录中…' : '登录'}
          </button>
        </form>
      </div>
    </div>
  );
}
