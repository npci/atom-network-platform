# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""ChromaDB ↔ the platform pgvector RAG bridge.

The docgen pipeline's `docgen.rag.engine` is built around per-collection ChromaDB stores.
The platform already has a category-driven hybrid retriever (pgvector + BM25 + RRF)
in `app.rag.retrieval.retrieve()` indexing 5k+ authority/network chunks.

This module re-exposes only the two functions the docgen pipeline's pipeline actually calls
inside the LangGraph nodes:

    retrieve_multi_query(prompt, topic, collection_name, top_k=None)
        → (chunks: list[str], context: str)

    extract_reference_structure(file_path: str) -> str
        (kept verbatim from docgen — file-format heading extractor, no DB)

`collection_name` is accepted but ignored — the platform retrieval spans the whole
knowledge base. Categories are filtered downstream of the LLM if needed.

Multi-query rewriting is intentionally minimal here (1 extra topic-only query
on top of the original prompt). The platform's hybrid_retrieve already does dense+sparse
fusion; aggressive query rewriting tends to dilute results.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from app.core.database import SessionLocal
from app.rag.retrieval import retrieve as platform_retrieve

logger = logging.getLogger(__name__)


# ── Public API expected by docgen.agents.pipeline ────────────────────────────

def retrieve_multi_query(
    prompt: str,
    topic: str = "",
    collection_name: str = "default",   # kept for signature compatibility, unused
    top_k: Optional[int] = None,
) -> tuple[list[str], str]:
    """Run prompt + optional topic through the platform's hybrid retriever, dedupe, return.

    Returns:
        chunks    — list of plain-text chunk strings (the docgen pipeline's expected shape)
        context   — chunks joined with "\\n\\n---\\n\\n" separators, ready to paste into prompt
    """
    queries: list[str] = [prompt.strip()]
    topic = (topic or "").strip()
    if topic and topic.lower() != prompt.strip().lower():
        queries.append(topic)

    final_top_k = top_k or 8
    seen: set[str] = set()
    all_chunks: list[str] = []           # legacy text-only shape
    sourced: list[tuple[str, str]] = []  # parallel (source_file, content)

    db = SessionLocal()
    try:
        for q in queries:
            try:
                results = platform_retrieve(q, db, top_k=final_top_k)
            except Exception as e:
                logger.warning("[docgen-rag-bridge] retrieve('%s…') failed: %s", q[:40], e)
                continue
            for r in results:
                content = (r.get("content") or "").strip()
                if not content:
                    continue
                key = content[:200]
                if key in seen:
                    continue
                seen.add(key)
                source = r.get("source_file") or r.get("doc_category") or "kb"
                all_chunks.append(f"[{source}]\n{content}")
                sourced.append((source, content))
    finally:
        db.close()

    cap = final_top_k * 2
    sourced = sourced[:cap]

    # Build a [S#]-tagged context block + Source index footer so the LLM has
    # concrete handles to cite. The system prompts mandate that every grounded
    # claim carry an inline [S#] / [W#] tag; without numbered tags in the user
    # message, the LLM has nothing to cite.
    if sourced:
        body_parts: list[str] = []
        footer_parts: list[str] = []
        for idx, (source, content) in enumerate(sourced, start=1):
            tag = f"[S{idx}]"
            snippet = content if len(content) <= 1200 else (content[:1200].rstrip() + " …")
            body_parts.append(f"{tag} ({source})\n{snippet}")
            footer_parts.append(f"{tag} {source}")
        # Heading MUST stay in lock-step with the `evidence_heading` term the
        # citation rules point the model at (core/prompt_blocks.DEFAULT_TERMS /
        # the active pack's `evidence_heading` block) — a renamed header here
        # silently points the prompt at nothing.
        from app.core.domain.registry import prompt_block

        _heading = prompt_block("evidence_heading", "Retrieved authority corpus evidence")
        context = (
            f"## {_heading} — cite inline as [S#]\n\n"
            + "\n\n---\n\n".join(body_parts)
            + "\n\n## Source index (reproduce verbatim under a 'References' section at the end of the document)\n"
            + "\n".join(footer_parts)
        )
    else:
        context = ""

    logger.info(
        "[docgen-rag-bridge] retrieved %d unique chunks across %d queries (S-tagged: %d)",
        len(all_chunks), len(queries), len(sourced),
    )
    return all_chunks, context


def extract_reference_structure(file_path: str) -> str:
    """Extract heading/section structure from a reference document (verbatim from docgen).

    Kept for parity with the the docgen pipeline; only used when the user supplies
    a `reference_template` file in their GenerateRequest.
    """
    path = Path(file_path)
    suffix = path.suffix.lower()
    structure_lines: list[str] = []

    try:
        if suffix in (".docx", ".doc"):
            from docx import Document as DocxDocument
            doc = DocxDocument(str(path))
            for para in doc.paragraphs:
                if para.style.name.startswith("Heading"):
                    level = para.style.name.replace("Heading", "").strip()
                    structure_lines.append(f"{'#' * int(level or 1)} {para.text}")
        elif suffix == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            for page in reader.pages:
                text = page.extract_text() or ""
                for line in text.split("\n"):
                    stripped = line.strip()
                    if stripped and len(stripped) < 100 and stripped[0].isupper():
                        structure_lines.append(f"# {stripped}")
                if len(structure_lines) > 50:
                    break
        else:
            with open(str(path), encoding="utf-8", errors="ignore") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        structure_lines.append(stripped)
    except Exception as e:
        logger.warning("Could not extract structure from %s: %s", file_path, e)

    return "\n".join(structure_lines[:50]) if structure_lines else ""
