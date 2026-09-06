# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 2.5 — Maximal Marginal Relevance diversifier tests.

Pure-Python unit tests; no DB / no network. Cover both `_cosine` and
`mmr_select`, including:
  - basic relevance ordering
  - novelty kicking duplicates out at lambda < 1.0
  - lambda extremes (1.0 = pure relevance; 0.0 = pure novelty)
  - missing / malformed embeddings (fail-open backfill)
  - top_k caps and empty inputs
"""
from __future__ import annotations

import math

import pytest

from app.rag.diversify import _cosine, _coerce_embedding, mmr_select


# ── _cosine ─────────────────────────────────────────────────────────────────

def test_cosine_identical_vectors_is_one():
    assert _cosine([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_vectors_is_zero():
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_opposite_vectors_is_negative_one():
    assert _cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_dim_mismatch_returns_zero():
    # Should NOT raise — falls open to 0.
    assert _cosine([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0


def test_cosine_zero_norm_returns_zero():
    assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_coerce_embedding_pgvector_text_form():
    out = _coerce_embedding("[0.1,0.2,-0.3]")
    assert out == [0.1, 0.2, -0.3]


# ── mmr_select ──────────────────────────────────────────────────────────────

def _chunk(idx: int, emb: list[float]) -> dict:
    return {"id": f"c{idx}", "content": f"chunk {idx}", "embedding": emb}


def test_mmr_empty_inputs():
    assert mmr_select([], [1.0, 0.0], k=5) == []
    assert mmr_select([_chunk(0, [1.0, 0.0])], [1.0, 0.0], k=0) == []


def test_mmr_k_caps_to_pool_size():
    chunks = [_chunk(0, [1.0, 0.0]), _chunk(1, [0.0, 1.0])]
    out = mmr_select(chunks, [1.0, 0.0], k=10)
    assert len(out) == 2


def test_mmr_lambda_one_picks_pure_relevance_order():
    # query is closer to chunk 0 than chunk 1; lambda=1 → pure-relevance.
    query = [1.0, 0.0]
    chunks = [
        _chunk(0, [0.95, 0.05]),  # close to query
        _chunk(1, [0.5, 0.87]),   # farther from query
        _chunk(2, [0.99, 0.01]),  # closest
    ]
    out = mmr_select(chunks, query, k=3, lambda_=1.0)
    # Top picks should be c2 then c0 (relevance ordering ignoring novelty).
    assert out[0]["id"] == "c2"
    assert out[1]["id"] == "c0"


def test_mmr_lambda_below_one_diversifies_near_duplicates():
    # Two near-duplicates close to query, plus one orthogonal-ish chunk.
    # With lambda < 1, the diversifier should jump from one of the dupes
    # to the orthogonal chunk before picking the second dupe.
    query = [1.0, 0.0, 0.0]
    chunks = [
        _chunk(0, [1.0, 0.0, 0.0]),       # exact match
        _chunk(1, [0.999, 0.001, 0.0]),   # near-duplicate of c0
        _chunk(2, [0.6, 0.8, 0.0]),       # less similar to query, orthogonal axis
    ]
    # lambda MUST be < 0.5 here, not == 0.5. c0 is exactly the query vector, so
    # every candidate's novelty penalty equals its relevance and the MMR score
    # collapses to (2*lambda - 1) * relevance — identically 0 for ALL candidates
    # at lambda=0.5. That is a tie broken by iteration order, not by novelty, so
    # 0.5 cannot demonstrate diversification with this fixture.
    out = mmr_select(chunks, query, k=2, lambda_=0.3)
    ids = [c["id"] for c in out]
    # First pick is the most relevant (c0). Second pick should NOT be c1
    # (the near-duplicate) once novelty matters.
    assert ids[0] == "c0"
    assert ids[1] == "c2"


def test_mmr_lambda_zero_purely_diversifies():
    # With lambda=0, after the first pick, we want the chunk most
    # dissimilar to those already selected.
    query = [1.0, 0.0]
    chunks = [
        _chunk(0, [1.0, 0.0]),
        _chunk(1, [0.99, 0.01]),
        _chunk(2, [0.0, 1.0]),
    ]
    out = mmr_select(chunks, query, k=2, lambda_=0.0)
    ids = [c["id"] for c in out]
    # First pick is arbitrary among ties (penalty=0); after that, c2 is
    # the most dissimilar.
    assert ids[1] == "c2"


def test_mmr_missing_embeddings_backfill():
    # MMR ranks the chunks that have embeddings, then backfills from the
    # pool so callers always get up to k rows.
    query = [1.0, 0.0]
    chunks = [
        _chunk(0, [1.0, 0.0]),
        {"id": "c1", "content": "no-emb"},   # no embedding
        _chunk(2, [0.0, 1.0]),
    ]
    out = mmr_select(chunks, query, k=3, lambda_=0.5)
    assert {c["id"] for c in out} == {"c0", "c1", "c2"}


def test_mmr_malformed_query_vec_falls_open():
    chunks = [_chunk(0, [1.0, 0.0]), _chunk(1, [0.0, 1.0])]
    # `query_vec=None` must not raise — fall open to chunks[:k].
    out = mmr_select(chunks, None, k=2, lambda_=0.5)
    assert [c["id"] for c in out] == ["c0", "c1"]


def test_mmr_clamps_lambda_out_of_range():
    chunks = [_chunk(0, [1.0, 0.0]), _chunk(1, [0.5, 0.5])]
    # lambda=2.5 should clamp to 1.0 (pure relevance, no penalty).
    out = mmr_select(chunks, [1.0, 0.0], k=2, lambda_=2.5)
    assert [c["id"] for c in out] == ["c0", "c1"]


def test_mmr_all_chunks_missing_embedding_returns_input_slice():
    chunks = [{"id": "c0", "content": "x"}, {"id": "c1", "content": "y"}]
    out = mmr_select(chunks, [1.0, 0.0], k=1)
    assert len(out) == 1 and out[0]["id"] == "c0"


def test_mmr_does_not_mutate_input_chunks():
    chunks = [_chunk(0, [1.0, 0.0]), _chunk(1, [0.0, 1.0])]
    snapshot = [dict(c) for c in chunks]
    _ = mmr_select(chunks, [1.0, 0.0], k=2, lambda_=0.5)
    # Inputs unchanged (we return references, not copies, but we don't add
    # or remove keys on the originals).
    assert chunks == snapshot
