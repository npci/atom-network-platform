#!/usr/bin/env bash
# Native (non-Docker) dev launcher for the API.
#
#   bash backend/run_dev.sh
#
# The environment (DB/Redis URLs, DOMAIN_PACK, ARTIFACTS_DIR) lives in
# run_env.sh so this and run_worker.sh cannot drift apart — see the note there.
# Everything else (SECRET_KEY, CERT_HARNESS, CAPTCHA_ENABLED, …) loads from
# backend/.env via pydantic-settings' own env_file mechanism.
set -euo pipefail
cd "$(dirname "$0")"
. ./run_env.sh
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
