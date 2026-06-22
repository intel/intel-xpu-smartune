import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 39527,
    proxy: {
      '/api': {
        target: 'https://127.0.0.1:9001',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
        secure: false,
        ws: true,
        configure: (proxy) => {
          proxy.on('error', (err, req, res) => {
            console.warn(`[proxy] ${req?.method} ${req?.url} -> ${(err as NodeJS.ErrnoException).code || err.message}`)
            if (res && 'writeHead' in res && !res.headersSent) {
              try {
                res.writeHead(502, { 'Content-Type': 'application/json' })
                res.end(JSON.stringify({ error: 'upstream_unavailable', detail: err.message }))
              } catch { /* socket already gone */ }
            }
          })
          proxy.on('proxyReq', (proxyReq) => {
            proxyReq.on('error', (e) => {
              console.warn('[proxy] proxyReq error:', (e as NodeJS.ErrnoException).code || e.message)
            })
          })
        },
      },
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          'react-vendor': ['react', 'react-dom'],
          'antd-vendor': ['antd', '@ant-design/icons', '@ant-design/colors'],
          'charts-vendor': ['recharts'],
          'http-vendor': ['axios'],
        },
      },
    },
  },
})
