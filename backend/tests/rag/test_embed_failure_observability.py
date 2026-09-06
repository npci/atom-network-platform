# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 3 Gap A — observable zero-vector failures.

We can't easily make Ollama fail in unit tests, but we CAN verify the
plumbing: `_record_embed_zero_vector` bumps a counter and `embed_failure_stats`
reports it. The fail-hard mode raises EmbedHardFailure when the env var is set.
"""
from __future__ import annotations

import os

import pytest

from app.rag import embeddings


@pytest.fixture(autouse=True)
def _reset():
    embeddings._reset_embed_failure_counters_for_tests()
    yield
    embeddings._reset_embed_failure_counters_for_tests()


def test_record_zero_vector_increments_counter():
    assert embeddings.embed_failure_stats()["zero_vector_total"] == 0
    embeddings._record_embed_zero_vector("test reason", char_len=42)
    embeddings._record_embed_zero_vector("another", char_len=0)
    assert embeddings.embed_failure_stats()["zero_vector_total"] == 2


def test_fail_hard_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("EMBED_FAIL_HARD", raising=False)
    assert embeddings._embed_fail_hard_enabled() is False


def test_fail_hard_flag_recognises_truthy_values(monkeypatch):
    for val in ("1", "true", "TRUE", "yes"):
        monkeypatch.setenv("EMBED_FAIL_HARD", val)
        assert embeddings._embed_fail_hard_enabled() is True


def test_default_concurrency_within_bounds():
    val = embeddings._default_embed_concurrency()
    assert 8 <= val <= 32
