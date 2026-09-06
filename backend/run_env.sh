#!/usr/bin/env bash
# Shared native (non-Docker) dev environment — SOURCE this, do not exec it.
#
#   . "$(dirname "$0")/run_env.sh"
#
# Every native process needs the SAME environment. It used to live only in
# run_dev.sh, so the API got it and the celery worker — started by hand —
# did not. That is not a cosmetic drift: DOMAIN_PACK is read straight from
# os.environ (see below), the agents run in the WORKER, and a worker without
# it silently generates another domain's vocabulary. Anything that starts a
# native process sources this file so the two cannot diverge again.
#
# Docker needs none of this: compose passes `env_file: ./backend/.env`, which
# puts every key into the container's real environment.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# Points at the SAME already-migrated Postgres/Redis brought up by docker
# compose — only the app tier moves out of Docker. backend/.env deliberately
# omits these two so a native run cannot inherit container hostnames.
#
# The passwords come from .env (gitignored), never from this file. A datastore
# password with a repository default is a datastore with no password — the same
# reason docker-compose.yml declares POSTGRES_PASSWORD/REDIS_PASSWORD as
# `${VAR:?}` and refuses to start rather than fall back.
: "${POSTGRES_PASSWORD:=$(grep -m1 '^POSTGRES_PASSWORD=' .env 2>/dev/null | cut -d= -f2-)}"
: "${REDIS_PASSWORD:=$(grep -m1 '^REDIS_PASSWORD=' .env 2>/dev/null | cut -d= -f2-)}"
if [ -z "${POSTGRES_PASSWORD}" ] || [ -z "${REDIS_PASSWORD}" ]; then
  echo "run_env.sh: POSTGRES_PASSWORD and REDIS_PASSWORD must be set, in the" >&2
  echo "environment or in backend/.env (cp backend/.env.example backend/.env)." >&2
  return 1 2>/dev/null || exit 1
fi
export DATABASE_URL="postgresql://atom_user:${POSTGRES_PASSWORD}@127.0.0.1:17433/atom_cm_db"
export REDIS_URL="redis://:${REDIS_PASSWORD}@127.0.0.1:16380/0"

# DOMAIN_PACK is the one setting pydantic does NOT supply. app/core/domain/
# registry.py reads it from os.environ directly, because several agents build
# their system prompts as module-level constants at import time and a registry
# needing a DB session or a request would be unusable there. pydantic-settings'
# env_file loading populates Settings, NOT the process environment — so a
# DOMAIN_PACK sitting in .env is invisible to the registry and it falls back to
# packs/upi/upi.yaml with no error. Exporting it here is what makes .env the
# single source of truth for native runs too.
if grep -q '^DOMAIN_PACK=' .env 2>/dev/null; then
  export DOMAIN_PACK="$(grep '^DOMAIN_PACK=' .env | tail -1 | cut -d= -f2-)"
fi

# settings.artifacts_dir defaults to "/app/artifacts" (the Docker mount point),
# which does not exist on a native run. Prefer whatever .env declares; fall
# back to a writable directory this user owns. `backend/artifacts` cannot be
# used — it is a root-owned leftover from an earlier bind mount.
if grep -q '^ARTIFACTS_DIR=' .env 2>/dev/null; then
  ARTIFACTS_DIR="$(grep '^ARTIFACTS_DIR=' .env | tail -1 | cut -d= -f2-)"
else
  ARTIFACTS_DIR="${TMPDIR:-/tmp}/atom-artifacts"
fi
mkdir -p "$ARTIFACTS_DIR"
export ARTIFACTS_DIR

printf 'env: DOMAIN_PACK=%s\n' "${DOMAIN_PACK:-<unset — will default to UPI>}" >&2
