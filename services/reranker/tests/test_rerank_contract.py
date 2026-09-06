# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Contract tests for the reranker sidecar.

THE POINT OF THESE TESTS. This service exists so that torch and
sentence-transformers can leave the backend image (6 SBOM findings, one at
CVSS 9.8) WITHOUT losing reranking. That only holds if the service speaks
exactly the protocol `backend/app/rag/reranker.py::_rerank_remote` already
expects — the client was NOT modified when this service was introduced, so the
service is the side that has to fit.

Every assertion below therefore mirrors a specific line of client behaviour:

  * the client re-attaches original chunks by a `_key` field it puts in each
    candidate, falling back to matching on `text`. If we drop unknown fields,
    that re-attachment fails, the client discards the result, and reranking
    silently stops working.
  * the client treats `results: []` as "unusable, fall back to RRF order".
  * the client reads `score` from each result and copies it to `rerank_score`.

The model itself is stubbed throughout — these tests must run in CI without
downloading 600 MB of weights, and they are about the CONTRACT, not the
ranking quality of a particular cross-encoder.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app import main as svc  # noqa: E402


class _StubModel:
    """Deterministic stand-in for CrossEncoder.

    Scores by lexical overlap between query and candidate so the expected
    ordering is obvious from the test data rather than from model internals.
    """

    def __init__(self):
        self.calls = 0

    def predict(self, pairs):
        self.calls += 1
        out = []
        for query, text in pairs:
            q = set((query or "").lower().split())
            c = set((text or "").lower().split())
            out.append(float(len(q & c)))
        return out


@pytest.fixture
def client(monkeypatch):
    """A client with the model stubbed in.

    `_load_model` IS PATCHED TO A NO-OP, not merely bypassed. The app loads the
    model eagerly in its lifespan handler (deliberately — see the module
    docstring in app/main.py: it makes the readiness probe truthful), so
    entering TestClient's context manager would otherwise attempt a real
    600 MB HuggingFace download. Setting `_model` alone is not enough for the
    broken-model fixture below, where `_model` is None by design and
    `_load_model` would therefore run for real.
    """
    monkeypatch.setattr(svc, "_load_model", lambda: None, raising=True)
    monkeypatch.setattr(svc, "_model", _StubModel(), raising=False)
    monkeypatch.setattr(svc, "_model_error", "", raising=False)
    with TestClient(svc.app) as c:
        yield c


@pytest.fixture
def broken_client(monkeypatch):
    """A client where the model failed to load — the fail-open path.

    `_load_model` must be stubbed here too: with `_model` set to None, the real
    loader would fire on lifespan startup and spend a minute retrying against
    huggingface.co before failing. (That it DOES fail cleanly and the service
    still serves requests is the behaviour under test — but it should be
    simulated, not performed.)
    """
    monkeypatch.setattr(svc, "_load_model", lambda: None, raising=True)
    monkeypatch.setattr(svc, "_model", None, raising=False)
    monkeypatch.setattr(svc, "_model_error", "OSError: no space left on device",
                        raising=False)
    with TestClient(svc.app) as c:
        yield c


def _body(query="alpha beta", n=3, top_k=3):
    return {
        "query": query,
        "top_k": top_k,
        "candidates": [
            {"text": t, "_key": k, "file_path": "f.py", "source": "code"}
            for k, t in [("k0", "zzz"), ("k1", "alpha beta"), ("k2", "alpha")][:n]
        ],
    }


# ── Health ────────────────────────────────────────────────────────────────────

def test_healthz_reports_model_loaded(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["model_loaded"] is True


def test_healthz_reports_model_failure(broken_client):
    """`model_loaded: false` is the ONLY signal that distinguishes "reranking
    is working" from "reranking is silently off" — the client fails open, so a
    broken model shows up in production only as slightly worse search results.
    This field is what monitoring watches.
    """
    r = broken_client.get("/healthz")
    assert r.status_code == 200
    payload = r.json()
    assert payload["model_loaded"] is False
    assert "no space left" in payload["model_error"]


# ── The scoring contract ─────────────────────────────────────────────────────

def test_results_are_sorted_by_score_descending(client):
    r = client.post("/rerank", json=_body())
    assert r.status_code == 200
    scores = [x["score"] for x in r.json()["results"]]
    assert scores == sorted(scores, reverse=True)
    assert r.json()["results"][0]["_key"] == "k1"    # exact query match wins


def test_round_trip_key_is_preserved(client):
    """THE most important assertion in this file.

    The client builds `_key` per candidate and uses it to re-attach the
    original chunk dict (preserving id, chunk_index, source_file, ...). If this
    service dropped unknown fields — the Pydantic DEFAULT behaviour, which is
    why Candidate sets `extra: allow` — the client would find no match, discard
    every row, log "returned 0 reusable results", and fall back. Reranking
    would appear to work while doing nothing.
    """
    r = client.post("/rerank", json=_body())
    keys = {x["_key"] for x in r.json()["results"]}
    assert keys == {"k0", "k1", "k2"}


def test_arbitrary_extra_fields_survive(client):
    """The client may add fields at any time; the contract is that we echo
    what we are given rather than enumerate it."""
    body = _body()
    body["candidates"][0]["custom_field"] = "preserve-me"
    r = client.post("/rerank", json=body)
    match = [x for x in r.json()["results"] if x["_key"] == "k0"][0]
    assert match["custom_field"] == "preserve-me"


def test_top_k_truncates(client):
    body = _body(top_k=2)
    assert len(client.post("/rerank", json=body).json()["results"]) == 2


def test_latency_ms_is_reported(client):
    """The client logs this field; a missing key would make its debug line
    print None and mask a slow reranker."""
    payload = client.post("/rerank", json=_body()).json()
    assert isinstance(payload["latency_ms"], (int, float))


# ── Fail-open behaviour ──────────────────────────────────────────────────────

def test_missing_model_returns_passthrough_not_an_error(broken_client):
    """A 5xx would make the client wait for, then discard, a failed request on
    every single search. Returning the input order keeps retrieval fast and
    correct-if-unranked, matching the in-process implementation's fail-open
    design.
    """
    r = broken_client.post("/rerank", json=_body())
    assert r.status_code == 200
    payload = r.json()
    assert payload["degraded"] is True
    assert [x["_key"] for x in payload["results"]] == ["k0", "k1", "k2"]
    assert all(x["score"] == 0.0 for x in payload["results"])


def test_predict_exception_is_degraded_not_500(client, monkeypatch):
    """A malformed tensor or an OOM inside torch must not surface as an error
    to the retrieval path."""
    class _Boom:
        def predict(self, pairs):
            raise RuntimeError("CUDA out of memory")
    monkeypatch.setattr(svc, "_model", _Boom(), raising=False)

    r = client.post("/rerank", json=_body())
    assert r.status_code == 200
    assert r.json()["degraded"] is True


def test_score_count_mismatch_is_degraded(client, monkeypatch):
    """Mirrors the client-side guard: a model returning the wrong number of
    scores means the pairing is untrustworthy, so discard rather than
    mis-attribute scores to the wrong chunks."""
    class _Short:
        def predict(self, pairs):
            return [1.0]          # always one score, regardless of input
    monkeypatch.setattr(svc, "_model", _Short(), raising=False)

    payload = client.post("/rerank", json=_body()).json()
    assert payload["degraded"] is True


def test_empty_candidates_returns_empty_results(client):
    payload = client.post("/rerank", json={"query": "q", "candidates": [], "top_k": 5}).json()
    assert payload["results"] == []


def test_zero_top_k_returns_empty_results(client):
    payload = client.post("/rerank", json=_body(top_k=0)).json()
    assert payload["results"] == []


# ── Bounded work (backs the torch DoS-shaped advisories) ─────────────────────

def test_candidate_count_is_capped(client, monkeypatch):
    """The torch advisories that are DoS-shaped need unbounded input. The
    client already truncates to 12; this is the independent server-side ceiling
    so a rogue caller cannot ask for unbounded work.
    """
    monkeypatch.setattr(svc, "MAX_CANDIDATES", 5, raising=False)
    body = {
        "query": "alpha",
        "top_k": 100,
        "candidates": [{"text": f"alpha {i}", "_key": f"k{i}"} for i in range(100)],
    }
    assert len(client.post("/rerank", json=body).json()["results"]) == 5


def test_text_is_truncated_before_scoring(client, monkeypatch):
    """Long inputs are cut before they reach the tokenizer, not after."""
    seen = {}

    class _Recorder:
        def predict(self, pairs):
            seen["len"] = len(pairs[0][1])
            return [1.0] * len(pairs)

    monkeypatch.setattr(svc, "_model", _Recorder(), raising=False)
    monkeypatch.setattr(svc, "MAX_TEXT_CHARS", 50, raising=False)

    client.post("/rerank", json={
        "query": "q", "top_k": 1,
        "candidates": [{"text": "x" * 5000, "_key": "k0"}],
    })
    assert seen["len"] == 50


def test_model_name_is_not_taken_from_the_request(client):
    """THE security control for CVE-2026-68770 (CVSS 9.8, unsafe
    deserialisation of a malicious model artifact).

    The exploit precondition is an attacker choosing which model gets loaded.
    This service reads the model name from the environment only. Sending one in
    the request body must have no effect — asserted here so that a future
    "make the model configurable per request" change fails this test and forces
    a re-triage of that CVE rather than silently making it live.
    """
    before = svc.MODEL_NAME
    body = _body()
    body["model"] = "attacker/evil-model"
    r = client.post("/rerank", json=body)
    assert r.status_code == 200
    assert svc.MODEL_NAME == before
