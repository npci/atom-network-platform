# Configuration Guide — AtOM

How AtOM is configured, which layer wins when two disagree, and what each group
of settings controls.

**`backend/.env.example` is the authoritative reference.** All ~107 settings are
documented there, in place, next to the value they govern — that is deliberate,
because a separate list drifts from the code the moment someone adds a knob.
This document explains the *model*: where configuration comes from, what
overrides what, and which groups matter for which task. It does not restate
every variable.

- [The three layers](#the-three-layers)
- [Secrets](#secrets)
- [Where to change what](#where-to-change-what)
- [Settings by group](#settings-by-group)
- [Adding a setting](#adding-a-setting)
- [Environment-specific behaviour](#environment-specific-behaviour)

---

## The three layers

Configuration arrives from three places. When they disagree, this is the order:

```
   1.  Admin → Configuration   (database)        ← WINS at runtime
              ↑
   2.  docker-compose.yml  environment:          ← wins over .env
              ↑
   3.  backend/.env                              ← the bootstrap default
```

### 1. The database — Admin → Configuration

Operator-tunable settings and **all secrets** are owned by the database and
edited in the admin UI at runtime. That is the single edit surface once the
platform is running:

- AI provider and model selection
- GitLab, SMTP, Jenkins and UAT endpoints
- Every API key, token and password

Values here are read through `app_config_sync` and win over anything in the
environment. A value set in the UI stays set across restarts and redeploys.

### 2. Compose `environment:`

`DATABASE_URL`, `REDIS_URL` and `OLLAMA_URL` are set explicitly under each
service in `docker-compose.yml`, so the in-network service names resolve inside
the containers. **These override `backend/.env`.** Setting `DATABASE_URL` in
`.env` and wondering why it has no effect under Docker is the usual first
encounter with this layer.

A native install has no such override, which is why it must set all three
explicitly — the defaults point at container hostnames that do not exist outside
the Compose network.

### 3. `backend/.env`

First-run bootstrap defaults, and the permanent home of the settings the admin
UI deliberately does not expose:

- Infrastructure: the three URLs above, storage paths, CORS and A2A URLs
- `PHASE_B_*` — the code-generation runner
- `USE_*` and `AGENTIC_*` feature flags

There is also a **root `.env`**, which is a different thing again: it is read by
Docker Compose for *variable interpolation* into `docker-compose.yml`, not
passed to any container. It holds `POSTGRES_PASSWORD`, `REDIS_PASSWORD` and
`CERTSIM_INTERNAL_TOKEN`. See [`.env.example`](.env.example).

---

## Secrets

### `CONFIG_ENCRYPTION_KEY`

A Fernet key that encrypts, in the same `enc:v1:` on-disk format:

- every secret in the `app_configs` table — `*_API_KEY`, `GITLAB_TOKEN`,
  `SMTP_PASSWORD`, `JENKINS_TOKEN`
- every credential on `partner_agents` — `api_key`, `jwt_signing_secret`,
  `signing_secret`, and their `previous_*` rotation-grace copies

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

It is **the one secret that must live in `.env`** — database-stored secrets
cannot be bootstrapped without it. Outside development, storing a secret with it
unset is refused rather than silently written in the clear.

### `SECRET_KEY`

Signs operator session JWTs. Long and random; a weak value here is a forgeable
session.

### Rotation

Partner credentials rotate through
`POST /api/admin/partners/{id}/rotate-hmac-secret`, which keeps the previous
value for a grace period so in-flight calls do not fail. A scheduled Celery task
(`scan_partner_secret_ages_task`) logs a warning per partner whose credentials
were last rotated more than 90 days ago. That threshold is a task parameter
rather than a setting — adjust it in the beat schedule if your policy differs.

---

## Where to change what

| I want to… | Change it… |
|---|---|
| Switch LLM provider or model | Admin → Configuration |
| Add or rotate an API key | Admin → Configuration |
| Point at a different GitLab / SMTP / Jenkins | Admin → Configuration |
| Change the database or Redis address | `docker-compose.yml`, or `backend/.env` natively |
| Turn a feature flag on or off | `backend/.env`, then restart |
| Change the datastore passwords | root `.env`, then recreate the datastores |
| Change the Phase B runner mode | `backend/.env` |
| Change the active domain pack | `DOMAIN_PACK` in the environment |

Feature flags and infrastructure need a restart:

```bash
docker compose restart backend celery celery_beat
```

Admin → Configuration changes do not.

---

## Settings by group

The groups below match the section banners in `backend/.env.example`. Read that
file for the full list and the reasoning attached to each value.

| Group | What it governs | Notable |
|---|---|---|
| **Application** | `APP_NAME`, `APP_ENV`, `SECRET_KEY`, session TTL | `APP_ENV` is load-bearing — see below |
| **Secret encryption** | `CONFIG_ENCRYPTION_KEY` | Required before any secret can be stored |
| **Database / Redis** | Connection URLs, pool sizing | Overridden by Compose under Docker |
| **Authentication & login hardening** | CAPTCHA, lockout thresholds, cookie behaviour | `AUTH_RETURN_TOKEN_IN_BODY` should stay `false` |
| **LDAP / Active Directory** | Hybrid directory auth | Optional; local accounts work without it |
| **LLM provider** | `LLM_PROVIDER`, per-provider keys, model routing | `claude` \| `openai` \| `ainxt` \| `ollama` |
| **Embeddings** | Ollama URL and model | Needs `nomic-embed-text` pulled |
| **Email** | SMTP for approval notifications | Optional |
| **Storage paths** | Artifacts, knowledge base, workspace | Bind-mounted in Compose |
| **CORS** | Permitted browser origins | Must match the front door |
| **GitLab integration** | Token and project mapping | Only for Phase B code-change features |
| **Feature flags (RAG)** | Chunking, retrieval, reranking | `USE_RERANKER` needs the sidecar profile |
| **Docgen diagrams** | Mermaid rendering | Requires `INSTALL_MERMAID=true` at build |
| **Sandbox / code-change loop** | Docker-in-Docker execution | Refuses rather than running unisolated |
| **Agentic codegen** | Branch prefix, budgets, replay, concurrency | `PHASE_B_RUNNER_MODE` defaults to simulated |
| **Scheduled ingest** | Celery beat sweep interval | |
| **Precert engine** | Certification simulator database | **Off by default** |
| **Cert-agent** | `X-Internal-Token` contract | Must match the receiving service byte for byte |
| **SSRF guard** | Allowed outbound hosts | Operator-supplied URLs are validated against it |
| **Cleartext transport policy** | CWE-319 enforcement | Refuses `http://` for credential-bearing URLs |

### The flags most often changed for local work

| Variable | Default | Effect |
|---|---|---|
| `CAPTCHA_ENABLED` | `true` | Turn off for a local loop only |
| `DEV_SKIP_APPROVALS` | `true` in the template | Auto-approves the BRD so the workflow is unblocked without reviewer accounts. **Refused when `APP_ENV=production`** |
| `USE_RERANKER` | `false` | Needs `docker compose --profile reranker up -d` |
| `GOVERNANCE_REVIEWS_ENABLED` | `false` | Governance review stages |
| `PHASE_B_RUNNER_MODE` | `demo` | Fully simulated, and says so in every log line |

---

## Adding a setting

**Declare it as a field on `Settings`.** The class is `extra="ignore"`, so
reading an undeclared name with `getattr(settings, "name", default)` means the
environment variable is silently dropped and the default always wins — a knob
that looks configurable and is not. This has caught people out, so
`backend/tests/core/test_config_no_phantom_knobs.py` now enforces it and will
fail the suite.

`DOMAIN_PACK` is the deliberate exception: `app/core/domain/registry.py` reads it
straight from the environment, because several agents build their system prompts
as module-level constants evaluated at import time, before a settings object
with a database or request context exists.

Document the new setting in `backend/.env.example` in the same commit, in the
section it belongs to. That file is the reference; a setting absent from it is
undiscoverable.

---

## Environment-specific behaviour

`APP_ENV` is not cosmetic. Setting it to `production` changes real behaviour:

| Behaviour | `development` | `production` |
|---|---|---|
| Session cookie `Secure` flag | off | **on** |
| HSTS header | off | **on** |
| API docs (`/api/docs`, `/api/redoc`, OpenAPI) | served | **disabled** |
| `DEV_SKIP_APPROVALS` | honoured | **refused** |
| Storing a secret with no `CONFIG_ENCRYPTION_KEY` | warned | **refused** |
| `A2A_REQUIRE_HMAC_FOR_ACTIVE_PARTNERS=false` | permitted | **critical startup failure** |
| Weak `SECRET_KEY` | warned | **startup failure** |

Several of those are hard failures by design: a misconfiguration that silently
weakens a security control is worse than a refused start, because the weakened
control still *looks* configured.

Startup validation runs the whole set and reports every issue at once rather
than the first, so a single restart surfaces the full remediation list. See
`backend/app/core/startup_validation.py`.

---

## Related documents

| Document | Covers |
|---|---|
| [`DEPLOYMENT_GUIDE.md`](DEPLOYMENT_GUIDE.md) | Installation, Docker and native, and production hardening |
| [`backend/.env.example`](backend/.env.example) | Every setting, documented in place |
| [`.env.example`](.env.example) | The root file Compose interpolates |
| [`FAQ.md`](FAQ.md) | Why two env files, and other why questions |
| [`SECURITY.md`](SECURITY.md) | The security posture these settings enforce |
