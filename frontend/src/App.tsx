import type { CSSProperties } from 'react';
import { Navigate, NavLink, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { getCurrentUser, logout } from './api/auth';
import RequireAuth from './auth/RequireAuth';
import DatasetsPage from './pages/DatasetsPage';
import HealthPage from './pages/HealthPage';
import LoginPage from './pages/LoginPage';
import ParamsPage from './pages/ParamsPage';
import RunDetailPage from './pages/RunDetailPage';
import RunsPage from './pages/RunsPage';

// 侧边导航项。/health 是公开探针，业务路由需登录（由 RequireAuth 守卫）。
const navItems: ReadonlyArray<{ path: string; label: string }> = [
  { path: '/health', label: '健康检查' },
  { path: '/datasets', label: '数据集' },
  { path: '/params', label: '参数' },
  { path: '/runs', label: 'Run' },
  { path: '/results', label: '结果' },
  { path: '/feedback', label: '反馈' },
];

// 占位组件：尚未实现的业务路由统一渲染它，避免为空路由白屏。
function Placeholder({ title }: { title: string }) {
  return (
    <div>
      <h2>{title}</h2>
      <p>该功能将在后续迭代逐步实现。</p>
    </div>
  );
}

const headerStyle: CSSProperties = {
  padding: '12px 20px',
  borderBottom: '1px solid #e0e0e0',
  fontWeight: 600,
  fontSize: 18,
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'center',
};

const navStyle: CSSProperties = {
  width: 160,
  padding: 12,
  borderRight: '1px solid #e0e0e0',
  display: 'flex',
  flexDirection: 'column',
  gap: 8,
};

const mainStyle: CSSProperties = { flex: 1, padding: 20 };

const linkStyle = ({ isActive }: { isActive: boolean }): CSSProperties => ({
  padding: '6px 10px',
  borderRadius: 4,
  textDecoration: 'none',
  color: isActive ? '#fff' : '#333',
  background: isActive ? '#3b82f6' : 'transparent',
});

export default function App() {
  const navigate = useNavigate();
  // 订阅路由变化：登录/登出后导航会触发重渲染，头部据此刷新登录态展示。
  useLocation();
  const user = getCurrentUser();

  function handleLogout() {
    logout();
    navigate('/login', { replace: true });
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>
      <header style={headerStyle}>
        <span>记忆系统评测平台</span>
        {user && (
          <span style={{ fontSize: 13, fontWeight: 400, display: 'flex', gap: 12, alignItems: 'center' }}>
            <span>
              已登录：{user.username}（{user.role}）
            </span>
            <button
              onClick={handleLogout}
              style={{ padding: '4px 10px', border: '1px solid #ccc', borderRadius: 6, background: '#fff', cursor: 'pointer' }}
            >
              退出
            </button>
          </span>
        )}
      </header>
      <div style={{ display: 'flex', flex: 1 }}>
        <nav style={navStyle}>
          {navItems.map((item) => (
            <NavLink key={item.path} to={item.path} style={linkStyle}>
              {item.label}
            </NavLink>
          ))}
        </nav>
        <main style={mainStyle}>
          <Routes>
            <Route path="/" element={<Navigate to="/runs" replace />} />
            <Route path="/login" element={<LoginPage />} />
            <Route path="/health" element={<HealthPage />} />
            <Route
              path="/datasets"
              element={
                <RequireAuth>
                  <DatasetsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/params"
              element={
                <RequireAuth>
                  <ParamsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/runs"
              element={
                <RequireAuth>
                  <RunsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/runs/:runId"
              element={
                <RequireAuth>
                  <RunDetailPage />
                </RequireAuth>
              }
            />
            <Route
              path="/results"
              element={
                <RequireAuth>
                  <Placeholder title="评测结果" />
                </RequireAuth>
              }
            />
            <Route
              path="/feedback"
              element={
                <RequireAuth>
                  <Placeholder title="反馈" />
                </RequireAuth>
              }
            />
            <Route path="*" element={<Placeholder title="页面不存在" />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
