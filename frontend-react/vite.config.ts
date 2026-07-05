import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

const apiTarget = process.env.VITE_API_TARGET || 'http://127.0.0.1:8080'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 3567,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
      '/images': {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    rollupOptions: {
      output: {
        // Wave C: 560KB 单包拆分——react 核心与图标库独立成稳定 chunk,
        // 业务代码更新时用户无需重新下载框架部分
        manualChunks: {
          'vendor-react': ['react', 'react-dom'],
          'vendor-ui': ['lucide-react', 'sonner', 'zustand'],
        },
      },
    },
  },
})
