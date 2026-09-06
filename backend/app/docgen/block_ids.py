# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Stable per-block identity for docgen sections (surgical editing — M1).

Every content block (paragraph, bullet, numbered item, code block, table + its
rows) gets a permanent ID stored in a ``block_ids`` sidecar on the section dict.
IDs are *identity, not content*: assigned once when a block first appears and
preserved across edits. The ``ensure_*`` helpers are idempotent — they fill only
missing IDs, so calling them on an already-tagged document is a no-op.

This is what lets ``app.docgen.section_diff`` prove "only intended blocks
changed" by exact ID comparison instead of text-similarity guessing, and what
lets ``app.docgen.patch`` address a single paragraph/row.

The sidecar is additive metadata — the .docx renderer ignores it, and it is a
declared-but-optional field on ``GeneratedContent`` so documents generated before
IDs existed still validate (they gain IDs on first load).
"""
from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

# Content list fields that get one ID per element, positionally aligned.
LIST_FIELDS: tuple[str, ...] = ("paragraphs", "bullet_points", "numbered_items", "code_blocks")

_PREFIX: dict[str, str] = {
    "paragraphs": "p",
    "bullet_points": "b",
    "numbered_items": "n",
    "code_blocks": "c",
}


def new_id(prefix: str) -> str:
    """Mint a fresh block ID with a type prefix (e.g. ``p_1a2b3c4d``)."""
    return f"{prefix}_{uuid4().hex[:8]}"


def new_block_id(field: str) -> str:
    """Mint an ID for a content-list field, prefixed by that field's type."""
    return new_id(_PREFIX.get(field, "x"))


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")
    return slug or "section"


def ensure_block_ids(section: dict[str, Any]) -> dict[str, Any]:
    """Assign stable IDs to every block in one section, filling only gaps.

    Idempotent: existing IDs are preserved by position; only blocks without an
    aligned ID get a fresh one. Positional alignment with the content lists is
    maintained. NOTE: this backfills — it does not *reconcile* a mid-edit
    mismatch. Keeping IDs aligned during an edit is the patch applier's job; this
    is only ever called on fresh generation, on load of an untagged doc, or as an
    idempotent no-op.
    """
    ids = section.get("block_ids")
    if not isinstance(ids, dict):
        ids = {}

    for field in LIST_FIELDS:
        items = section.get(field) or []
        existing = ids.get(field)
        if not isinstance(existing, list):
            existing = []
        aligned = list(existing[: len(items)])
        while len(aligned) < len(items):
            aligned.append(new_block_id(field))
        ids[field] = aligned

    table = section.get("table_data")
    rows = table.get("rows") if isinstance(table, dict) else None
    if table and rows:
        t = ids.get("table")
        if not isinstance(t, dict):
            t = {}
        if not t.get("table_id"):
            t["table_id"] = new_id("t")
        row_ids = t.get("row_ids")
        if not isinstance(row_ids, list):
            row_ids = []
        aligned = list(row_ids[: len(rows)])
        while len(aligned) < len(rows):
            aligned.append(new_id("r"))
        t["row_ids"] = aligned
        ids["table"] = t
    else:
        ids.pop("table", None)  # table removed → drop stale table ids

    section["block_ids"] = ids
    return section


def ensure_document_ids(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure a stable ``section_key`` + block IDs across a whole document.

    ``section_key`` is stabilized from the heading when the planner omitted it
    (blueprint docs already carry stable keys). Uniqueness is enforced so two
    same-named sections don't collide. Mutates in place and returns the list.
    """
    seen: set[str] = set()
    for i, sec in enumerate(sections):
        key = (sec.get("section_key") or "").strip()
        if not key:
            key = _slugify(sec.get("section_heading") or f"section_{i + 1}")
        base, n = key, 2
        while key in seen:
            key = f"{base}_{n}"
            n += 1
        seen.add(key)
        sec["section_key"] = key
        ensure_block_ids(sec)
    return sections
