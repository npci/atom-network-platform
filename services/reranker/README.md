# Cross-encoder reranker sidecar

A one-endpoint HTTP service that scores `(query, passage)` pairs with a
cross-encoder. It exists so that the **backend image does not have to contain
PyTorch**.

## Why this service exists

The A2A compliance SBOM of 2026-08-27 reported 18 policy violations. Six of
them — a third of the report — came from two packages:

| Component | Advisory | CVSS | Policy |
|---|---|---|---|
| sentence-transformers 3.3.1 | CVE-2026-68770 | 9.8 | Security-Critical |
| torch 2.13.0 | CVE-2025-3121 | 5.5 | Security-Medium |
| torch 2.13.0 | CVE-2025-3000 | 5.3 | Security-Medium |
| torch 2.13.0 | CVE-2025-3136 | 4.6 | Security-Medium |
| torch 2.13.0 | CVE-2025-4287 | 3.3 | Security-Low |
| torch 2.13.0 | CVE-2025-2149 | 2.0 | Security-Low |

Both were installed in the backend image even though reranking defaults to
**off**. The platform was carrying the risk without using the feature.

Two obvious responses were both wrong:

- **Delete the reranker.** It is worth an estimated +5–15pp recall@10. Losing
  retrieval quality to satisfy a scanner is a bad trade.
- **Annotate and move on.** The reachability argument is genuinely strong — the
  model name is operator-set, the feature is off by default, and the only API
  touched is `CrossEncoder.predict()`. But a single config flag was all that
  separated "dormant" from "live", and a scanner is right to report what is
  installed rather than what is currently reachable.

So the capability moved instead of disappearing. The backend keeps reranking
via `RERANKER_BACKEND=remote`, an HTTP path that was **already implemented and
already production-proven** — `backend/app/rag/reranker.py::_rerank_remote`
carries a note about payload capping learned in the 2026-05-03 production run.
Nothing in the retrieval pipeline changed. Same model, same scores, different
process.

The findings do not vanish from the world: this service is scanned on its own.
That is the honest outcome, and it is a much better position, because the
advisories are now attached to a container with no database, no user data, no
auth surface and no internet egress — instead of to the component that holds
the entire platform's data.

**Second benefit:** the backend image drops roughly 4 GB. `torch`'s CUDA
closure was what made it 5.4 GB and exhausted the CI runner's disk.

## Running it

```bash
# Start the sidecar (it is behind a compose profile — see below)
docker compose --profile reranker up -d

# Turn reranking on in backend/.env
USE_RERANKER=true
```

`RERANKER_BACKEND` and `RERANKER_URL` are already wired in `docker-compose.yml`
for both the backend and the celery worker, so no other configuration is
needed.

It sits behind a **compose profile** because reranking is off by default —
starting a ~2 GB container with ~1.5 GB resident memory for a disabled feature
would be wasteful.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `RERANKER_MODEL` | `BAAI/bge-reranker-v2-m3` | **Operator-set only.** See the security note below. |
| `RERANKER_MAX_CANDIDATES` | `32` | Server-side ceiling. The client already caps at 12. |
| `RERANKER_MAX_TEXT_CHARS` | `4000` | Truncation before tokenising. |
| `RERANKER_BATCH_SIZE` | `16` | Pairs per `predict()` call. |
| `LOG_LEVEL` | `INFO` | |

### The model name is deliberately not caller-configurable

`CVE-2026-68770` (CVSS 9.8) is an unsafe-deserialisation bug: a malicious model
artifact executes code when loaded. Its precondition is **an attacker choosing
which model gets loaded**. This service reads the model name only from the
environment and ignores any `model` field in the request body, which is what
keeps that CVE unreachable.

`tests/test_rerank_contract.py::test_model_name_is_not_taken_from_the_request`
asserts this, so a future "make the model configurable per request" change
fails the suite and forces a re-triage rather than silently making the CVE
live.

## Failure behaviour: it degrades, it does not break

Reranking is a **quality enhancement, not a correctness requirement**. Every
failure path — model unavailable, `predict()` raising, score-count mismatch —
returns the candidates in the order they arrived with score `0.0`. The client
treats that as "no useful reranking" and keeps its own RRF order.

This mirrors the in-process implementation's long-standing fail-open design and
is why the service returns `200` with `"degraded": true` rather than a `5xx`: a
`5xx` would make every search wait for a failed request before falling back.

**The cost of fail-open is that a broken reranker is invisible.** Search still
works, just slightly worse. Two things make that visible:

1. `GET /healthz` returns `model_loaded: false` with the load error. **Wire
   this to your monitoring** — it is the difference between "reranking is
   working" and "reranking is silently off".
2. `backend/app/core/startup_validation.py::validate_reranker_backend` reports
   at startup if `USE_RERANKER=true` is combined with a backend or URL that
   cannot work.

## Operational notes

- **First boot downloads ~600 MB** of model weights. The `reranker_models`
  volume persists them across container recreation; without it, every restart
  re-downloads. The healthcheck allows a 300-second `start_period` for this.
- **Air-gapped deployments** should pre-bake the weights — uncomment the
  `PREFETCH_MODEL` block in the `Dockerfile` and build with
  `--build-arg PREFETCH_MODEL=true`. Otherwise there is no internet at run time
  and the reranker will never load.
- **Scale with containers, not workers.** Each uvicorn worker loads its own
  copy of the model, so `--workers 4` quadruples memory for no throughput gain.
  The cross-encoder is CPU-bound and torch already parallelises internally.

## Testing

```bash
python -m pytest services/reranker/tests -q
```

The model is stubbed throughout, so the suite runs in about a second and needs
no network. The tests assert the **wire contract** the existing backend client
expects — particularly that unknown fields such as `_key` round-trip
untouched, since the client uses that key to re-attach the original chunk and
would silently discard every result if it were dropped.
