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
        target: 'http://localhost:8093',
        changeOrigin: true,
      },
    },
  },
});
