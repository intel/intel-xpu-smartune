// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dev server is served over HTTPS so that it is a secure context even when
// opened by IP (http://<host-ip>:39527 is not). The dashboard elects one tab to
// hold the server-push streams with the Web Locks API, which browsers expose
// only to secure contexts; without it every tab opens its own streams and the
// third tab exhausts the 6-connections-per-host limit (src/events/sseHub.ts).
//
// The certificate is a self-signed one generated on first start into
// dashboard/.dev-cert/ (key/b_server.* belongs to root and is not readable
// here). Browsers warn once per host; accept it like the backend's.
// VITE_DEV_HTTP=1 serves plain http instead.
const CERT_DIR = join(__dirname, '.dev-cert')

function devCert(): { cert: Buffer; key: Buffer } | undefined {
  if (process.env.VITE_DEV_HTTP === '1') return undefined
  const cert = join(CERT_DIR, 'dev.crt')
  const key = join(CERT_DIR, 'dev.key')
  try {
    if (!existsSync(cert) || !existsSync(key)) {
      mkdirSync(CERT_DIR, { recursive: true, mode: 0o700 })
      execFileSync('openssl', [
        'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '825',
        '-subj', '/CN=SmarTune dashboard dev server',
        '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1',
        '-keyout', key, '-out', cert,
      ], { stdio: 'ignore' })
    }
    return { cert: readFileSync(cert), key: readFileSync(key) }
  } catch (err) {
    console.warn(`[dev-cert] HTTPS unavailable, serving plain http: ${(err as Error).message}`)
    console.warn('[dev-cert] Opened by IP, more than two dashboard tabs may hang.')
    return undefined
  }
}

export default defineConfig(({ command }) => ({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 39527,
    // Only for `vite` (serve): a build must not need openssl or write a cert.
    https: command === 'serve' ? devCert() : undefined,
    proxy: {
      '/api': {
        // Backend to proxy to. Both server modes (-a/-m) listen on 9001; the UI
        // adapts to a monitor-only server via /smartune/capabilities. Override
        // with VITE_PROXY_TARGET for a remote backend.
        target: process.env.VITE_PROXY_TARGET || 'https://127.0.0.1:9001',
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
}))
