# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Byte-identity guard for prompt text — Phase 1 of the prompt-externalisation plan.

WHAT THIS IS FOR. Moving a prompt out of a Python constant and into a file is
supposed to change nothing at all. This test makes "nothing at all" checkable:
it hashes every module-level prompt constant in `app/` and fails if any byte
moves. A migration PR that leaves this green is provably behaviour-preserving,
which is the only way to review a hundred-file prompt move without reading a
hundred prompts.

WHY BYTES AND NOT MEANING. One byte is not cosmetic here.
`core/prompt_blocks.segments_for_anthropic_cache` marks system-prompt segments
with `cache_control`, and Anthropic's prompt cache keys on the exact prefix
bytes. A stray trailing newline picked up from a text file misses the cache on
every subsequent request: no error, no failing assertion, just a larger bill.
See the trailing-newline rule in `app/core/prompts.py`.

WHAT THIS IS NOT. This cannot tell you a prompt got WORSE — only that it
changed. Deliberate rewording (the domain-vocabulary sweep, Phase 4) will fail
this test by design; you re-bless the snapshot and reach for
`docs/GOLDEN_OUTPUTS.md`, which exists precisely because genericisation removes
quality invisibly. The two are complements: this one guards mechanical moves,
the golden harness guards semantic ones.

RE-BLESS after an intentional change:

    docker compose run --rm -e UPDATE_PROMPT_SNAPSHOT=1 backend \
        pytest tests/core/test_prompt_snapshot.py
    docker cp atom_backend:/app/tests/core/prompt_snapshot.json \
        backend/tests/core/prompt_snapshot.json     # tests/ is baked, not mounted

and put the WHY in the commit message — a snapshot diff with no explanation is
indistinguishable from an accident.
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import json
import os
import pathlib

import pytest

APP = pathlib.Path(__file__).resolve().parents[2] / "app"
SNAPSHOT = pathlib.Path(__file__).resolve().parent / "prompt_snapshot.json"

# Chars. Below this a literal is a log line, an error message or a docstring
# fragment, not a prompt. Changing it rewrites every key — treat it as frozen.
MIN_PROMPT_CHARS = 200


def _is_binop_string(node: ast.expr) -> bool:
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)


def _binop_literal_size(node: ast.expr) -> int:
    """Literal chars in a `+`-concatenated string tree (0 if it is not one)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return len(node.value)
    if isinstance(node, ast.JoinedStr):
        return sum(len(p.value) for p in node.values
                   if isinstance(p, ast.Constant) and isinstance(p.value, str))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _binop_literal_size(node.left) + _binop_literal_size(node.right)
    return 0


_PROMPT_CALLS = {"load_prompt", "render_prompt"}


def _is_load_prompt_call(node: ast.expr) -> bool:
    """`X = load_prompt(...)` / `render_prompt(...)` — an ALREADY-MIGRATED prompt.

    These must stay discoverable or the snapshot cannot follow a constant
    through the migration it exists to protect: the value stops being a string
    literal, the AST scan loses sight of it, and a move that silently mangled
    the text would report as REMOVED (an expected-looking diff) instead of
    CHANGED. Tracking the NAME across the representation change is the whole
    point — the hash before and after must be identical.
    """
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _PROMPT_CALLS
    )


def _prompt_symbols() -> list[tuple[str, str]]:
    """(module, constant) for every module-level prompt: a string constant big
    enough to be one, or a `load_prompt(...)` binding. AST-based, so discovery
    costs no imports and cannot be fooled by a runtime reassignment."""
    found: list[tuple[str, str]] = []
    for path in sorted(APP.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if not isinstance(target, ast.Name):
                continue
            value = node.value
            size = 0
            if _is_binop_string(value):
                # `X = "..." + BLOCK + "..."` — string CONCATENATION, which is
                # neither a Constant nor a JoinedStr. Phase 2 migrated Constants,
                # Phase 3 migrated JoinedStrs, and this scan originally saw only
                # those two, so 47 prompts totalling ~89K chars — including
                # _ANALYSIS_PREFACE, the largest prompt in the tree — were
                # externalised by neither and guarded by nothing.
                size = _binop_literal_size(value)
            elif _is_load_prompt_call(value):
                # Size is not visible in the AST any more — it lives in the .md
                # file. Always track it; the imported value supplies the hash.
                size = MIN_PROMPT_CHARS
            elif isinstance(value, ast.Constant) and isinstance(value.value, str):
                size = len(value.value)
            elif isinstance(value, ast.JoinedStr):
                # f-string: measure only the literal parts. The interpolated
                # parts are other prompt constants (verified), so they are
                # measured under their own key.
                size = sum(
                    len(p.value)
                    for p in value.values
                    if isinstance(p, ast.Constant) and isinstance(p.value, str)
                )
            if size >= MIN_PROMPT_CHARS:
                module = ".".join(path.relative_to(APP.parent).with_suffix("").parts)
                found.append((module, target.id))
    return found


def _current() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for module, name in _prompt_symbols():
        value = getattr(importlib.import_module(module), name, None)
        if not isinstance(value, str):
            # Reassigned to a non-string at import time, or removed. Recorded so
            # the diff is explicit rather than a silently vanished key.
            out[f"{module}:{name}"] = {"sha256": None, "chars": 0}
            continue
        out[f"{module}:{name}"] = {
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
            "chars": len(value),
        }
    return out


def test_prompt_text_is_unchanged():
    from app.core.domain.registry import active_pack_key

    snapshot = json.loads(SNAPSHOT.read_text()) if SNAPSHOT.exists() else {}
    meta = snapshot.get("_meta", {})
    entries = snapshot.get("entries", {})
    current = _current()

    if os.environ.get("UPDATE_PROMPT_SNAPSHOT"):
        SNAPSHOT.write_text(
            json.dumps(
                {"_meta": {"domain_pack": active_pack_key(),
                           "min_prompt_chars": MIN_PROMPT_CHARS},
                 "entries": dict(sorted(current.items()))},
                indent=2,
            )
            + "\n"
        )
        pytest.skip(f"snapshot re-blessed: {len(current)} prompts")

    assert entries, (
        "no prompt snapshot on disk — generate it with "
        "UPDATE_PROMPT_SNAPSHOT=1 (see this module's docstring)"
    )

    # Several prompts embed `prompt_block(...)` from the active domain pack, so
    # their bytes legitimately differ per pack. Comparing across packs would
    # report a diff that is correct behaviour, so refuse to compare instead.
    if meta.get("domain_pack") != active_pack_key():
        pytest.skip(
            f"snapshot was taken under DOMAIN_PACK={meta.get('domain_pack')!r}, "
            f"active pack is {active_pack_key()!r} — hashes are pack-specific"
        )

    added = sorted(set(current) - set(entries))
    removed = sorted(set(entries) - set(current))
    changed = sorted(
        k for k in set(current) & set(entries)
        if current[k]["sha256"] != entries[k]["sha256"]
    )

    problems = []
    if changed:
        problems.append("CHANGED (prompt text differs):\n    " + "\n    ".join(
            f"{k}  {entries[k]['chars']} -> {current[k]['chars']} chars" for k in changed))
    if removed:
        problems.append("REMOVED (constant gone — deleted, renamed, or no longer a "
                        "module-level string):\n    " + "\n    ".join(removed))
    if added:
        problems.append("ADDED (new prompt constant):\n    " + "\n    ".join(added))

    assert not problems, (
        "prompt text moved.\n\n"
        + "\n".join(problems)
        + "\n\nIf this was a MECHANICAL move (constant -> file via load_prompt), it "
          "should NOT have changed any bytes — the usual cause is a trailing "
          "newline; see the trailing-newline rule in app/core/prompts.py.\n"
          "If the change was INTENTIONAL, re-bless the snapshot (docstring above) "
          "and run the golden-output harness: this test proves prompts changed, "
          "not that they still work."
    )
