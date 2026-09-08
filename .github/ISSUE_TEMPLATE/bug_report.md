---
name: Bug report
about: Something behaves differently from how it is documented
title: ''
labels: bug
assignees: ''
---

<!--
SECURITY: do not report vulnerabilities here. Issues are public.
Follow SECURITY.md instead.
-->

## What happened

<!-- Observed behaviour. Paste exact error text if it is short. -->

## What you expected

<!-- And where that expectation comes from — a doc, a docstring, a test. -->

## Reproduction

1.
2.
3.

## Environment

| | |
|---|---|
| Commit / tag | |
| Alembic head | `docker compose run --rm backend alembic current` |
| LLM provider | `claude` / `openai` / `ainxt` / `ollama` |
| Domain pack | `DOMAIN_PACK` (default `network`) |
| Deployment | compose / native / other |

## Already checked

<!-- The stack has failure modes that look like bugs and are not. Please rule
     out the ones that apply — README "Troubleshooting" covers each: -->

- [ ] Restarted nginx after recreating a backend (it caches upstream DNS; a 502 after a rebuild is usually this)
- [ ] An edit under `backend/app/**` is actually live (`app/` is baked into the image without a `docker-compose.override.yml` bind mount)
- [ ] An edited test is actually live (`backend/tests/` is baked, not mounted)
- [ ] The corpus is populated and the embedding model pulled, if this is a retrieval problem

## Logs

<!-- Redact partner identifiers, tokens and personal data before pasting. -->

```
```
