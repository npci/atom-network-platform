# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Cross-encoder reranker sidecar.

Exposes the `/rerank` contract that `backend/app/rag/reranker.py::_rerank_remote`
already speaks — this service was written to fit the EXISTING client, not the
other way round, so no backend retrieval logic changed when it was introduced.

    POST /rerank
      {"query": str, "candidates": [{"text": str, "_key": str, ...}], "top_k": int}
    ->
      {"results": [{"text": str, "score": float, "_key": str, ...}],
       "latency_ms": float}

WHY THIS SERVICE EXISTS (SBOM findings 2, 12, 13, 14, 17, 18). `torch` and
`sentence-transformers` carry six advisories between them, including a CVSS 9.8.
They were installed in the backend image while the reranker defaulted to OFF —
so the platform carried the risk without using the feature. Rather than drop
reranking (worth +5-15pp recall@10), the model stack moved out of the backend
into this one-endpoint container. The backend sets `reranker_backend=remote`
and gets identical results over HTTP.

DESIGN NOTES

* **Fail-open, server-side.** The client already falls back to RRF order on any
  HTTP or parse error, and it treats `results: []` as "fall back". This service
  matches that contract: if the model cannot load, it returns the candidates in
  the order received with score 0.0 rather than a 5xx. Retrieval degrades to
  un-reranked instead of failing. That is the same fail-open philosophy the
  in-process backend has always had (`_LOAD_FAILED` sentinel).

* **Eager model load at startup, not on first request.** The opposite of the
  in-process design, and deliberately so. In-process, a lazy load was necessary
  because the ~600 MB download would otherwise happen during `pip install`-time
  or block an unrelated request; it needed a 45 s deadline and a fail-open path
  (see `_get_model` in the backend). Here the container has one job, so loading
  at startup means the readiness probe tells the orchestrator the truth: the
  pod is not ready until the model is actually usable. No request ever pays the
  cold-start cost, and no request-path deadline is needed.

* **The model name is NOT accepted from the request.** It comes from
  `RERANKER_MODEL` in the environment, operator-set. This is the control that
  makes CVE-2026-68770 (unsafe deserialisation of a malicious model artifact)
  unreachable: an attacker who can reach this endpoint still cannot choose what
  gets loaded. If a future change ever accepts a caller-supplied model name,
  that CVE becomes live and this comment is the reason it must not.

* **Bounded work per request.** `MAX_CANDIDATES` and `MAX_TEXT_CHARS` cap the
  work a single call can demand. The torch advisories that are DoS-shaped need
  unbounded or malformed input; the cross-encoder here sees at most 32 strings
  of at most 4000 chars, truncated before they reach the tokenizer.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("reranker")

# Operator-configured, never caller-configured. See the module docstring —
# this is the control that keeps CVE-2026-68770 unreachable.
MODEL_NAME = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")

# Upper bounds on per-request work. The backend client already truncates to 12
# candidates / 2000 chars; these are the independent server-side ceiling, set
# higher so the client stays the tuning point but a rogue caller still cannot
# ask for unbounded work.
MAX_CANDIDATES = int(os.getenv("RERANKER_MAX_CANDIDATES", "32"))
MAX_TEXT_CHARS = int(os.getenv("RERANKER_MAX_TEXT_CHARS", "4000"))
BATCH_SIZE = int(os.getenv("RERANKER_BATCH_SIZE", "16"))

_model: Any = None
_model_error: str = ""
_model_lock = threading.RLock()


def _load_model() -> None:
    """Load the cross-encoder once, at startup.

    Records the failure reason instead of raising: a service that is up and
    honestly reporting "model unavailable" on /healthz lets the caller fail
    open, whereas a crash-looping container makes every retrieval call wait for
    a connection timeout first.
    """
    global _model, _model_error
    with _model_lock:
        if _model is not None:
            return
        try:
            # Imported here, not at module scope, so that `--help`, a bare
            # import, or a unit test that stubs the model never pays the
            # multi-second torch import.
            from sentence_transformers import CrossEncoder
            t0 = time.perf_counter()
            logger.info("Loading cross-encoder %r (first run downloads ~600MB)", MODEL_NAME)
            _model = CrossEncoder(MODEL_NAME)
            logger.info("Model %r ready in %.1fs", MODEL_NAME, time.perf_counter() - t0)
        except Exception as exc:  # noqa: BLE001 — reported via /healthz, not raised
            # Type name only — /healthz returns this field, so the full message
            # would put loader paths and library internals in a response body.
            _model_error = type(exc).__name__
            logger.error(
                "Cross-encoder %r FAILED to load. Service will answer /rerank "
                "with pass-through order so callers degrade instead of erroring.",
                MODEL_NAME, exc_info=True,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_model()
    yield


app = FastAPI(
    title="Cross-encoder reranker",
    description=(
        "Sidecar for AtOM retrieval. Holds the torch / "
        "sentence-transformers stack so the backend image does not."
    ),
    lifespan=lifespan,
)


class Candidate(BaseModel):
    """One retrieval candidate.

    `model_config` allows extra fields because the backend round-trips its own
    keys through this service — notably `_key`, which it uses to re-attach the
    original chunk dict (preserving `id`, `chunk_index`, etc.) after scoring.
    Dropping unknown fields would break that re-attachment and the client would
    correctly treat the response as unusable.
    """
    model_config = {"extra": "allow"}

    text: str = ""


class RerankRequest(BaseModel):
    query: str = ""
    candidates: list[Candidate] = Field(default_factory=list)
    top_k: int = 10


@app.get("/healthz")
def healthz() -> dict:
    """Liveness + readiness.

    `model_loaded: false` is a REAL signal, not a formality — it is how an
    operator distinguishes "reranking is silently off" from "reranking is
    working". The backend's fail-open behaviour means a broken model here shows
    up only as slightly worse search results, so this endpoint is the place
    that tells the truth. Wire it to your monitoring.
    """
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "model_loaded": _model is not None,
        "model_error": _model_error,
    }


@app.post("/rerank")
def rerank(req: RerankRequest) -> dict:
    """Score (query, candidate) pairs and return them in descending score order.

    Never raises to the caller. On any failure the candidates come back in the
    order they arrived with score 0.0, which the client treats as "no useful
    reranking" and falls back to its own RRF order.
    """
    t0 = time.perf_counter()
    incoming = req.candidates[:MAX_CANDIDATES]

    def _passthrough(reason: str) -> dict:
        logger.warning("Pass-through (%s) — returning input order", reason)
        return {
            "results": [
                {**c.model_dump(), "score": 0.0} for c in incoming[: req.top_k]
            ],
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
            "degraded": True,
            "reason": reason,
        }

    if not incoming or req.top_k <= 0:
        return {
            "results": [],
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
        }

    if _model is None:
        return _passthrough(_model_error or "model not loaded")

    pairs = [(req.query or "", (c.text or "")[:MAX_TEXT_CHARS]) for c in incoming]
    scores: list[float] = []
    try:
        for start in range(0, len(pairs), BATCH_SIZE):
            batch_scores = _model.predict(pairs[start : start + BATCH_SIZE])
            if hasattr(batch_scores, "tolist"):
                batch_scores = batch_scores.tolist()
            scores.extend(float(s) for s in batch_scores)
    except Exception as exc:  # noqa: BLE001 — degrade, never 5xx
        # Type name only. `str(exc)` from torch/sentence-transformers carries
        # filesystem paths and library internals, and `reason` is returned in
        # the response body — the same leak class the platform's
        # client_safe_detail() exists to prevent. Full detail goes to the log.
        logger.warning("predict failed", exc_info=True)
        return _passthrough(f"predict failed: {type(exc).__name__}")

    if len(scores) != len(incoming):
        return _passthrough(f"score count {len(scores)} != candidate count {len(incoming)}")

    scored = [
        {**c.model_dump(), "score": s} for c, s in zip(incoming, scores)
    ]
    scored.sort(key=lambda r: r["score"], reverse=True)

    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    logger.debug(
        "Reranked %d candidates -> top %d in %.1fms (best=%.4f)",
        len(scored), min(req.top_k, len(scored)), latency_ms,
        scored[0]["score"] if scored else 0.0,
    )
    return {"results": scored[: req.top_k], "latency_ms": latency_ms}
