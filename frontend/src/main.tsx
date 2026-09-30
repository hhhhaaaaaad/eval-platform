import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';

// 入口：React 18 createRoot 挂载，外层包 BrowserRouter 以支持客户端路由。
const container = document.getElementById('root');

if (!container) {
  // 挂载点缺失属于不可恢复的环境错误，直接抛出便于定位，而不是静默白屏。
  throw new Error('未找到 #root 挂载点，请检查 index.html');
}

createRoot(container).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
