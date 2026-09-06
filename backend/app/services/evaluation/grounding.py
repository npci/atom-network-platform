# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Knowledge-base grounding seam for the eval critic.

The critic's job is LLM-as-judge: score an artifact against a rubric. On its
own it can only judge *internal consistency* (does the target follow from the
source?). This module adds the missing half — **grounding the judge on the
product and code knowledge base** so it can also judge *correctness against our
actual product reality* (e.g. "this BRD's error code contradicts the indexed
The Authority error-code spec").

Design contract:
- **Read-only consumer.** This module only *calls* the team's retrieval surface
  (`app.rag.retrieval.retrieve`). It never ingests, mutates, or owns indexing —
  that is other people's subsystem. Our part is evaluation.
- **Fail-open, always.** Any error, missing index, or empty result returns an
  empty `GroundingResult`. The critic then proceeds rubric-only — byte-for-byte
  the ungrounded behaviour. This is what lets the same code run smoothly on a
  laptop with nothing indexed and light up in UAT where the indexes are full.
- **Feature-flagged.** `settings.eval_grounding_enabled` gates the whole thing.

Provenance (which sources grounded a verdict) is returned so the runner can
surface it in Eval Logs — that transparency is what makes the judge auditable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.evaluation.schemas import CheckpointContract

logger = logging.getLogger(__name__)

# Per-snippet char budget — keep the critic prompt small so latency stays low.
SNIPPET_CHARS = 900
# Char budget for the query we send to retrieval. nomic-embed handles long
# inputs, but a focused query retrieves better than a whole artifact dump.
QUERY_CHARS = 600

# Which doc categories count as "product knowledge" vs "code knowledge".
# Used only for labelling each snippet in the prompt + provenance; retrieval
# itself searches everything indexed. Mirrors app.models.document_chunk.DocCategory.
_CODE_CATEGORIES = {
    "java_source",
    "python_source",
    "typescript_source",
    "javascript_source",
}


@dataclass(slots=True)
class GroundingSnippet:
    source_file: str
    doc_category: str
    content: str
    score: float

    @property
    def kind(self) -> str:
        return "code" if self.doc_category in _CODE_CATEGORIES else "product"


@dataclass(slots=True)
class GroundingResult:
    snippets: list[GroundingSnippet] = field(default_factory=list)
    enabled: bool = True
    error: str | None = None

    @property
    def empty(self) -> bool:
        return not self.snippets

    def provenance(self) -> list[dict]:
        """Compact, log-friendly list of what grounded the verdict."""
        return [
            {
                "source_file": s.source_file,
                "doc_category": s.doc_category,
                "kind": s.kind,
                "score": round(s.score, 4),
            }
            for s in self.snippets
        ]


def _build_query(contract: CheckpointContract, target_text: str) -> str:
    """A focused retrieval query from the checkpoint intent + target head.

    The target artifact is what we're grounding, so its opening text drives the
    query; the checkpoint description anchors the domain (e.g. "tech spec").
    """
    head = (target_text or "").strip()[:QUERY_CHARS]
    desc = (contract.description or "").strip()
    return f"{desc}\n{head}".strip()


def retrieve_grounding(
    db: Session,
    contract: CheckpointContract,
    target_text: str,
    *,
    top_k: int | None = None,
) -> GroundingResult:
    """Return knowledge-base snippets relevant to the target artifact.

    Never raises. Returns an empty result (and the critic proceeds rubric-only)
    when grounding is disabled, nothing is indexed, or retrieval errors.
    """
    if not getattr(settings, "eval_grounding_enabled", True):
        return GroundingResult(enabled=False)

    query = _build_query(contract, target_text)
    if not query:
        return GroundingResult()

    k = top_k if top_k is not None else int(getattr(settings, "eval_grounding_top_k", 4) or 4)

    try:
        # Local import: keeps the eval package importable in the test harness
        # without dragging in the RAG/embeddings stack.
        from app.rag.retrieval import retrieve

        # categories=None → search everything indexed (product + code). On a
        # laptop with only product docs this still returns product grounding;
        # in UAT it returns both. retrieve() is itself fail-open internally.
        rows = retrieve(query, db, top_k=k)
    except Exception as exc:  # noqa: BLE001 — grounding must never break the critic
        logger.warning("eval grounding retrieval failed (fail-open): %s", exc)
        return GroundingResult(error=f"{type(exc).__name__}: {exc}")

    snippets: list[GroundingSnippet] = []
    for r in rows or []:
        content = (r.get("content") or "").strip()
        if not content:
            continue
        snippets.append(
            GroundingSnippet(
                source_file=str(r.get("source_file") or "unknown"),
                doc_category=str(r.get("doc_category") or "unknown"),
                content=content[:SNIPPET_CHARS],
                score=float(r.get("score") or 0.0),
            )
        )

    if snippets:
        logger.info(
            "eval grounding: checkpoint=%s retrieved=%d sources=%s",
            contract.checkpoint_id.value,
            len(snippets),
            [s.source_file for s in snippets],
        )
    return GroundingResult(snippets=snippets)


def format_grounding_block(result: GroundingResult) -> str:
    """Render the grounding snippets as a prompt block, or '' when empty."""
    if result.empty:
        return ""
    lines = []
    for i, s in enumerate(result.snippets, 1):
        lines.append(
            f"[{i}] ({s.kind}/{s.doc_category}) {s.source_file}\n{s.content}".rstrip()
        )
    return "\n\n".join(lines)
