// Copyright 2026 The ATOM Authors
// SPDX-License-Identifier: MIT

import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Dev-server mirror of the production nginx rule
// (`location = / { return 302 /${AUTHORITY_CONTEXT}/; }`): the app's Router has
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

// Where the dev server proxies API/WS traffic. Defaults to the native backend
// (backend/run_dev.sh), which is what `npm run dev` alone assumes. Override when
// the backend you want is the DOCKERISED one — compose publishes it on 18000,
// not 8000 — e.g. `BACKEND_ORIGIN=http://localhost:18000 npm run dev`. Env-driven
// rather than edited in place so switching targets is not a working-tree change.
const BACKEND_ORIGIN = process.env.BACKEND_ORIGIN || 'http://localhost:8000'
const BACKEND_WS = BACKEND_ORIGIN.replace(/^http/, 'ws')

// Keep the browser's Host (localhost:<dev port>) instead of rewriting it to the
// target. `core/csrf._allowed_origins` accepts the request's OWN origin, derived
// from Host, so preserving it makes the dev server same-origin as far as the
// CSRF check is concerned. Rewriting it made Host the backend's origin while the
// browser still sent `Origin: localhost:3000`, and every state-changing request
// came back 403 "Cross-origin request rejected" — GETs were unaffected, so the
// app looked fine until the first POST. Production is unaffected: there the SPA
// and the API are behind one nginx and genuinely share an origin.
const changeOrigin = false

export default defineConfig({
  base: './',
  plugins: [react(), tailwindcss(), redirectRootToA2a()],
  server: {
    port: Number(process.env.PORT) || 3000,
    proxy: {
      // Order matters — Vite matches the first prefix that hits, so the more
      // specific WS rule MUST come before the generic /a2a/api rule.
      // The frontend connects to /a2a/api/ws/... (because the WS endpoints
      // live under FastAPI's /api/ws/... router and the platform is mounted
      // under /a2a/). After the rewrite strips /a2a, the backend sees /api/ws/...
      '/a2a/api/ws': { target: BACKEND_WS, ws: true, changeOrigin, rewrite: (path) => path.replace(/^\/a2a/, '') },
      '/a2a/api':    { target: BACKEND_ORIGIN, changeOrigin, rewrite: (path) => path.replace(/^\/a2a/, '') },
      '/a2a/ws':     { target: BACKEND_WS, ws: true, rewrite: (path) => path.replace(/^\/a2a/, '') },
      '/api':        { target: BACKEND_ORIGIN, changeOrigin },
      '/ws':         { target: BACKEND_WS, ws: true },
    },
  },
})
