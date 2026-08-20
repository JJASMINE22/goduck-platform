import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath, URL } from 'node:url'

// 后端默认监听 0.0.0.0:8000（见 backend/app/config.py）。
// dev 通过 /api 前缀代理，避开跨域；生产由 VITE_API_BASE 注入真实地址。
const API_TARGET = process.env.VITE_API_TARGET ?? 'http://localhost:8000'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    // 允许局域网设备访问（监听所有网卡）。
    // 可用 `npm run dev -- --host 0.0.0.0` 临时覆盖，或 `--host` 仅本机。
    host: true,
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
        // /api/agents -> http://localhost:8000/agents
        rewrite: (path) => path.replace(/^\/api/, ''),
        // 语音输入 WS（/api/sessions/{id}/voice）经同一代理升级转发
        ws: true,
      },
    },
  },
})
