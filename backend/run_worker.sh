#!/usr/bin/env bash
# Native (non-Docker) dev launcher for the celery worker.
#
#   bash backend/run_worker.sh
#
# This is the process the AGENTS run in — Phase A document generation, the
# certification test-case engine, Phase B codegen. It therefore needs the same
# environment as the API, and DOMAIN_PACK in particular: without it the engine
# writes another domain's vocabulary into every generated workbook (a library
# change stamped "UPI 2.0" / "NPCI") with no error anywhere.
#
# The API had a launcher and the worker did not, which is exactly how the two
# came to disagree. Both now source run_env.sh.
set -euo pipefail
cd "$(dirname "$0")"
. ./run_env.sh
exec .venv/bin/celery -A app.services.celery_tasks worker \
  --loglevel=info -Q agentic,celery --concurrency=2
