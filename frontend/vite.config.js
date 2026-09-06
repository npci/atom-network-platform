// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Dev-server mirror of the production nginx rule
// (`location = / { return 302 /${NPCI_CONTEXT}/; }`): the app's Router has
// basename="/a2a", so a bare `/` renders nothing (React mounts, Router
// matches no route, page is blank) — matches production behavior exactly
// rather than only working around it for dev.
function redirectRootToA2a() {
  return {
    name: 'redirect-root-to-a2a',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (req.url === '/') {
          res.writeHead(302, { Location: '/a2a/' })
          res.end()
          return
        }
        next()
      })
    },
  }
}

export default defineConfig({
  base: './',
  plugins: [react(), tailwindcss(), redirectRootToA2a()],
  server: {
    port: 3000,
    proxy: {
      // Order matters — Vite matches the first prefix that hits, so the more
      // specific WS rule MUST come before the generic /a2a/api rule.
      // The frontend connects to /a2a/api/ws/... (because the WS endpoints
      // live under FastAPI's /api/ws/... router and the platform is mounted
      // under /a2a/). After the rewrite strips /a2a, the backend sees /api/ws/...
      '/a2a/api/ws': { target: 'ws://localhost:8000', ws: true, changeOrigin: true, rewrite: (path) => path.replace(/^\/a2a/, '') },
      '/a2a/api':    { target: 'http://localhost:8000', changeOrigin: true, rewrite: (path) => path.replace(/^\/a2a/, '') },
      '/a2a/ws':     { target: 'ws://localhost:8000', ws: true, rewrite: (path) => path.replace(/^\/a2a/, '') },
      '/api':        { target: 'http://localhost:8000', changeOrigin: true },
      '/ws':         { target: 'ws://localhost:8000', ws: true },
    },
  },
})
