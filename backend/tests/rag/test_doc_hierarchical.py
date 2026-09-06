# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the hierarchical markdown chunker (Slice 7).

Pure tests — no DB, no LLM. The chunker is a pure function over string input.
Also includes a sanity check that the `deprecated` SQL filter is wired into
both retrieval queries (string inspection, not DB execution — the actual
row-filtering effect is verified in the live eval harness).
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.rag import doc_chunker_hierarchical as h
from app.rag import hybrid_search


# ──────────────────────────────────────────────────────────────────────────────
# chunk_markdown — core behaviour
# ──────────────────────────────────────────────────────────────────────────────

SAMPLE_MD = """# The network Overview

The network is a real-time payment system. It enables instant transfers between bank
accounts through mobile devices, 24x7.

## Transaction Flow

1. User initiates payment through PSP app.
2. PSP forwards the request to the Authority.
3. The Authority routes request to beneficiary bank.
4. Beneficiary bank validates and debits.

## Security

Two-factor authentication is mandatory. Device binding ties each the network ID to a
specific device fingerprint. All traffic is TLS-encrypted end-to-end.

### Authentication

PIN is the primary auth factor. Biometric fallback is supported on
capable devices via the OS keystore.

## Limits

Per-transaction limit is ₹1 lakh for most banks. Merchant payments may have
separate ceilings.
"""


def test_emits_parent_per_heading_section():
    chunks = h.chunk_markdown("guide.md", SAMPLE_MD, last_modified=None)
    parents = [c for c in chunks if c["is_parent"]]
    parent_titles = [p["title_breadcrumb"] for p in parents]
    # Expected top-level heading + 3 H2 sections + 1 H3 section = 5 parents
    assert len(parents) == 5
    assert "The network Overview" in parent_titles
    assert any("Transaction Flow" in (t or "") for t in parent_titles)
    assert any("Security" in (t or "") for t in parent_titles)
    assert any("Authentication" in (t or "") for t in parent_titles)
    assert any("Limits" in (t or "") for t in parent_titles)


def test_child_paragraphs_link_to_parent():
    chunks = h.chunk_markdown("guide.md", SAMPLE_MD, last_modified=None)

    # Security section has 1 multi-sentence paragraph — should yield a child
    security_parent = next(c for c in chunks if c.get("is_parent") and "Security" == c["title_breadcrumb"].split(" > ")[-1])
    security_children = [
        c for c in chunks
        if not c["is_parent"] and c.get("parent_chunk_id") == security_parent["id"]
    ]
    assert len(security_children) >= 1
    assert all("Two-factor" in c["content"] or "Device binding" in c["content"] or "TLS" in c["content"]
               for c in security_children)


def test_breadcrumb_shows_full_heading_path():
    chunks = h.chunk_markdown("guide.md", SAMPLE_MD, last_modified=None)
    # The "Authentication" section is an H3 nested under "Security" (H2)
    auth = next(c for c in chunks if c.get("is_parent") and (c["title_breadcrumb"] or "").endswith("Authentication"))
    assert auth["title_breadcrumb"] == "The network Overview > Security > Authentication"


def test_parent_ids_are_valid_uuids_and_unique():
    chunks = h.chunk_markdown("guide.md", SAMPLE_MD, last_modified=None)
    ids = [c["id"] for c in chunks]
    assert len(set(ids)) == len(ids)  # unique
    for cid in ids:
        UUID(cid)  # parses without raising


def test_last_modified_forwarded_on_all_chunks():
    when = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    chunks = h.chunk_markdown("guide.md", SAMPLE_MD, last_modified=when)
    for c in chunks:
        assert c["last_modified"] == when


def test_document_with_no_headings_yields_single_parent():
    content = "Just plain text with no headings.\n\nSecond paragraph also without a heading."
    chunks = h.chunk_markdown("no_headings.md", content, last_modified=None)
    assert len(chunks) == 1
    assert chunks[0]["is_parent"]
    assert chunks[0]["title_breadcrumb"] is None
    assert chunks[0]["parent_chunk_id"] is None
    assert "Just plain text" in chunks[0]["content"]


def test_short_paragraphs_folded_into_parent_only():
    """Paragraphs below MIN_PARAGRAPH_CHARS become no child — content is only
    in the parent chunk."""
    content = "# Title\n\nShort.\n\nOK.\n"
    chunks = h.chunk_markdown("short.md", content, last_modified=None)
    parents = [c for c in chunks if c["is_parent"]]
    children = [c for c in chunks if not c["is_parent"]]
    assert len(parents) == 1
    assert len(children) == 0
    assert "Short." in parents[0]["content"]


def test_chunk_index_is_sequential():
    chunks = h.chunk_markdown("guide.md", SAMPLE_MD, last_modified=None)
    indexes = [c["chunk_index"] for c in chunks]
    assert indexes == list(range(len(chunks)))


def test_empty_input_returns_empty_list():
    assert h.chunk_markdown("empty.md", "", last_modified=None) == []
    assert h.chunk_markdown("ws.md", "   \n\n\t\n", last_modified=None) == []


# ──────────────────────────────────────────────────────────────────────────────
# deprecated filter — SQL string inspection
# ──────────────────────────────────────────────────────────────────────────────

def _sql_body(func_name: str) -> str:
    """Retrieve the text() SQL string compiled inside a retrieval helper.

    We inspect the source to verify the `deprecated IS NOT TRUE` clause is
    present. Actual row-filtering behaviour is exercised by the live eval
    harness (make eval-retrieval) since we can't easily run pgvector
    in-process.
    """
    import inspect
    src = inspect.getsource(getattr(hybrid_search, func_name))
    return src


def test_dense_search_applies_deprecated_filter():
    assert "deprecated IS NOT TRUE" in _sql_body("_dense_search")


def test_hydrate_chunks_applies_deprecated_filter():
    assert "deprecated IS NOT TRUE" in _sql_body("_hydrate_chunks")
