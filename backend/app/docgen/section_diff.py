# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Pure block-level diff between two versions of a docgen section (M2).

Compares by stable block ID (from ``app.docgen.block_ids``), so "did this block
change" is exact equality — not text-similarity guessing. This powers the diff
gate: an edit declares the block IDs it may touch, and any ID that changed / was
added / was removed outside that declared set is a violation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.docgen.block_ids import LIST_FIELDS


def block_values(section: dict[str, Any]) -> dict[str, str]:
    """Map every block ID in a section to a normalized string value.

    Paragraphs/bullets/numbered/code → their text; a table's ``table_id`` → its
    serialized headers; each row_id → its serialized row. Blocks with no aligned
    ID (should not happen after ``ensure_block_ids``) are skipped.
    """
    ids = section.get("block_ids") or {}
    out: dict[str, str] = {}
    for f in LIST_FIELDS:
        items = section.get(f) or []
        fids = ids.get(f) or []
        for bid, val in zip(fids, items):
            out[bid] = str(val)
    table = section.get("table_data")
    if isinstance(table, dict):
        t = ids.get("table") or {}
        tid = t.get("table_id")
        if tid:
            out[tid] = json.dumps(table.get("headers") or [], ensure_ascii=False)
        for rid, row in zip(t.get("row_ids") or [], table.get("rows") or []):
            out[rid] = json.dumps(row, ensure_ascii=False)
    return out


@dataclass
class SectionDiff:
    changed: set[str] = field(default_factory=set)
    added: set[str] = field(default_factory=set)
    removed: set[str] = field(default_factory=set)

    @property
    def touched(self) -> set[str]:
        return self.changed | self.added | self.removed

    def is_empty(self) -> bool:
        return not (self.changed or self.added or self.removed)


def diff_sections(before: dict[str, Any], after: dict[str, Any]) -> SectionDiff:
    """Which block IDs changed / were added / were removed between two versions."""
    bv, av = block_values(before), block_values(after)
    bkeys, akeys = set(bv), set(av)
    changed = {k for k in (bkeys & akeys) if bv[k] != av[k]}
    return SectionDiff(changed=changed, added=akeys - bkeys, removed=bkeys - akeys)


def gate_violations(before: dict, after: dict, allowed: set[str]) -> set[str]:
    """Block IDs that moved but were NOT declared as edit targets. Empty ⇒ clean."""
    return diff_sections(before, after).touched - set(allowed)
