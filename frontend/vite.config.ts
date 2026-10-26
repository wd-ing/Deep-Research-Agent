import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5180,
    proxy: {
      // SSE 必须关掉代理的缓冲，否则事件会被攒住一次性吐出来
      '/api': {
        // 默认 8000（用户的常驻回放/开发后端）；本地起临时后端时
        // 用环境变量改目标，不改常驻配置的默认值。
        target: process.env.VITE_API_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
