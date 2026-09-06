# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Deterministic patch operations over docgen sections (surgical editing — M3).

Every op targets a block by its stable ID (from ``app.docgen.block_ids``) and is
applied by pure Python — no LLM in the apply step. The ``block_ids`` sidecar is
kept positionally aligned with the content lists as blocks are replaced /
inserted / deleted, so ``app.docgen.section_diff`` can prove exactly which blocks
moved.

Op vocabulary (each op is a plain dict):
    {"op": "replace_text",  "block_id": "p_..", "text": "..."}
    {"op": "find_replace",  "block_id": "p_..", "find": "T+1", "replace": "T+0"}
    {"op": "delete_block",  "block_id": "b_.."}
    {"op": "insert_block",  "section_key": "settlement", "field": "paragraphs",
                             "after": "p_.." | null, "text": "..."}
    {"op": "set_cell",      "table_id": "t_..", "row_id": "r_..",
                             "column": "Window" | <int>, "value": "T+0"}

``apply_patch`` applies the ops and runs the diff gate; it returns the new
sections plus a report of touched IDs and any gate violations (block IDs that
changed outside the declared set — always empty for a correct deterministic
apply; non-empty only signals a bug or a malformed op with side effects).
"""
from __future__ import annotations

import copy
from typing import Any

from app.docgen.block_ids import LIST_FIELDS, new_block_id, new_id


class PatchError(Exception):
    """A patch op referenced a block/section/column that does not exist, or is
    malformed. Structural — distinct from a gate violation, which is reported."""


def _find_block(sections: list[dict], block_id: str) -> tuple[int, str, int] | None:
    """Locate a block by ID → (section_idx, field, pos). ``field`` is one of the
    LIST_FIELDS, or ``"table"`` (pos -1) for the table itself, or ``"table_row"``.
    """
    for si, sec in enumerate(sections):
        ids = sec.get("block_ids") or {}
        for f in LIST_FIELDS:
            fids = ids.get(f) or []
            if block_id in fids:
                return si, f, fids.index(block_id)
        t = ids.get("table") or {}
        if t.get("table_id") == block_id:
            return si, "table", -1
        rids = t.get("row_ids") or []
        if block_id in rids:
            return si, "table_row", rids.index(block_id)
    return None


def apply_ops(sections: list[dict], ops: list[dict]) -> tuple[list[dict], dict[int, set[str]]]:
    """Apply ops to a deep copy of ``sections``. Returns (new_sections, touched)
    where ``touched`` maps section index → the block IDs the ops declared they
    would change (including newly inserted and removed IDs). Raises PatchError on
    a malformed / dangling op."""
    out = copy.deepcopy(sections)
    touched: dict[int, set[str]] = {}

    def _touch(si: int, bid: str) -> None:
        touched.setdefault(si, set()).add(bid)

    for op in ops:
        kind = op.get("op")

        if kind in ("replace_text", "find_replace", "delete_block"):
            bid = op.get("block_id")
            loc = _find_block(out, bid)
            if loc is None:
                raise PatchError(f"unknown block_id {bid!r} for op {kind!r}")
            si, f, pos = loc
            sec = out[si]
            if f in LIST_FIELDS:
                if kind == "replace_text":
                    sec[f][pos] = str(op.get("text", ""))
                elif kind == "find_replace":
                    sec[f][pos] = str(sec[f][pos]).replace(op.get("find", ""), op.get("replace", ""))
                else:  # delete_block
                    del sec[f][pos]
                    del sec["block_ids"][f][pos]
            elif f == "table_row":
                rows = sec["table_data"]["rows"]
                if kind == "find_replace":
                    rows[pos] = [str(c).replace(op.get("find", ""), op.get("replace", "")) for c in rows[pos]]
                elif kind == "delete_block":
                    del rows[pos]
                    del sec["block_ids"]["table"]["row_ids"][pos]
                else:  # replace_text on a row is ambiguous — use set_cell / set_row
                    raise PatchError("replace_text is not valid on a table row; use set_cell")
            else:
                raise PatchError(f"op {kind!r} not valid on block type {f!r}")
            _touch(si, bid)

        elif kind == "set_cell":
            rid = op.get("row_id")
            col = op.get("column")
            loc = _find_block(out, rid)
            if loc is None or loc[1] != "table_row":
                raise PatchError(f"unknown row_id {rid!r} for set_cell")
            si, _, pos = loc
            sec = out[si]
            headers = sec["table_data"].get("headers") or []
            row = sec["table_data"]["rows"][pos]
            cidx = col if isinstance(col, int) else (headers.index(col) if col in headers else None)
            if cidx is None or cidx >= len(row):
                raise PatchError(f"unknown column {col!r} for set_cell")
            row[cidx] = str(op.get("value", ""))
            _touch(si, rid)

        elif kind == "insert_block":
            skey = op.get("section_key")
            f = op.get("field")
            after = op.get("after")
            if f not in LIST_FIELDS:
                raise PatchError(f"insert_block field must be one of {LIST_FIELDS}, got {f!r}")
            si = next((i for i, s in enumerate(out) if s.get("section_key") == skey), None)
            if si is None:
                raise PatchError(f"unknown section_key {skey!r} for insert_block")
            sec = out[si]
            sec.setdefault(f, [])
            sec.setdefault("block_ids", {}).setdefault(f, [])
            nid = new_block_id(f)
            if after:
                loc = _find_block(out, after)
                pos = (loc[2] + 1) if (loc and loc[0] == si and loc[1] == f) else len(sec[f])
            else:
                pos = len(sec[f])
            sec[f].insert(pos, str(op.get("text", "")))
            sec["block_ids"][f].insert(pos, nid)
            _touch(si, nid)

        elif kind == "insert_row":
            tid = op.get("table_id")
            after = op.get("after")   # row_id to insert after, or None to append
            loc = _find_block(out, tid)
            if loc is None or loc[1] != "table":
                raise PatchError(f"unknown table_id {tid!r} for insert_row")
            si = loc[0]
            sec = out[si]
            table = sec.get("table_data") or {}
            rows = table.setdefault("rows", [])
            headers = table.get("headers") or []
            cells = op.get("cells")
            if not isinstance(cells, list):
                raise PatchError("insert_row requires a 'cells' list")
            cells = [str(c) for c in cells]
            if headers:  # normalize to the header count
                while len(cells) < len(headers):
                    cells.append("")
                cells = cells[: len(headers)]
            rid = new_id("r")
            row_ids = sec.setdefault("block_ids", {}).setdefault("table", {}).setdefault("row_ids", [])
            if after:
                aloc = _find_block(out, after)
                pos = (aloc[2] + 1) if (aloc and aloc[0] == si and aloc[1] == "table_row") else len(rows)
            else:
                pos = len(rows)
            rows.insert(pos, cells)
            row_ids.insert(pos, rid)
            _touch(si, rid)

        else:
            raise PatchError(f"unknown op {kind!r}")

    return out, touched


def apply_patch(sections: list[dict], ops: list[dict]) -> tuple[list[dict], dict[str, Any]]:
    """Apply ops and run the diff gate. Returns (new_sections, report) where
    report = {"touched": {si: {ids}}, "violations": {si: {ids}}}.

    Raises PatchError on a malformed op. The gate itself never raises — a
    non-empty ``violations`` means a block changed outside the declared target
    set (a bug or a side-effecting op); the caller decides whether to keep or
    discard the result."""
    from app.docgen.section_diff import diff_sections

    after, touched = apply_ops(sections, ops)
    violations: dict[int, set[str]] = {}
    for si in range(len(after)):
        moved = diff_sections(sections[si], after[si]).touched
        extra = moved - touched.get(si, set())
        if extra:
            violations[si] = extra
    return after, {"touched": touched, "violations": violations}
