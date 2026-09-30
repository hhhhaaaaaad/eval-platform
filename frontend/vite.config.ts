import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Vite 配置。
// 为什么用 proxy：开发期前端跑在 5173，后端在 8093，跨端口直连会触发浏览器 CORS；
// 通过代理把 /api 前缀转发到后端，浏览器视角下变为同源请求，省去后端 CORS 配置。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        // 用 127.0.0.1 而非 localhost：Windows 下 Node 会把 localhost 优先解析成 IPv6 ::1，
        // 而后端只监听 IPv4（0.0.0.0:8093），导致代理连接被拒（ECONNREFUSED ::1:8093）。
        target: 'http://127.0.0.1:8093',
        changeOrigin: true,
      },
    },
  },
});
