import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath, URL } from 'node:url'

// 后端默认监听 0.0.0.0:8000（见 backend/app/config.py）。
// dev 通过 /api 前缀代理，避开跨域；生产由 VITE_API_BASE 注入真实地址。
const API_TARGET = process.env.VITE_API_TARGET ?? 'http://localhost:8000'

// CSS 构建目标 = 项目玻璃化样式的支持矩阵（源码注释同源）。
const CSS_TARGETS = ['chrome76', 'edge79', 'firefox103', 'safari9']

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    cssTarget: CSS_TARGETS,
    // CSS 不压缩（仅去空白级优化由 gzip 承担）：Vite 8 的 CSS 压缩链
    // （rolldown/lightningcss，cssTarget 与 cssMinify:'lightningcss' 均无法
    // 改变其行为）会把标准 backdrop-filter 视为 -webkit- 别名的冗余而整条
    // 删除；Firefox 不应用该别名 → 生产构建玻璃模糊全部失效（dev server
    // 未压缩故正常）。JS 仍走默认压缩。
    cssMinify: false,
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
