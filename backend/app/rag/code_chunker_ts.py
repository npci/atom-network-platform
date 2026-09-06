# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tree-sitter code chunker — public dispatcher (Slice 3).

`chunk_source_file(path, content, language, fallback=...)` is the one public
entry. It:

  1. Checks the `USE_TREE_SITTER_CHUNKER` feature flag (default OFF).
  2. Tries the tree-sitter path via `code_chunker_langs.extract_chunks`.
  3. Falls back to the caller-supplied `fallback` fn when the flag is off,
     the language is unsupported, or tree-sitter returns nothing.

Call-site pattern in `code_ingestion.py`:

    from app.rag import code_chunker_ts
    chunks = code_chunker_ts.chunk_source_file(
        path, content, "java",
        fallback=lambda: _chunk_java_file(path, content),
    )

Slice 3 ships Java + Python + JS/TS; remaining 15 languages land as sub-slices
by adding entries to `code_chunker_langs._build_registry()`.
"""
from __future__ import annotations

import logging
from typing import Callable

from app.core.config import settings
from app.rag import code_chunker_langs

logger = logging.getLogger(__name__)


def chunk_source_file(
    path: str,
    content: str,
    language: str,
    *,
    fallback: Callable[[], list[dict]] | None = None,
) -> list[dict]:
    """Chunk a source file via tree-sitter when the flag + language support allow.

    Args:
        path: Source path (stored as-is in `source_file`).
        content: Full file text.
        language: Language key — see `code_chunker_langs.supported_languages()`.
        fallback: Zero-arg function producing the legacy chunk list; called when
                  the tree-sitter path is unavailable or produces no output.

    Returns:
        A list of chunk dicts (see `code_chunker_langs.extract_chunks` for
        schema). Guaranteed non-empty when fallback is supplied.
    """
    if not settings.use_tree_sitter_chunker:
        logger.debug("tree-sitter chunker disabled by config; using fallback for %s", path)
        return fallback() if fallback else []

    chunks = code_chunker_langs.extract_chunks(path, content, language)
    if chunks:
        logger.debug("tree-sitter chunker: %s emitted %d chunks (lang=%s)", path, len(chunks), language)
        return chunks

    logger.warning(
        "tree-sitter chunker returned no chunks for lang=%s path=%s — falling back",
        language, path,
    )
    return fallback() if fallback else []


def supported_languages() -> list[str]:
    """Pass-through to the registry for external introspection."""
    return code_chunker_langs.supported_languages()


# ── Slice 4 — 3-view expansion ────────────────────────────────────────────────

def expand_to_multiview(chunk: dict, nl_summary: str = "") -> list[dict]:
    """Given a single tree-sitter chunk, produce up to 3 view dicts sharing a
    newly-generated `parent_symbol_id`.

    Pass-through cases (returns `[chunk]` unchanged):
      - chunk is a file-level chunk (`symbol_kind == 'file'`)
      - chunk has no `symbol_kind` (regex-chunker output, or non-code rows)

    For symbol chunks, emits:
      - **body view**: full symbol source, `view_kind='body'`
      - **signature view** (if `signature` is non-empty): declaration line
        with kind label prepended, `view_kind='signature'`
      - **nl_summary view** (if `nl_summary` arg is non-empty): the summary text
        with kind label prepended, `view_kind='nl_summary'`

    The caller supplies `nl_summary` (typically from `code_summarizer`) so this
    function stays pure — no LLM calls, no I/O — and is unit-testable.
    """
    from uuid import uuid4

    symbol_kind = chunk.get("symbol_kind")
    if symbol_kind in (None, "file"):
        return [chunk]

    group_id = str(uuid4())

    # Body view — original content, unchanged.
    body_view = {**chunk, "view_kind": "body", "parent_symbol_id": group_id}
    views: list[dict] = [body_view]

    # Signature view — declaration line + kind label.
    sig_text = (chunk.get("signature") or "").strip()
    if sig_text:
        sig_content = f"[{symbol_kind}] {sig_text}"
        views.append({
            **chunk,
            "content": sig_content,
            "view_kind": "signature",
            "parent_symbol_id": group_id,
        })

    # NL-summary view — caller-supplied summary text.
    summary_text = (nl_summary or "").strip()
    if summary_text:
        views.append({
            **chunk,
            "content": summary_text,
            "view_kind": "nl_summary",
            "parent_symbol_id": group_id,
        })

    return views
