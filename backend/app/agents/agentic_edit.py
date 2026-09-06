# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The 4-level string-match edit ladder (THE BOOK §8).

``edit_file`` replaces an exact ``old_string`` with ``new_string``. Models don't
always reproduce whitespace perfectly, so we climb a ladder of progressively
looser *line-based* matches — but **every loosened level keeps a single-match
guard**: if the loosened pattern matches more than one place, we refuse rather
than guess. This is the anti-corruption rule (§8: "single-match guard at levels
≥2; never weaken"). A wrong-location edit silently corrupts code; a refused edit
just makes the model add more context and retry.

Pure and deterministic — no I/O. ``apply_edit`` raises :class:`EditError`
(turned into an ``is_error`` tool_result by the runtime) when it can't place the
edit uniquely.
"""
from __future__ import annotations

import re


class EditError(ValueError):
    """The edit could not be placed uniquely — surfaced to the model to retry."""


# Per-line normalizers, loosest-last. Level 1 (exact) is handled separately.
def _norm_trailing(line: str) -> str:   # level 2 — trailing whitespace only
    return line.rstrip()


def _norm_strip(line: str) -> str:      # level 3 — indentation-insensitive
    return line.strip()


def _norm_collapse(line: str) -> str:   # level 4 — collapse internal whitespace
    return re.sub(r"\s+", " ", line).strip()


_LEVELS = ((2, _norm_trailing), (3, _norm_strip), (4, _norm_collapse))


def _find_block(content: str, old: str, norm) -> list[tuple[int, int]]:
    """Find char spans in `content` whose lines equal `old`'s lines under `norm`.

    Line-based: code edits align to lines, and matching whole normalized lines
    avoids the false partial-line hits a character-level fuzzy match would make.
    """
    c_lines = content.splitlines(keepends=True)
    o_lines = old.splitlines()
    if not o_lines:
        return []
    o_norm = [norm(l) for l in o_lines]
    c_norm = [norm(l.rstrip("\n").rstrip("\r")) for l in c_lines]
    k = len(o_norm)
    spans: list[tuple[int, int]] = []
    offsets = [0]
    for ln in c_lines:
        offsets.append(offsets[-1] + len(ln))
    for i in range(0, len(c_norm) - k + 1):
        if c_norm[i:i + k] == o_norm:
            spans.append((offsets[i], offsets[i + k]))
    return spans


def apply_edit(content: str, old_string: str, new_string: str) -> tuple[str, int]:
    """Replace `old_string` with `new_string` in `content`.

    Returns ``(new_content, level_used)``. Raises :class:`EditError` if the match
    is empty, absent, or ambiguous at the matched level.
    """
    if old_string == "":
        raise EditError("old_string must not be empty")
    if old_string == new_string:
        raise EditError("old_string and new_string are identical")

    # Level 1 — exact, with uniqueness guard.
    exact = content.count(old_string)
    if exact == 1:
        return content.replace(old_string, new_string, 1), 1
    if exact > 1:
        raise EditError(
            f"old_string occurs {exact} times — include more surrounding context "
            "so it identifies exactly one location"
        )

    # Levels 2-4 — line-normalized, each with the single-match guard.
    for level, norm in _LEVELS:
        spans = _find_block(content, old_string, norm)
        if len(spans) == 1:
            start, end = spans[0]
            repl = new_string
            # Preserve the block's trailing newline so the following line isn't
            # pulled up when new_string omits it.
            if end > 0 and content[end - 1] == "\n" and not repl.endswith("\n"):
                repl += "\n"
            return content[:start] + repl + content[end:], level
        if len(spans) > 1:
            raise EditError(
                f"old_string matches {len(spans)} locations at match-level {level} — "
                "include more surrounding context to disambiguate"
            )

    raise EditError("old_string not found in the file")
