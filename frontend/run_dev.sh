#!/usr/bin/env bash
# Native (non-Docker) dev launcher for the Vite dev server. vite.config.js's
# proxy targets are hardcoded to http://localhost:8000 — matches the native
# backend's port (backend/run_dev.sh), so no config changes needed here.
set -euo pipefail
cd "$(dirname "$0")"
exec npm run dev -- --host 0.0.0.0 --port 3000
