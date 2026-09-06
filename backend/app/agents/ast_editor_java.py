# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Aider-style SEARCH/REPLACE patch editor (Slice 16).

Plan §7.4 lists two options for the editor output format: "SEARCH/REPLACE
block format (the format Aider proved robust) or a structured AST patch
(preferred for languages with reliable formatters)." This module implements
the first — text-based SEARCH/REPLACE — because it:

  - works across any language (filename says Java for backlog consistency
    but the format is language-neutral)
  - doesn't require a per-language parser at edit time
  - is well-validated in the Aider ecosystem

True tree-sitter AST-level patching is a follow-up if measurement shows
the text format has too many false negatives (e.g. whitespace drift).

Patch format (what the LLM is asked to emit — `build_system_prompt()`
spells this out):

    File: relative/path/to/File.java
    <<<<<<< SEARCH
    exact source lines to find
    =======
    replacement lines
    >>>>>>> REPLACE

Multiple patch blocks per file are allowed (emitted as separate
`File:` headers, not stacked under one). An empty SEARCH block means
"insert at end of file".

This module is PURE (except the convenience generator). `parse_patches`
and `apply_patches` never raise; structured results communicate failures.
"""
from __future__ import annotations
from app.core.prompts import load_prompt

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Data shapes
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Patch:
    """One SEARCH/REPLACE block targeting a specific file."""
    file_path: str
    search: str
    replace: str


@dataclass
class PatchFailure:
    """Why a particular patch couldn't be applied."""
    file_path: str
    reason: str                 # "file_not_found" | "pattern_not_found" | "ambiguous_match"
    search_excerpt: str = ""    # first 120 chars of the search text


@dataclass
class ApplyResult:
    """Outcome of applying a batch of patches to a file set."""
    updated_files: dict[str, str]
    applied_count: int = 0
    failures: list[PatchFailure] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Patch format tokens
# ──────────────────────────────────────────────────────────────────────────────

_FILE_HEADER_RE = re.compile(r"^File:\s*(.+?)\s*$", re.MULTILINE)
_SEARCH_OPEN   = "<<<<<<< SEARCH"
_SEARCH_CLOSE  = "======="
_REPLACE_CLOSE = ">>>>>>> REPLACE"


# ──────────────────────────────────────────────────────────────────────────────
# Pure parser
# ──────────────────────────────────────────────────────────────────────────────

def parse_patches(llm_output: str) -> list[Patch]:
    """Extract `Patch` objects from LLM output. Malformed blocks are silently
    dropped — the apply step will never see them.

    The parser scans line-by-line, accumulating state machine:
      idle          → saw `File:` header → state=expect_search_open
      expect_search_open → saw `<<<<<<< SEARCH` → state=collect_search
      collect_search   → saw `=======` → state=collect_replace
      collect_replace  → saw `>>>>>>> REPLACE` → emit Patch, state=idle

    Any line that violates the state transition (unexpected header, missing
    close, etc.) discards the in-progress patch and returns to idle. This
    tolerates LLM drift without raising.
    """
    if not llm_output:
        return []

    patches: list[Patch] = []
    lines = llm_output.splitlines()

    state = "idle"
    current_path: str | None = None
    search_buf: list[str] = []
    replace_buf: list[str] = []

    for line in lines:
        stripped = line.rstrip("\r")

        if state == "idle":
            m = _FILE_HEADER_RE.match(stripped)
            if m:
                current_path = m.group(1).strip()
                state = "expect_search_open"
                search_buf = []
                replace_buf = []
            # else: ignore
            continue

        if state == "expect_search_open":
            if stripped.strip() == _SEARCH_OPEN:
                state = "collect_search"
            elif _FILE_HEADER_RE.match(stripped):
                # New File header without opening a block — reset with new path.
                current_path = _FILE_HEADER_RE.match(stripped).group(1).strip()
                # stay in expect_search_open
            # else: ignore preamble / blank lines
            continue

        if state == "collect_search":
            if stripped.strip() == _SEARCH_CLOSE:
                state = "collect_replace"
            else:
                search_buf.append(stripped)
            continue

        if state == "collect_replace":
            if stripped.strip() == _REPLACE_CLOSE:
                if current_path:
                    patches.append(Patch(
                        file_path=current_path.strip(),
                        search="\n".join(search_buf),
                        replace="\n".join(replace_buf),
                    ))
                # Reset for next patch.
                state = "idle"
                current_path = None
                search_buf = []
                replace_buf = []
            else:
                replace_buf.append(stripped)
            continue

    # Trailing incomplete block (missing REPLACE close) is discarded.
    return patches


# ──────────────────────────────────────────────────────────────────────────────
# Pure applier
# ──────────────────────────────────────────────────────────────────────────────

def apply_patches(files: dict[str, str], patches: list[Patch]) -> ApplyResult:
    """Apply patches to a copy of `files`. Each patch either succeeds or
    produces a `PatchFailure`; other patches are unaffected by a single
    failure.

    Rules:
      - If a patch's `file_path` is not in `files` AND `search` is non-empty
        → failure `file_not_found`.
      - If `search` is empty (or whitespace-only): APPEND `replace` to the
        existing file (or create a new file when path is absent).
      - If `search` is non-empty but not found in file content → failure
        `pattern_not_found`.
      - If `search` is found MORE THAN ONCE → failure `ambiguous_match`.
      - Otherwise replace the first (only) occurrence.
    """
    updated = dict(files)
    failures: list[PatchFailure] = []
    applied = 0

    for patch in patches:
        path = patch.file_path
        if not path or not isinstance(path, str):
            failures.append(PatchFailure(
                file_path=str(path or ""),
                reason="invalid_path",
                search_excerpt=patch.search[:120],
            ))
            continue

        # Empty search → append (or create).
        if not patch.search.strip():
            if path in updated:
                existing = updated[path]
                separator = "" if existing.endswith("\n") else "\n"
                updated[path] = existing + separator + patch.replace
            else:
                updated[path] = patch.replace
            applied += 1
            continue

        if path not in updated:
            failures.append(PatchFailure(
                file_path=path, reason="file_not_found",
                search_excerpt=patch.search[:120],
            ))
            continue

        content = updated[path]
        occurrences = content.count(patch.search)
        if occurrences == 0:
            failures.append(PatchFailure(
                file_path=path, reason="pattern_not_found",
                search_excerpt=patch.search[:120],
            ))
        elif occurrences > 1:
            failures.append(PatchFailure(
                file_path=path, reason="ambiguous_match",
                search_excerpt=patch.search[:120],
            ))
        else:
            updated[path] = content.replace(patch.search, patch.replace, 1)
            applied += 1

    return ApplyResult(updated_files=updated, applied_count=applied, failures=failures)


# ──────────────────────────────────────────────────────────────────────────────
# Prompt scaffolding
# ──────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT_TEMPLATE = load_prompt("agents/ast_editor_java/system_prompt_template.md")


def build_system_prompt() -> str:
    """Return the strict-format system prompt used for patch generation."""
    return _SYSTEM_PROMPT_TEMPLATE


# ──────────────────────────────────────────────────────────────────────────────
# Convenience: LLM-based patch generator
# ──────────────────────────────────────────────────────────────────────────────

_GeneratorFn = Callable[[str, list[dict]], Awaitable[str]]


async def generate_patches(
    files: dict[str, str],
    task: str,
    *,
    call_llm_fn: _GeneratorFn | None = None,
    max_tokens: int = 4000,
) -> tuple[list[Patch], str]:
    """Async convenience wrapper. Returns `(patches, raw_llm_output)`.

    Uses `app.core.llm.call_llm` by default; callers can inject any
    `(system, messages) -> awaitable[str]` for testing or alternate providers.

    Fail-open: LLM exceptions → `([], "")`. Non-string response also yields
    the same empty result.
    """
    if call_llm_fn is None:
        from app.core.llm import call_llm as _default_call
        async def call_llm_fn(system, messages):  # type: ignore[misc]
            return await _default_call(system=system, messages=messages, max_tokens=max_tokens)

    if not files or not task or not task.strip():
        return [], ""

    import json as _json
    payload = {
        "task": task.strip(),
        "files": {
            path: (content[:6000] + ("\n// ...truncated..." if len(content) > 6000 else ""))
            for path, content in files.items()
        },
    }
    user_content = _json.dumps(payload, indent=2)[:15000]

    try:
        raw = await call_llm_fn(
            build_system_prompt(),
            [{"role": "user", "content": user_content}],
        )
    except Exception as e:
        logger.warning("generate_patches: LLM call failed: %s", e)
        return [], ""

    if not isinstance(raw, str):
        return [], ""
    return parse_patches(raw), raw
