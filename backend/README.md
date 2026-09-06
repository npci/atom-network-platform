<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)"
            srcset="../frontend/src/assets/ATOM-logo-dark.png">
    <source media="(prefers-color-scheme: light)"
            srcset="../frontend/src/assets/ATOM-logo-light.png">
    <img alt="AtOM"
         src="../frontend/src/assets/ATOM-logo-light.png" width="460">
  </picture>
</p>

<h1 align="center">Platform Backend</h1>

<p align="center">
  The authoring, retrieval and code-generation service — ~104 LLM agents, a hybrid
  retrieval stack, and the A2A server that talks to partner platforms.
</p>

[![Python](https://img.shields.io/badge/Python-3.12-3776AB.svg?style=flat&logo=python&logoColor=white)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.118-009688.svg?style=flat&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0-D71F00.svg?style=flat&logo=sqlalchemy&logoColor=white)](https://www.sqlalchemy.org)
[![Alembic](https://img.shields.io/badge/Alembic-0123%20head-6BA81E.svg?style=flat)](alembic/versions)
[![Celery](https://img.shields.io/badge/Celery-5.4-37814A.svg?style=flat&logo=celery&logoColor=white)](https://docs.celeryq.dev)
[![Tests](https://img.shields.io/badge/Tests-2%2C937-0A9EDC.svg?style=flat&logo=pytest&logoColor=white)](tests)

## Overview

This service owns everything upstream of a partner: turning a prompt into documents, grounding those documents in a real corpus and a real codebase, generating and reviewing code, and driving certification. The receiving side is a peer application in a separate repository — [atom-partner-platform](https://github.com/npci/atom-partner-platform) — reached only across the A2A boundary.

- 🧩 **~104 agents, one module per agent** in `app/agents/` — the workflow phases are these agents orchestrated by LangGraph, not a monolith
- 🔎 **Hybrid retrieval** in `app/rag/` — BM25 + pgvector fused by reciprocal rank, over both an ingested document corpus and tree-sitter-chunked source code
- 📄 **Document pipeline** in `app/docgen/` — a LangGraph `.docx` pipeline for requirements, specifications, circulars and product notes
- 🧾 **Externalised prompts** in `app/prompts/` — 68 prompts live as `.md` files, loaded rather than hardcoded
- 🚦 **Evaluation gate** in `app/services/evaluation/` — judge, critic, grounding and deterministic checks between stages
- 🔐 **A2A server** in `app/a2a_common/` — HMAC envelope, JWT, CIDR; the wire code is **generated**, not hand-edited

## Layout

```
app/
  agents/          ~104 LLM agents (brd, tech_spec, canvas, code_change,
                   adversarial_reviewer, cert_triage, negotiation, …)
  prompts/         Externalised prompt text (.md) — see prompts/README.md
  rag/             Retrieval: hybrid_search, reranker, ingestion, code_ingestion,
                   tree-sitter chunkers, LSP resolvers, symbol graphs
  docgen/          LangGraph .docx pipeline
  kg/              Knowledge-graph impact analysis over the symbol graph
  services/        Non-LLM orchestration: celery_tasks, git_integrator,
                   build_runner, cert_orchestrator, notifications
  services/evaluation/  The quality gate (judge, critic, grounding, deterministic)
  excel_testcase_engine/  Standalone cert-test-case engine (own agents + prompts)
  a2a_common/      A2A wire code — GENERATED from ../packages/a2a-core
  packs/           Domain packs: vocabulary behind an interface
  core/            Config, LLM provider routing, security, database
  api/             FastAPI routers
  models/          SQLAlchemy models
alembic/versions/  Migrations (head: 0123)
tests/             Mirrors app/ — a2a_common, agents, api, core, docgen, eval,
                   golden, kg, rag, services
scripts/           Operator tools (create_user, reset_password, bulk_create_users)
```

## Running

The backend is not run directly — it runs as a Compose service from the repository root.

```bash
docker compose up -d backend                    # start
docker compose up -d --build backend            # after dependency changes
docker compose logs -f backend                  # follow
docker compose run --rm backend alembic upgrade head
```

### Tests

```bash
docker compose run --rm backend pytest                       # all (~2,937)
docker compose run --rm backend pytest tests/rag -x -q       # one subdir
docker compose run --rm backend pytest tests/agents -k fact  # by keyword
```

> **`tests/` is baked into the image, not bind-mounted.** After editing a test, either rebuild (`docker compose build backend`) or `docker cp` the file in — otherwise the container runs the stale copy. Code under `app/` is live only if you have created the gitignored `docker-compose.override.yml` bind mount.

## Conventions that will bite you

These are the ones that fail silently rather than loudly. .

**Never call the `anthropic` / `openai` SDKs from an agent.** Go through `app/core/llm.py` (`call_llm`, `stream_llm`, `call_llm_structured`). It dispatches on `settings.llm_provider` and strips provider-specific features. Model choice is per-agent *purpose* via `llm_router.py`, not a hardcoded model string.

**Never edit `app/a2a_common/{hmac_signer,protocol,executor_base}.py`.** They are generated from `../packages/a2a-core/`; edit there and run `scripts/ci/sync-a2a-core.sh`. The hygiene gate fails on drift. Every service hashes the same wire bytes, so a one-sided edit breaks signatures across a trust boundary.

**Never read a setting with `getattr(settings, "name", default)` unless the field is declared.** `Settings` is `extra="ignore"`, so an undeclared name means the env var is silently dropped and the default always wins — a knob that looks configurable and is not. `tests/core/test_config_no_phantom_knobs.py` enforces this.

**Prompt files end with exactly one trailing newline**, and the loader strips exactly one. Anthropic's prompt cache keys on exact prefix bytes, so a stray byte silently misses the cache on every request. `tests/core/test_prompt_snapshot.py` hashes every prompt and fails if any byte moves.

**Migrations are idempotent and inspector-gated.** Follow the shape in `alembic/versions/0035_a2a_session_revocation.py`: bind, inspect, and only then add. Use `.with_variant(sa.String(...), "sqlite")` for Postgres-native types so the SQLite test harness still builds.

**Dependencies are hash-locked.** The image installs from `requirements.<arch>.lock` with `--require-hashes`; editing `requirements.txt` alone changes nothing. One lock per architecture, because `torch` declares CUDA dependencies on amd64 that do not exist on arm64.

## Configuration

`app/core/config.py` — one flat `Settings` class, sectioned by banner comments. Every field is populated from the environment or `backend/.env`.

The knobs you are most likely to want:

| Setting | Default | Effect |
|---|---|---|
| `LLM_PROVIDER` | `claude` | `claude` \| `openai` \| `ainxt` \| `ollama` |
| `CAPTCHA_ENABLED` | `true` | Login CAPTCHA; turn off only for a local loop |
| `USE_RERANKER` | — | Cross-encoder reranking on retrieval (downloads a model on first use) |
| `GOVERNANCE_REVIEWS_ENABLED` | `false` | Governance review stages |
| `SANDBOX_ENABLED` | — | Docker-in-Docker sandboxed code execution |

`DOMAIN_PACK` (default `network`) is deliberately **not** a `Settings` field — `app/core/domain/registry.py` reads it straight from the environment, because several agents build their system prompts as module-level constants evaluated at import time, before a settings object with a DB or request context would be available.

## Further reading

- [`../wiki/`](../wiki/) — architecture, workflow phases, retrieval and the A2A wire
— conventions and the gotcha index
- [`app/prompts/README.md`](app/prompts/README.md) — how prompts are stored and loaded
- [`tests/eval/README.md`](tests/eval/README.md) — the evaluation harnesses
- `tests/golden/` — the output-quality harness
