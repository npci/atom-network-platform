# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for Slice 18 — doc↔code linker (pure helpers + orchestrator with DI).

No real DB, no real LLM. Candidate-finding, scoring, and upserting are all
injected callables so the orchestrator runs fully in-process.
"""
from __future__ import annotations

import pytest

from app.rag import doc_code_linker as linker
from app.rag.doc_code_linker import SymbolCandidate


# ──────────────────────────────────────────────────────────────────────────────
# _extract_symbol_mentions — pure parser
# ──────────────────────────────────────────────────────────────────────────────

class TestExtractSymbolMentions:
    def test_captures_dotted_class_method(self):
        text = "The rate limiter calls RateLimiter.acquire() to check the cap."
        mentions = linker._extract_symbol_mentions(text)
        assert "RateLimiter" in mentions
        assert "acquire" in mentions

    def test_captures_camelcase_class_name(self):
        text = "See PaymentRetryController for the retry logic."
        mentions = linker._extract_symbol_mentions(text)
        assert "PaymentRetryController" in mentions

    def test_captures_backtick_identifier_with_parens(self):
        text = "Call `validate()` before proceeding; also see `TenantContext`."
        mentions = linker._extract_symbol_mentions(text)
        assert "validate" in mentions
        assert "TenantContext" in mentions

    def test_deduplicates_preserving_order(self):
        text = "RateLimiter.acquire() is called. Then RateLimiter.release(). Again RateLimiter."
        mentions = linker._extract_symbol_mentions(text)
        # RateLimiter appears first; acquire second; release third.
        assert mentions[0] == "RateLimiter"
        assert "acquire" in mentions
        assert "release" in mentions
        # No duplicate RateLimiter entries
        assert mentions.count("RateLimiter") == 1

    def test_empty_input(self):
        assert linker._extract_symbol_mentions("") == []

    def test_plain_prose_no_mentions(self):
        assert linker._extract_symbol_mentions("this is a plain sentence with no identifiers.") == []


# ──────────────────────────────────────────────────────────────────────────────
# _parse_confidence — pure parser
# ──────────────────────────────────────────────────────────────────────────────

class TestParseConfidence:
    def test_plain_float(self):
        assert linker._parse_confidence("0.87") == 0.87
        assert linker._parse_confidence("1.0") == 1.0
        assert linker._parse_confidence("0") == 0.0

    def test_json_with_confidence_key(self):
        assert linker._parse_confidence('{"confidence": 0.72}') == 0.72
        assert linker._parse_confidence('{"confidence":0.9}') == 0.9

    def test_json_with_trailing_text(self):
        assert linker._parse_confidence('{"confidence": 0.55}\nExplanation...') == 0.55

    def test_empty_input(self):
        assert linker._parse_confidence("") == 0.0
        assert linker._parse_confidence(None or "") == 0.0

    def test_pure_prose_returns_zero(self):
        assert linker._parse_confidence("high confidence") == 0.0
        assert linker._parse_confidence("I think it's quite likely") == 0.0

    def test_percent_style_values(self):
        """LLM sometimes writes "80" thinking "80%"."""
        assert linker._parse_confidence("80") == 0.80
        assert linker._parse_confidence("100") == 1.0

    def test_clamps_above_one_percent(self):
        assert linker._parse_confidence("150") == 1.0   # caps at 1
        assert linker._parse_confidence("9999") == 1.0

    def test_negative_clamped_to_zero(self):
        assert linker._parse_confidence("-0.5") == 0.0


# ──────────────────────────────────────────────────────────────────────────────
# score_link — mocked LLM
# ──────────────────────────────────────────────────────────────────────────────

class TestScoreLink:
    @pytest.mark.asyncio
    async def test_returns_parsed_confidence(self):
        async def fake_llm(system, messages):
            return '{"confidence": 0.82}'

        conf = await linker.score_link(
            "The rate limiter enforces per-tenant quotas.",
            "public class RateLimiter { ... }",
            call_llm_fn=fake_llm,
        )
        assert conf == 0.82

    @pytest.mark.asyncio
    async def test_empty_doc_returns_zero(self):
        async def fake_llm(system, messages):
            return '{"confidence": 0.9}'
        conf = await linker.score_link("", "class X {}", call_llm_fn=fake_llm)
        assert conf == 0.0

    @pytest.mark.asyncio
    async def test_empty_symbol_returns_zero(self):
        async def fake_llm(system, messages):
            return '{"confidence": 0.9}'
        conf = await linker.score_link("some doc", "", call_llm_fn=fake_llm)
        assert conf == 0.0

    @pytest.mark.asyncio
    async def test_llm_exception_returns_zero(self):
        async def fake_llm(system, messages):
            raise RuntimeError("LLM down")
        conf = await linker.score_link("doc", "code", call_llm_fn=fake_llm)
        assert conf == 0.0

    @pytest.mark.asyncio
    async def test_non_string_response_returns_zero(self):
        async def fake_llm(system, messages):
            return 42
        conf = await linker.score_link("doc", "code", call_llm_fn=fake_llm)
        assert conf == 0.0

    @pytest.mark.asyncio
    async def test_prose_response_returns_zero(self):
        async def fake_llm(system, messages):
            return "I'd say about high, maybe very high."
        conf = await linker.score_link("doc", "code", call_llm_fn=fake_llm)
        assert conf == 0.0


# ──────────────────────────────────────────────────────────────────────────────
# link_chunks — orchestrator with DI
# ──────────────────────────────────────────────────────────────────────────────

def _make_candidate(symbol_chunk_id: str, name: str, content: str = "...",
                    kind: str = "class", source_file: str = "X.java") -> SymbolCandidate:
    return SymbolCandidate(
        symbol_chunk_id=symbol_chunk_id,
        symbol_name=name,
        symbol_kind=kind,
        source_file=source_file,
        content=content,
    )


class TestLinkChunks:
    @pytest.mark.asyncio
    async def test_happy_path_writes_high_confidence_edges(self):
        doc_chunks = [
            ("doc-1", "RateLimiter.acquire() enforces per-tenant quotas."),
        ]

        def find_candidates(doc_id, doc_content):
            return [
                _make_candidate("sym-1", "RateLimiter"),
                _make_candidate("sym-2", "PaymentRetryController"),
                _make_candidate("sym-3", "UnrelatedClass"),
            ]

        async def score_link(doc_content, symbol_content):
            # First candidate is a strong match; others are weak.
            if "RateLimiter" in symbol_content or True:
                return 0.9 if "RateLimiter" == symbol_content else 0.3
            return 0.0

        # Simpler scorer: match by candidate ordering
        scores = iter([0.9, 0.55, 0.2])

        async def score_link2(doc_content, symbol_content):
            return next(scores)

        written: list[tuple[str, str, float]] = []

        def upsert(doc_id, sym_id, conf):
            written.append((doc_id, sym_id, conf))

        report = await linker.link_chunks(
            doc_chunks=doc_chunks,
            find_candidates_fn=find_candidates,
            score_link_fn=score_link2,
            upsert_edge_fn=upsert,
            min_confidence=0.6,
        )

        # Only the 0.9-score candidate makes the cut.
        assert report.edges_written == 1
        assert written == [("doc-1", "sym-1", 0.9)]
        assert report.candidates_considered == 3
        assert report.edges_skipped_low_confidence == 2
        assert report.per_doc_edge_counts["doc-1"] == 1

    @pytest.mark.asyncio
    async def test_max_candidates_per_doc_caps_scoring_calls(self):
        def find_candidates(doc_id, doc_content):
            # Return 10 candidates; orchestrator should only score the first 3.
            return [_make_candidate(f"sym-{i}", f"Class{i}") for i in range(10)]

        call_count = {"n": 0}

        async def score_link(doc_content, symbol_content):
            call_count["n"] += 1
            return 0.9

        written: list = []

        def upsert(doc_id, sym_id, conf):
            written.append(sym_id)

        report = await linker.link_chunks(
            doc_chunks=[("doc-1", "content")],
            find_candidates_fn=find_candidates,
            score_link_fn=score_link,
            upsert_edge_fn=upsert,
            min_confidence=0.5,
            max_candidates_per_doc=3,
        )
        assert call_count["n"] == 3
        assert report.candidates_considered == 3
        assert report.edges_written == 3

    @pytest.mark.asyncio
    async def test_find_candidates_exception_skips_doc_continues(self):
        def find_candidates(doc_id, doc_content):
            if doc_id == "doc-bad":
                raise RuntimeError("DB down")
            return [_make_candidate("sym-1", "X")]

        async def score_link(doc_content, symbol_content):
            return 0.9

        written: list = []

        def upsert(doc_id, sym_id, conf):
            written.append((doc_id, sym_id))

        report = await linker.link_chunks(
            doc_chunks=[("doc-bad", "x"), ("doc-good", "y")],
            find_candidates_fn=find_candidates,
            score_link_fn=score_link,
            upsert_edge_fn=upsert,
            min_confidence=0.5,
        )
        # doc-bad skipped; doc-good produced one edge.
        assert report.docs_processed == 2
        assert report.edges_written == 1
        assert written == [("doc-good", "sym-1")]

    @pytest.mark.asyncio
    async def test_score_link_exception_counts_as_skipped(self):
        def find_candidates(doc_id, doc_content):
            return [_make_candidate("sym-1", "X")]

        async def score_link(doc_content, symbol_content):
            raise ConnectionError("LLM socket closed")

        written: list = []

        def upsert(doc_id, sym_id, conf):
            written.append(sym_id)

        report = await linker.link_chunks(
            doc_chunks=[("doc-1", "x")],
            find_candidates_fn=find_candidates,
            score_link_fn=score_link,
            upsert_edge_fn=upsert,
        )
        assert report.edges_skipped_llm_error == 1
        assert report.edges_written == 0
        assert written == []

    @pytest.mark.asyncio
    async def test_upsert_exception_counted_as_not_written(self):
        def find_candidates(doc_id, doc_content):
            return [_make_candidate("sym-1", "X")]

        async def score_link(doc_content, symbol_content):
            return 0.9

        def upsert(doc_id, sym_id, conf):
            raise RuntimeError("unique constraint")

        report = await linker.link_chunks(
            doc_chunks=[("doc-1", "x")],
            find_candidates_fn=find_candidates,
            score_link_fn=score_link,
            upsert_edge_fn=upsert,
            min_confidence=0.5,
        )
        assert report.edges_written == 0

    @pytest.mark.asyncio
    async def test_empty_doc_chunks_list_returns_empty_report(self):
        async def score_link(doc_content, symbol_content):
            return 1.0

        report = await linker.link_chunks(
            doc_chunks=[],
            find_candidates_fn=lambda *a: [],
            score_link_fn=score_link,
            upsert_edge_fn=lambda *a: None,
        )
        assert report.docs_processed == 0
        assert report.edges_written == 0

    @pytest.mark.asyncio
    async def test_idempotency_contract_upsert_called_per_pair(self):
        """We can't test DB-level idempotency here, but we can assert the
        orchestrator calls upsert once per (doc, symbol) pair per pass — the
        DB's unique constraint converts repeat invocations into UPDATEs."""
        def find_candidates(doc_id, doc_content):
            return [_make_candidate("sym-1", "X")]

        async def score_link(doc_content, symbol_content):
            return 0.8

        call_count = {"n": 0}

        def upsert(doc_id, sym_id, conf):
            call_count["n"] += 1

        # Same doc, two passes. Each should call upsert once; the DB layer
        # (tested separately via integration) handles the idempotent merge.
        for _ in range(2):
            await linker.link_chunks(
                doc_chunks=[("doc-1", "x")],
                find_candidates_fn=find_candidates,
                score_link_fn=score_link,
                upsert_edge_fn=upsert,
                min_confidence=0.5,
            )
        assert call_count["n"] == 2
