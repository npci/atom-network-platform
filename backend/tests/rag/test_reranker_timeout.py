# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The local reranker must NEVER hang the agent loop on a stalled model load — a slow
load fails open to RRF order within the deadline (§ retrieval robustness)."""
import time

import pytest

from app.rag import reranker
from app.core.config import settings


@pytest.fixture(autouse=True)
def _reset_model_cache():
    reranker._model = None
    yield
    reranker._model = None


def test_load_fails_open_on_hang(monkeypatch):
    # Simulate the real failure: the 600MB download stalls (load never returns).
    monkeypatch.setattr(settings, "reranker_load_timeout_s", 1)
    monkeypatch.setattr(reranker, "_load_cross_encoder", lambda: time.sleep(8))
    t0 = time.monotonic()
    model = reranker._get_model()
    elapsed = time.monotonic() - t0
    assert model is None                          # fell open, did not return a model
    assert elapsed < 3                            # returned at ~1s deadline, NOT after 8s
    assert reranker._model is reranker._LOAD_FAILED  # disabled for the process; no re-hang


def test_load_returns_model_when_fast(monkeypatch):
    monkeypatch.setattr(settings, "reranker_load_timeout_s", 5)
    sentinel = object()
    monkeypatch.setattr(reranker, "_load_cross_encoder", lambda: sentinel)
    assert reranker._get_model() is sentinel


def test_load_fails_open_on_error(monkeypatch):
    monkeypatch.setattr(settings, "reranker_load_timeout_s", 5)
    def _boom():
        raise RuntimeError("weights unavailable")
    monkeypatch.setattr(reranker, "_load_cross_encoder", _boom)
    assert reranker._get_model() is None
    assert reranker._model is reranker._LOAD_FAILED
