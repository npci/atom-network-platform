# Deployment Guide — AtOM

Installation and configuration, for both Docker and native deployments.

[`README.md`](README.md) has a Quick Start that gets a local stack running in
about ten minutes. This document is the longer form: what each service is for,
every configuration layer and which one wins, and what to change before running
anywhere other than a laptop.

The Partner Platform has its own guide in its own repository —
[`atom-partner-platform`](https://github.com/npci/atom-partner-platform). It is
a separate stack and nothing here deploys it.

> **On Windows?** Every shell block in this guide is written for Linux/macOS
> and has a PowerShell equivalent alongside it. Read
> [§2 → Windows hosts](#windows-hosts) **before** the first `git clone` — one
> of the settings there (line endings) has to be right at clone time, and
> getting it wrong breaks the containers in a way that does not look like a
> Windows problem.

---

## Table of contents

1. [Platform overview](#1-platform-overview)
2. [Prerequisites](#2-prerequisites) — including [Windows hosts](#windows-hosts)
3. [Option A — Docker (recommended)](#3-option-a--docker-recommended)
4. [Option B — native](#4-option-b--native)
5. [First-time setup](#5-first-time-setup)
6. [Configuration](#6-configuration)
7. [Environment variable reference](#7-environment-variable-reference)
8. [Production hardening](#8-production-hardening)
9. [Verification checklist](#9-verification-checklist)
10. [Troubleshooting](#10-troubleshooting)

11. [Deployment pitfalls found in practice](#11-deployment-pitfalls-found-in-practice)

---

## 1. Platform overview

Eight services in the default stack, behind a single nginx front door.

| Service | Image | Published port | Role |
|---|---|---|---|
| `nginx` | `nginx:alpine` | `80` | The only intended entry point |
| `frontend` | built | `3000` | The platform UI |
| `backend` | built | `8010` → 8000 | Authoring, retrieval, code generation, A2A server |
| `celery` | built | — | Background work |
| `celery_beat` | built | — | Scheduler for periodic sweeps |
| `postgres` | `pgvector/pgvector:pg16` | `127.0.0.1:7432` | Platform data **and** the vector index |
| `redis` | `redis:7-alpine` | `127.0.0.1:6379` | Celery broker, HMAC nonce store, rate-limit counters |
| `ollama` | `ollama/ollama` | — | Local embeddings, so ingestion needs no external API |

A ninth service, `reranker`, is behind a Compose profile and off by default. It
holds the torch stack, costs roughly 2 GB of image and 1.5 GB of RAM, and serves
a feature that is itself disabled by default.

**Postgres and Redis are bound to `127.0.0.1`.** They are published for
debugging, not for use — nothing outside the host should reach them.

### Routing

| Path | Goes to |
|---|---|
| `/a2a/` | platform UI |
| `/a2a/api/` | platform backend |
| `/a2a/api/ws/` | backend, WebSocket upgrade |
| `/a2a-rpc/` | the A2A JSON-RPC wire |
| `/.well-known/` | agent-card discovery, **unprefixed by design** |

Card discovery is served at the root because a remote agent fetches the card
without knowing anything about local path layout. Publishing it under a prefix
broke discovery once.

---

## 2. Prerequisites

### Docker deployment

- **Docker** with **Compose v2**
- Roughly **15 GB** of free disk, and 10–20 minutes for the first build

### Native deployment

- **Python 3.12** (the image is `python:3.12-slim`)
- **Node 20** (the frontend builder is `node:20-alpine`)
- **PostgreSQL 16 with pgvector**
- **Redis 7**
- **Ollama**, with `nomic-embed-text` pulled

### Credentials you must supply

| Value | Why |
|---|---|
| `POSTGRES_PASSWORD` | No default — Compose refuses to start without it |
| `REDIS_PASSWORD` | No default |
| `CERTSIM_INTERNAL_TOKEN` | No default; any value for a local stack |
| `SECRET_KEY` | JWT signing key |
| `CONFIG_ENCRYPTION_KEY` | Required before any secret can be stored |
| An LLM provider key | The stack starts without one; every AI feature then fails on authentication |

A datastore password with a published default is a datastore with no password,
which is why the first three are declared `${VAR:?...}` rather than given a
convenient fallback.

### Windows hosts

The stack itself is Linux containers and runs unchanged. What differs is the
shell you type into and four host-level details that are easy to get wrong.

**Pick a path first.** Both work; they fail differently.

| | **A — clone inside WSL2** (recommended) | **B — clone on Windows, drive from PowerShell** |
|---|---|---|
| Shell | bash, exactly as written in this guide | PowerShell 7; every block has a variant below |
| `.sh` scripts (`hygiene-check`, `sync-a2a-core`) | run natively | need Git Bash or `wsl bash scripts/...` |
| Bind-mount performance | fast (native ext4) | slow — the tree crosses a 9p/virtiofs share |
| File-ownership semantics | Linux, so [§11.1](#111-a-missing-bind-mount-source-becomes-a-root-owned-directory) applies exactly as written | Docker Desktop exposes binds permissively, so the *ownership* half of §11.1 usually will not bite — the missing-source half still does |
| Where to clone | `~/atom-network-platform` inside the distro, **not** `/mnt/c/...` | anywhere on `C:` |

Cloning into `/mnt/c/...` from WSL is the worst of both: Linux semantics on top
of the slow share. Pick one side and stay on it.

**1. Docker Desktop, WSL2 backend.** Install
[Docker Desktop](https://docs.docker.com/desktop/install/windows-install/) and
confirm **Settings → General → Use the WSL 2 based engine** is ticked. Raise
**Settings → Resources** to at least 8 GB RAM and 15 GB disk — the first build
pulls a torch-free but still large backend image, and an under-provisioned VM
fails the build with an opaque exit 137 (out of memory).

On path B, also tick the repository's drive under **Settings → Resources →
File sharing**, or every `./artifacts`-style bind mount comes up empty.

**2. Line endings — set this before cloning.** `nginx/docker-entrypoint.sh` and
`frontend/docker-entrypoint.sh` are `#!/bin/sh` scripts that get bind-mounted
and copied into Linux containers. Git for Windows converts them to CRLF by
default, and the container then dies with
`exec /docker-entrypoint.sh: no such file or directory` or `: not found` on
line 2 — a message that names the file it just proved exists.

```powershell
git config --global core.autocrlf input
git clone https://github.com/npci/atom-network-platform
```

The repository ships a `.gitattributes` pinning `*.sh` to LF, which protects a
fresh clone. `core.autocrlf input` protects the files it does not cover, and
repairs an existing clone made before that file landed:

```powershell
git rm --cached -r .
git reset --hard
```

**3. Port 80 is often already taken.** IIS, the World Wide Web Publishing
Service, or a Hyper-V reserved port range will claim it, and nginx then fails
to bind:

```powershell
Get-NetTCPConnection -LocalPort 80 -State Listen | Select-Object OwningProcess
Get-Process -Id <pid>
```

Stop the owner, or publish nginx elsewhere by editing its `ports:` entry to
`"8080:80"` — and then set `FRONTEND_URL` to match, or every API call is
refused as cross-origin ([§11.5](#115-cors-allows-exactly-one-origin)).

**4. `PHASE_B_HOST_KEY_FILE` has a `/dev/null` default that Windows has no
path for.** `docker-compose.yml` falls back to `/dev/null` for the Phase B
deploy key so the bind never errors out. On a Windows host that path does not
resolve and Docker creates an empty *directory* in its place — the
[§11.1](#111-a-missing-bind-mount-source-becomes-a-root-owned-directory)
failure. Point it at a real file:

```powershell
# in the root .env, path B — any readable file will do if you are not using Phase B ssh mode
PHASE_B_HOST_KEY_FILE=C:\Users\<you>\.ssh\deploy_key
```

Path A (WSL) needs nothing here — `/dev/null` exists inside the distro.

**5. `curl` is not curl.** In PowerShell 5.1 `curl` is an alias for
`Invoke-WebRequest`, which takes different flags and returns an object. Call
`curl.exe` explicitly wherever this guide says `curl`. PowerShell 7 dropped the
alias, so `curl` works there — `curl.exe` is correct on both, and is what the
variants below use.

`jq`, `grep` and `head` are likewise absent. The PowerShell variants below
avoid them.

**6. Git Bash rewrites arguments that start with `/`.** MSYS silently converts
`/CN=localhost`, `/etc/nginx/conf.d`, or any other `/`-prefixed argument into a
Windows path — so a command like

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout deploy/tls/key.pem -out deploy/tls/cert.pem -subj "/CN=localhost"
```

produces a cert whose subject is
`/C=US/…/CN=C:/Program Files/Git/CN=localhost` instead of `CN=localhost`, and
the TLS handshake against `https://localhost` then fails hostname verification.
Prefix the command with `MSYS_NO_PATHCONV=1` (one-shot, per invocation):

```bash
MSYS_NO_PATHCONV=1 openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout deploy/tls/key.pem -out deploy/tls/cert.pem -subj "/CN=localhost"
```

This hits anyone generating a self-signed cert for the sibling
[`atom-partner-platform`](https://github.com/npci/atom-partner-platform)'s edge
proxy from Git Bash. WSL, PowerShell 7, and cmd.exe are all unaffected.

---

## 3. Option A — Docker (recommended)

```bash
git clone https://github.com/npci/atom-network-platform
cd atom-network-platform

# 1. Two env files, and you need both
cp .env.example .env                      # datastore credentials, read by Compose
cp backend/.env.example backend/.env      # ~100 application settings

# 2. Generate the two keys that must not be defaults
python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_urlsafe(48))"
python3 -c "from cryptography.fernet import Fernet; print('CONFIG_ENCRYPTION_KEY=' + Fernet.generate_key().decode())"
#    …and paste both into backend/.env, plus your LLM key.

# 3. The external network three services attach to
docker network create certagent_cert-net

# 4. Build and start
docker compose up -d --build

# 5. Pull the embedding model (first time only, ~274 MB)
docker exec atom_ollama ollama pull nomic-embed-text

# 6. Create the first admin user
docker compose exec backend python seed.py
```

<details>
<summary><b>PowerShell equivalent</b> (Windows, path B — path A uses the block above unchanged)</summary>

```powershell
git clone https://github.com/npci/atom-network-platform
cd atom-network-platform

# 0. Create the bind-mount sources yourself, before the first `up` — see §11.1
New-Item -ItemType Directory -Force -Path artifacts, knowledge_base, workspace, logs

# 1. Two env files, and you need both
Copy-Item .env.example .env
Copy-Item backend\.env.example backend\.env

# 2. Generate the two keys that must not be defaults
python -c "import secrets; print('SECRET_KEY=' + secrets.token_urlsafe(48))"
python -c "from cryptography.fernet import Fernet; print('CONFIG_ENCRYPTION_KEY=' + Fernet.generate_key().decode())"
#    …and paste both into backend\.env, plus your LLM key.

# 3. The external network three services attach to
docker network create certagent_cert-net

# 4. Build and start
docker compose up -d --build

# 5. Pull the embedding model (first time only, ~274 MB)
docker exec atom_ollama ollama pull nomic-embed-text

# 6. Create the first admin user
docker compose exec backend python seed.py
```

Step 2 needs a Python on the host with `cryptography` installed. If you would
rather not have one, generate the keys inside the container after step 4:

```powershell
docker compose run --rm backend python -c "import secrets; print(secrets.token_urlsafe(48))"
docker compose run --rm backend python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`python` is the launcher name on Windows; there is no `python3` unless you
made one. `py -3.12` selects a specific version where several are installed.

</details>

Open <http://localhost/a2a/>.

### Why two env files

The root `.env` is what Compose **interpolates into `docker-compose.yml`** —
`${POSTGRES_PASSWORD}` and friends. `backend/.env` is passed as the
**environment of three containers** via `env_file:`. They are read at different
times by different things, and neither substitutes for the other.

### Why the network step

`backend`, `celery` and `celery_beat` attach to `certagent_cert-net`, declared
`external`, so Compose expects it to already exist and refuses to start with
*"network certagent_cert-net declared as external, but could not be found"*.
Creating it empty is enough; it exists so those services can resolve a
certification agent where one is deployed.

### Migrations

Run automatically. The backend's command is `alembic upgrade head` followed by
uvicorn, so a fresh database is migrated on first boot. To run them by hand:

```bash
docker compose run --rm backend alembic upgrade head
docker compose run --rm backend alembic current      # should report 0130
```

### Everyday operations

```bash
docker compose up -d --build backend      # after a dependency change
docker compose restart nginx              # ALWAYS after recreating a backend
docker compose logs -f backend
docker compose down                       # stop, keeping data volumes
docker compose down -v                    # destroy all data
```

**Restart nginx after recreating any backend.** It resolves upstream DNS once at
startup, so a recreated container gets a new address while nginx keeps proxying
to the old one and returns 502. This is the most common false alarm in this
stack.

### Optional: the reranker

```bash
docker compose --profile reranker up -d   # and set USE_RERANKER=true in backend/.env
```

Retrieval **fails open** if the sidecar is unreachable while `USE_RERANKER=true`
— it returns the RRF ordering rather than erroring. Watch the sidecar's
`/healthz` `model_loaded` field so that degradation is visible rather than
silent.

### Optional: use the host's Ollama

```bash
docker compose -f docker-compose.yml -f docker-compose.ollama-host.yml up -d
```

Points the backend at `host.docker.internal:11434` and parks the bundled
container behind an unused profile.

---

## 4. Option B — native

Supported for the application; the datastores still run in containers unless you
provide your own.

```bash
# Datastores only
docker compose -f docker-compose.db.yml up -d

# Backend
cd backend
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit: DATABASE_URL, REDIS_URL, OLLAMA_URL,
                            # SECRET_KEY, CONFIG_ENCRYPTION_KEY, your LLM key
alembic upgrade head
python seed.py
uvicorn app.main:app --host 0.0.0.0 --port 8000

# Celery, in separate shells
celery -A app.services.celery_tasks worker --loglevel=info
celery -A app.services.celery_tasks beat   --loglevel=info

# Frontend
cd ../frontend
npm ci
npm run dev                 # :3000, proxies /a2a/api to localhost:8000
npm run build               # or build for a static server
```

<details>
<summary><b>PowerShell equivalent</b></summary>

```powershell
# Datastores only
docker compose -f docker-compose.db.yml up -d

# Backend
cd backend
py -3.12 -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env   # then edit: DATABASE_URL, REDIS_URL, OLLAMA_URL,
                              # SECRET_KEY, CONFIG_ENCRYPTION_KEY, your LLM key
alembic upgrade head
python seed.py
uvicorn app.main:app --host 0.0.0.0 --port 8000

# Celery, in separate shells
celery -A app.services.celery_tasks worker --loglevel=info --pool=solo
celery -A app.services.celery_tasks beat   --loglevel=info

# Frontend
cd ..\frontend
npm ci
npm run dev
npm run build
```

Three things differ beyond the syntax:

- **`Activate.ps1` may be blocked** by the execution policy, with
  *"running scripts is disabled on this system"*. Unblock it for this shell
  only — no permanent policy change:
  `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`
- **Celery needs `--pool=solo` on Windows.** The default `prefork` pool relies
  on `fork(2)`, which Windows does not have; without it the worker starts,
  accepts tasks and then dies on the first one. `solo` is single-threaded —
  fine for development, not for a real deployment. **Run production workers
  under WSL2 or Linux**, not native Windows.
- **`DOMAIN_PACK` must be a real environment variable**, not just a
  `backend\.env` line — see [§11.9](#119-domain_pack-is-read-from-the-process-environment),
  which applies to every native run regardless of OS.
  `$env:DOMAIN_PACK = "network"` before starting each process, in each shell.

</details>

A native install must set `DATABASE_URL`, `REDIS_URL` and `OLLAMA_URL`
explicitly. Under Docker those three are set in each service's `environment:`
block and **override** whatever `backend/.env` says, so the in-network service
names resolve; natively there is no such override and the defaults point at
container hostnames that do not exist.

### Dependencies are hash-locked

The images install from `requirements.<arch>.lock` with `--require-hashes`.
**Editing `requirements.txt` alone changes nothing.** Regenerate the lock — the
command is in the lock's own header. There is one lock per architecture because
`torch` declares CUDA dependencies on amd64 that do not exist on arm64.

---

## 5. First-time setup

### Create the admin user

```bash
docker compose exec backend python seed.py
```

Prints the generated password **once**. Set `ADMIN_EMAIL` and `ADMIN_PASSWORD`
in `backend/.env` beforehand to choose your own. Re-running is a no-op if the
user exists.

**`exec` reads the environment the container already has.** `backend/.env` is
loaded via `env_file:` when the container is *created*, so editing it after
`docker compose up -d` does not change a running container — `exec` then still
sees no `ADMIN_PASSWORD`, and `seed.py` falls back to a generated one. That
reads as "Compose is ignoring my `.env`", which is not what happened. Either
recreate first, or pass the value on the command line:

```bash
docker compose up -d backend                       # picks up the edited .env
# …or skip the recreate:
docker compose exec -e ADMIN_PASSWORD='<yours>' backend python seed.py
```

Only `seed.py` reads these three variables, and all are optional. The running
backend never reads them, so an unset `ADMIN_PASSWORD` **cannot** cause a
restart loop — if the backend is restarting, the cause is `alembic upgrade
head` failing in its command, not the admin password. See
[§11.3](#113-live-mounting-app-without-alembic-produces-a-crashloop).

Idempotency keys on `ADMIN_USERNAME`, not the email: once `admin` exists,
re-running will not apply a changed password. Use `reset_password.py` below.

**Log in with `admin`, not `admin@localhost`.** The seed writes
`ADMIN_USERNAME` (defaults to `admin`) as the login handle; the email is
metadata. Trying to sign in as `admin@localhost` returns *invalid username or
password* even though the row exists.

### Change the seeded password

```bash
docker compose run --rm -v "$(pwd)/backend/scripts:/app/bscripts" backend \
  python /app/bscripts/reset_password.py <admin-email> --password '<new>'
```

In PowerShell — `$(pwd)` is `${PWD}`, and the line continuation is a backtick:

```powershell
docker compose run --rm -v "${PWD}/backend/scripts:/app/bscripts" backend `
  python /app/bscripts/reset_password.py <admin-email> --password '<new>'
```

Keep the forward slash after `${PWD}`: Docker wants a POSIX path on the
container side of the colon, and accepts the mixed form on the host side.

### Add further users

From **Admin → User Management**, or in bulk:

```bash
docker compose exec backend python scripts/create_user.py \
  --email alice@example.org --name "Alice Patel" \
  --role product_owner --password 'StartHere123!'
```

Six roles exist: `product_owner`, `product_manager`, `tech_lead`,
`infosec_reviewer`, `risk_reviewer`, `admin`.

### The login CAPTCHA

On by default. Set `CAPTCHA_ENABLED=false` in `backend/.env` for a local loop,
and leave it on anywhere others can reach.

---

## 6. Configuration

### Three layers, and which one wins

This is the part most worth reading before changing a setting.

```
   Admin → Configuration  (database)        ← WINS at runtime
            ↑
   docker-compose.yml  environment:         ← wins over .env
            ↑
   backend/.env                             ← the bootstrap default
```

1. **Database, via Admin → Configuration.** Operator-tunable settings — AI
   provider and models, GitLab, SMTP, Jenkins and UAT endpoints — plus all
   secrets, are owned by the database and edited in the admin UI. That is the
   single edit surface at runtime.
2. **Compose `environment:`.** `DATABASE_URL`, `REDIS_URL` and `OLLAMA_URL` are
   set per service so in-network names resolve. These override `.env`.
3. **`backend/.env`.** First-run bootstrap defaults. Once a value is set in the
   UI, the database wins.

**Infrastructure settings stay env-owned and are not in the admin UI**: the
three URLs above, storage paths, `PHASE_B_*`, the CORS and A2A URLs, and the
`USE_*` / `AGENTIC_*` feature flags.

### Secrets at rest

`CONFIG_ENCRYPTION_KEY` is a Fernet key encrypting every secret in the
`app_configs` table and every credential on `partner_agents`, in the same
`enc:v1:` format. It is the one secret that must live in `.env` — you cannot
bootstrap database-stored secrets without it. **Outside development, storing a
secret with it unset is refused.**

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# or, with no host Python:
docker compose run --rm backend python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### A setting you add must be declared

`Settings` is `extra="ignore"`. Reading an undeclared name with
`getattr(settings, "name", default)` means the environment variable is silently
dropped and the default always wins — a knob that looks configurable and is not.
`backend/tests/core/test_config_no_phantom_knobs.py` enforces this.

---

## 7. Environment variable reference

`backend/.env.example` documents all ~107 settings in place, grouped by section.
The ones you are most likely to need:

### Required

| Variable | Notes |
|---|---|
| `SECRET_KEY` | JWT signing key. Long and random |
| `CONFIG_ENCRYPTION_KEY` | Fernet key; required before storing any secret |
| `POSTGRES_PASSWORD` · `REDIS_PASSWORD` · `CERTSIM_INTERNAL_TOKEN` | Root `.env`; no defaults |

### Core

| Variable | Default | Effect |
|---|---|---|
| `APP_ENV` | `development` | `development` \| `staging` \| `production`. Production enables the Secure cookie flag and hard-fails several unsafe configurations |
| `LLM_PROVIDER` | `anthropic` | `anthropic` \| `openai` \| `gemini` \| `ainxt` \| `ollama`. `claude` is accepted as a back-compat alias for `anthropic` |
| `ANTHROPIC_API_KEY` | — | Required when `LLM_PROVIDER=anthropic` |
| `DOMAIN_PACK` | `network` | The active domain pack. Read straight from the environment, not from `Settings` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `480` | Operator session TTL; slides forward past 50% |

### Feature flags

| Variable | Default | Effect |
|---|---|---|
| `CAPTCHA_ENABLED` | `true` | Login CAPTCHA |
| `USE_RERANKER` | `false` | Cross-encoder reranking; needs the sidecar |
| `GOVERNANCE_REVIEWS_ENABLED` | `false` | Governance review stages |
| `SANDBOX_ENABLED` | — | Docker-in-Docker sandboxed execution |
| `DEV_SKIP_APPROVALS` | `true` in the template | **Dev only** — auto-approves the BRD. Refused when `APP_ENV=production` |
| `AUTH_RETURN_TOKEN_IN_BODY` | `false` | Leave false; it is what keeps the JWT out of web storage |

### Phase B (code generation)

| Variable | Default | Effect |
|---|---|---|
| `PHASE_B_RUNNER_MODE` | `demo` | `demo` (simulated, labelled in every log line) \| `build` \| `ssh` \| `local` |
| `AGENTIC_BRANCH_PREFIX` | `atom` | Branches become `atom/xsd-<slug>` |

---

## 8. Production hardening

Beyond the defaults, before running anywhere real:

- **Set `APP_ENV=production`.** It is not cosmetic: it applies the Secure cookie
  flag, enables HSTS, disables the API docs endpoints, refuses `DEV_SKIP_APPROVALS`,
  and turns several weak configurations into startup failures.
- **Use the production overlay.** `docker-compose.prod.yml` takes no defaults and
  refuses to start on an unset credential.
- **Terminate TLS at the edge** and keep `proxy_read_timeout` on the A2A
  locations — long-running A2A calls need the headroom `nginx/nginx.conf`
  already provides.
- **Rotate partner secrets.** `POST /api/admin/partners/{id}/rotate-hmac-secret`.
  A scheduled Celery task (`scan_partner_secret_ages_task`) logs a warning per
  partner whose credentials were last rotated more than 90 days ago — a task
  parameter, not a configurable setting, so change it in the beat schedule if
  your policy differs.
- **Leave `A2A_REQUIRE_HMAC_FOR_ACTIVE_PARTNERS=true`.** Disabling it in
  production is itself a critical startup finding and the platform refuses to
  boot.
- **Back up Postgres**, including the pgvector data — the embedding index is
  expensive to rebuild.
- **Watch storage growth.** Artifacts, transcripts and the workspace grow with
  use; artifact cold-storage tiering exists but is not on by default.

---

## 9. Verification checklist

```bash
# Backend health
curl -s localhost:8010/api/health
# Migrations at head
docker compose run --rm backend alembic current          # expect 0130
# UI loads
curl -s -o /dev/null -w '%{http_code}\n' localhost/a2a/  # expect 200
# Agent card discovery, unprefixed
curl -s localhost/.well-known/agent-card.json | head -c 120
# Embedding model present
docker exec atom_ollama ollama list                      # expect nomic-embed-text
# Wire code matches its canonical source
bash scripts/ci/sync-a2a-core.sh --check
# Tests
docker compose run --rm backend pytest -q
```

<details>
<summary><b>PowerShell equivalent</b></summary>

```powershell
# Backend health
Invoke-RestMethod http://localhost:8010/api/health
# Migrations at head
docker compose run --rm backend alembic current            # expect 0130
# UI loads
(Invoke-WebRequest http://localhost/a2a/ -UseBasicParsing).StatusCode   # expect 200
# Agent card discovery, unprefixed
Invoke-RestMethod http://localhost/.well-known/agent-card.json | ConvertTo-Json -Depth 3
# Embedding model present
docker exec atom_ollama ollama list                        # expect nomic-embed-text
# Wire code matches its canonical source
wsl bash scripts/ci/sync-a2a-core.sh --check               # or run it from Git Bash
# Tests
docker compose run --rm backend pytest -q
```

`Invoke-WebRequest` throws on a non-2xx status instead of reporting it, so
wrap the UI check in `try { … } catch { $_.Exception.Response.StatusCode }`
if you want the failing code rather than a red exception.

**The `.sh` gates need a bash.** `sync-a2a-core.sh` and `hygiene-check.sh`
have no PowerShell port — run them through `wsl bash …`, through Git Bash, or
let CI run them. They are checks, not build steps: a Windows developer who
skips them locally still gets the same verdict on push.

</details>

---

## 10. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `network certagent_cert-net ... not found` | `docker network create certagent_cert-net` |
| `502 Bad Gateway` after rebuilding a backend | nginx caches upstream DNS at startup. `docker compose restart nginx` |
| `POSTGRES_PASSWORD must be set` | Root `.env` missing or incomplete. `cp .env.example .env` |
| `env file ... backend/.env not found` | `cp backend/.env.example backend/.env` |
| Every AI feature returns an auth error | No LLM key, or one that does not match `LLM_PROVIDER` |
| Storing a secret is refused | `CONFIG_ENCRYPTION_KEY` unset. Outside development this is deliberate |
| Retrieval scores 0 for everything | Empty corpus, or the model was never pulled: `docker exec atom_ollama ollama pull nomic-embed-text` |
| A `backend/app/**` edit has no effect | `app/` is baked into the image. Add a `docker-compose.override.yml` bind mount |
| A test edit has no effect | `backend/tests/` is baked too. Rebuild or `docker cp` it in |
| `pip ... hashes do not match` | Stale lockfile, or building a different architecture. Regenerate — see the lock header |
| A2A call rejected with a signature error | Check the `a2a_messages` audit row for the layer that refused, then confirm `hmac_signer.py` matches the partner's copy |
| `Excel Testcase Engine registration failed` | Almost always directory ownership, not engine wiring — see [11.1](#111-a-missing-bind-mount-source-becomes-a-root-owned-directory) |
| `not a directory: Are you trying to mount a directory onto a file` | A compose `volumes:` entry names a path that no longer exists — see [11.1](#111-a-missing-bind-mount-source-becomes-a-root-owned-directory) |
| `Can't locate revision identified by '<rev>'` | `app/` is live-mounted but `alembic/` is not — see [11.3](#113-live-mounting-app-without-alembic-produces-a-crashloop) |
| A setting in `.env` appears to be ignored | An `environment:` entry overrides it, or the key is assigned twice — see [11.2](#112-environment-silently-overrides-env_file) |
| Every API call is blocked by the browser as cross-origin | `FRONTEND_URL` does not match the origin you are browsing — see [11.5](#115-cors-allows-exactly-one-origin) |
| The UI is blank, and the Network tab shows no failures | An asset resolved to HTML with a 200 — see [11.6](#116-a-blank-spa-page-usually-means-an-asset-resolved-to-html) |
| A deep link 404s or 302s, but the index page loads | The proxy and the SPA config disagree on who owns the context prefix — see [11.6](#116-a-blank-spa-page-usually-means-an-asset-resolved-to-html) |
| nginx exits with `host not found in upstream` | One unresolvable upstream kills the whole proxy — see [11.7](#117-nginx-resolves-every-upstream-at-startup) |
| A job stays `running` and never times out | `celery` / `celery_beat` are not up — see [11.8](#118-background-workers-are-not-optional) |
| Generated prose uses the wrong domain's vocabulary | `DOMAIN_PACK` never took effect — see [11.9](#119-domain_pack-is-read-from-the-process-environment) |
| Behaviour contradicts the source you are reading | The running artefact is older than the source — see [11.4](#114-stale-build-artefacts-outrank-correct-source) |

### Windows only

| Symptom | Cause and fix |
|---|---|
| `exec /docker-entrypoint.sh: no such file or directory`, on a file that exists | CRLF line endings — see [11.10](#1110-windows-crlf-endings-break-the-entrypoint-scripts) |
| `: not found` on line 2 or 3 of an entrypoint | Same cause. The `\r` is part of the command name |
| nginx cannot bind, or `ports are not available: :80` | IIS or a Hyper-V reserved range owns port 80 — see [§2 → Windows hosts](#windows-hosts) |
| Celery worker starts, then dies on the first task | Native Windows has no `fork(2)`. Add `--pool=solo`, or run the worker under WSL2 |
| `running scripts is disabled on this system` | Execution policy blocks `Activate.ps1`. `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` |
| `curl: The parameter cannot be found` / curl returns an object | PowerShell 5.1 aliases `curl` to `Invoke-WebRequest`. Call `curl.exe` |
| A bind-mounted directory is empty inside the container | The drive is not shared. Docker Desktop → Settings → Resources → File sharing |
| `/run/secrets/deploy_key` mounts as a directory | The `/dev/null` fallback has no Windows path — set `PHASE_B_HOST_KEY_FILE` |
| Build fails with exit code 137 | Docker Desktop VM is out of memory. Raise Settings → Resources → Memory |
| Everything is inexplicably slow | The tree is on `/mnt/c` from WSL. Clone inside the distro instead |

---

## 11. Deployment pitfalls found in practice

Every item below cost real debugging time on a fresh deployment. They share one
trait: **none of them announce themselves.** The stack reports healthy, the
source looks right, and the behaviour is quietly wrong. Read this before your
first bring-up, not after.

### 11.1 A missing bind-mount source becomes a root-owned directory

Docker does not fail when a bind-mount source is absent — it **creates it, as an
empty directory owned by root**. Two consequences, both misleading:

- If the target was meant to be a **file**, the container refuses to start with
  `not a directory: Are you trying to mount a directory onto a file`.
- If it was meant to be a **directory**, the container starts but cannot write
  to it, because it runs as `appuser` and the directory belongs to root.

The second one surfaces as `RuntimeError: Excel Testcase Engine registration
failed. Fix the engine wiring or set EXCEL_ENGINE_ENABLED=false` — a message
that points at engine wiring when the real cause is directory ownership. The
underlying error is a `PermissionError` on `mkdir` under `ARTIFACTS_DIR`.

Create the bind sources yourself, as your own uid, before the first `up`:

```bash
mkdir -p artifacts knowledge_base workspace logs
```

```powershell
New-Item -ItemType Directory -Force -Path artifacts, knowledge_base, workspace, logs
```

If a root-owned stub already exists you usually cannot delete it in place —
`rmdir` needs write permission on the parent. Move the whole parent aside
(renaming only needs permission on *its* parent) and recreate it:

```bash
mv nginx nginx.root-stale && mkdir nginx     # then restore the real contents
```

```powershell
Move-Item nginx nginx.root-stale; New-Item -ItemType Directory nginx
```

**On Windows this pitfall splits in two.** Docker Desktop exposes
Windows-hosted bind mounts with permissive modes, so the *ownership* symptom
(the container starts but cannot write) usually does not occur on path B —
but the directory is still silently created, so a mount that was meant to
supply a **file** still fails, and a typo in a path still yields an empty
mount rather than an error. From a WSL2-filesystem clone (path A) the Linux
semantics apply exactly as written above.

Also verify that every path named in a compose `volumes:` entry actually
exists in the repo. A stale filename left over from a rename — say a config
file that a commit renamed — produces exactly this failure, and the compose
file still looks correct.

### 11.2 `environment:` silently overrides `env_file:`

Compose merges them by replacement, not by union. A key set in both places
takes the `environment:` value, and the `env_file:` one is discarded with no
warning.

This bites hardest with settings whose value is a **list or a JSON document**:
an override that declares one entry replaces a `.env` line that declared five,
and the four that vanished are exactly the ones nobody thinks to re-check. The
`.env` line still sits there looking authoritative.

After any override change, assert the value the *process* actually has:

```bash
docker compose exec backend printenv <VAR>
```

The same applies to duplicate keys **within** one `.env` file: dotenv keeps the
last assignment. An earlier line is dead, and reads as live.

### 11.3 Live-mounting `app/` without `alembic/` produces a crashloop

A common override live-mounts application code so edits land without a rebuild:

```yaml
volumes:
  - ./backend/app:/app/app
```

The image still carries the migrations that existed when it was built. Once the
database advances past that point, the container cannot find its own head
revision and crashloops on `Can't locate revision identified by '<rev>'`.

Mount them together, or neither:

```yaml
  - ./backend/app:/app/app
  - ./backend/alembic:/app/alembic
  - ./backend/alembic.ini:/app/alembic.ini:ro
```

The general rule: **anything live-mounted must be live-mounted consistently.**
A half-mounted service runs new code against old migrations, old templates, or
old static assets, and the mismatch is silent until it isn't.

### 11.4 Stale build artefacts outrank correct source

Distinct from the above, and more common than expected. Symptoms observed on
real deployments:

| What ran | What was wrong |
|---|---|
| Backend container | The image predated a route that exists in the working tree — every call to it 404'd |
| Frontend bundle | Contained a hardcoded rule that source had already replaced with a config-driven one |
| Frontend container | Was a stock `nginx:alpine` stub substituted by an override, not the project image at all |

In all three the source was correct and the running artefact was not. Backends
can be bind-mounted; **frontends cannot** — they are build-time bundles, so any
change under `frontend/src` or to an nginx template needs:

```bash
docker compose build frontend && docker compose up -d frontend
```

When behaviour contradicts source, verify inside the container before debugging
the code:

```bash
docker compose exec backend grep -c '<a string only the new code has>' /app/app/<file>.py
docker inspect <container> --format '{{.Config.Image}}'
```

### 11.5 CORS allows exactly one origin

The backend sets `allow_origins=[settings.frontend_url]` — a single-entry list,
with no development allowlist and no wildcard. If you reach the UI on any origin
other than `FRONTEND_URL`, every API call is rejected by the browser and the app
appears broken while the backend logs nothing unusual.

Set `FRONTEND_URL` to the origin you actually browse — scheme, host **and port**
— and restart the backend. Changing the published port later means changing this
too. Verify without a browser:

```bash
curl -s -o /dev/null -D - -X OPTIONS http://<origin>/<ctx>/api/auth/me \
  -H 'Origin: http://<origin>' -H 'Access-Control-Request-Method: GET' \
  | grep -i access-control-allow-origin
```

```powershell
(Invoke-WebRequest -Method Options http://<origin>/<ctx>/api/auth/me `
  -Headers @{ 'Origin' = 'http://<origin>'; 'Access-Control-Request-Method' = 'GET' } `
  -UseBasicParsing).Headers['Access-Control-Allow-Origin']
```

### 11.6 A blank SPA page usually means an asset resolved to HTML

`index.html` references its assets **relatively** (`./assets/…`). Served at
`/<ctx>/` that resolves correctly. Refresh on a deep link such as
`/<ctx>/changes/123` and the browser asks for
`/<ctx>/changes/assets/…` instead.

The confusing part: that request often returns **HTTP 200**, not 404, because
the SPA fallback (`try_files … /index.html`) answers with HTML. The browser
expects JavaScript, gets a document, and the module fails to parse — a blank
page with no failed request in the Network tab. Check the *content type*, not
the status code:

```bash
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' http://<host>/<ctx>/assets/<bundle>.js
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' http://<host>/<ctx>/deep/link/assets/<bundle>.js
```

```powershell
'/<ctx>/assets/<bundle>.js', '/<ctx>/deep/link/assets/<bundle>.js' | ForEach-Object {
  $r = Invoke-WebRequest "http://<host>$_" -UseBasicParsing
  "$($r.StatusCode) $($r.Headers['Content-Type'])"
}
```

`text/html` on either line is the failure, whatever the status code says.

The fix is a `<base href="/<ctx>/">` tag so relative URLs anchor at the public
prefix regardless of depth. Inject it at whichever nginx serves the HTML:

```nginx
proxy_set_header Accept-Encoding "";
sub_filter '<head>' '<head><base href="/<ctx>/">';
sub_filter_once on;
sub_filter_types text/html;
```

Clearing `Accept-Encoding` is required, not optional: `sub_filter` cannot
rewrite a gzipped upstream body and will pass it through **unmodified**, so the
fix appears applied and changes nothing.

Related: if a reverse proxy declares `proxy_pass http://upstream:80/` **with a
trailing slash**, nginx strips the matched prefix before forwarding. The
upstream then receives `/` and `/changes/123`, never `/<ctx>/…`. A downstream
config that matches on `location /<ctx>/` will answer 302 on the index and 404
on every deep link. The two configs must agree on who owns the prefix.

### 11.7 nginx resolves every upstream at startup

One unresolvable upstream name is fatal to the **whole** proxy, not just to its
own route — the container exits with `host not found in upstream`. A config
copied from a larger environment will therefore refuse to start until you remove
the upstreams whose services you are not running.

This is also why renaming a container breaks routing: declare the shipped
hostnames as network aliases rather than editing the other side's config.

### 11.8 Background workers are not optional

`celery` and `celery_beat` are easy to omit when starting a subset of services,
and most of the platform keeps working without them. What stops working is
everything time-driven — scheduled sweeps, deadline expiry, retry drains.

The failure mode is not an error. Work that should time out simply never does:
a run stays `running` forever with unreported placeholders, instead of failing
cleanly at its deadline and telling you why. Start them alongside the backend,
and if a long-running job seems stuck, check they are up before debugging the
job.

### 11.9 `DOMAIN_PACK` is read from the process environment

This setting is deliberately **not** a `Settings` field — `app/core/domain/registry.py`
reads `os.environ` directly, because agent modules resolve it at import time.

pydantic-settings populates `Settings`, **not** `os.environ`. So a `DOMAIN_PACK`
line in `backend/.env` is invisible to a **native** run. Under Docker it works
only because compose's `env_file:` injects it into the real container
environment.

A wrong or unset value does not raise — it silently falls back to the default
pack, and the only symptom is generated prose in the wrong domain's vocabulary.
Assert it from outside:

```bash
curl -s http://<host>/api/config/ui | jq .repo_roles
```

```powershell
(Invoke-RestMethod http://<host>/api/config/ui).repo_roles
```

If the pack lives outside the repo, mount it at **the same absolute path it has
on the host**. One value is then correct for both native and containerised runs
— which matters here precisely because this setting cannot be varied per run
mode. Mount it into every service that imports the agent modules — the API and
the workers — or the API speaks one domain while the workers fail to boot.

On Windows that trick cannot hold: `DOMAIN_PACK` is itself the path to the
pack's YAML file, and `C:\packs\aviation\aviation.yaml` is not a path a Linux
container can have. Set `DOMAIN_PACK` to the **container-side** path and accept
that a native Windows run needs a different value than the containerised one —
or keep the pack inside the repository tree, where a relative bind makes the
question moot. Under WSL2 the original advice works unchanged.

### 11.10 Windows: CRLF endings break the entrypoint scripts

The one Windows failure that looks like something else entirely, and the
reason [§2 → Windows hosts](#windows-hosts) asks you to configure Git *before*
cloning.

`nginx/docker-entrypoint.sh` is bind-mounted into the nginx container and
`frontend/docker-entrypoint.sh` is copied into the frontend image. Both begin
`#!/bin/sh`. If Git checked them out with CRLF endings, the kernel reads the
interpreter as `/bin/sh\r`, which does not exist, and the container exits with:

```
exec /docker-entrypoint.sh: no such file or directory
```

naming a file that is plainly there. A shell that gets past the shebang fails
instead on `: not found` at the next line, because `\r` is part of the command
name.

Nothing about the message says "line endings", the file looks correct in every
editor, and `docker compose config` validates. Check the bytes:

```powershell
docker compose exec nginx sh -c "head -1 /docker-entrypoint.sh | od -c | head -1"
# a trailing \r \n instead of a bare \n is the bug
```

The fix is at clone time — `core.autocrlf input` plus the repository's
`.gitattributes`, which pins `*.sh` to LF. To repair a clone that already has
the wrong bytes, renormalise rather than editing by hand:

```powershell
git rm --cached -r .
git reset --hard
docker compose up -d --build frontend nginx
```

The rebuild is not optional: the frontend script is baked into its image, so
correcting the working tree alone changes nothing
([§11.4](#114-stale-build-artefacts-outrank-correct-source)).

---

## Related documents

| Document | Covers |
|---|---|
| [`README.md`](README.md) | Overview, architecture, quick start, capability limits |
| [`FAQ.md`](FAQ.md) | Why questions this guide does not answer |
| [`CONFIGURATION.md`](CONFIGURATION.md) | The full settings reference |
| [`wiki/`](wiki/) | Architecture, workflow phases, retrieval, the evaluation gate |
| [`SECURITY.md`](SECURITY.md) | Reporting a vulnerability, and the security posture |
