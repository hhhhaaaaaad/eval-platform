import type { CSSProperties } from 'react';
import { Navigate, NavLink, Route, Routes } from 'react-router-dom';
import HealthPage from './pages/HealthPage';

// 侧边导航项。/health 已可用，其余业务路由在 EP-0 仅作占位。
const navItems: ReadonlyArray<{ path: string; label: string }> = [
  { path: '/health', label: '健康检查' },
  { path: '/datasets', label: '数据集' },
  { path: '/params', label: '参数' },
  { path: '/runs', label: 'Run' },
  { path: '/results', label: '结果' },
  { path: '/feedback', label: '反馈' },
];

// 占位组件：所有尚未实现的业务路由统一渲染它，避免为空路由白屏。
function Placeholder({ title }: { title: string }) {
  return (
    <div>
      <h2>{title}</h2>
      <p>该功能将在 EP-1 起逐步实现。</p>
    </div>
  );
}

const headerStyle: CSSProperties = {
  padding: '12px 20px',
  borderBottom: '1px solid #e0e0e0',
  fontWeight: 600,
  fontSize: 18,
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
  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>
      <header style={headerStyle}>记忆系统评测平台</header>
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
            <Route path="/" element={<Navigate to="/health" replace />} />
            <Route path="/health" element={<HealthPage />} />
            <Route path="/datasets" element={<Placeholder title="数据集管理" />} />
            <Route path="/params" element={<Placeholder title="参数快照" />} />
            <Route path="/runs" element={<Placeholder title="Run 管理" />} />
            <Route path="/results" element={<Placeholder title="评测结果" />} />
            <Route path="/feedback" element={<Placeholder title="反馈" />} />
            <Route path="*" element={<Placeholder title="页面不存在" />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
