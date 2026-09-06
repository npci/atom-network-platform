# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 6.4 — Hierarchical file-tree summary.

The original `_get_file_tree` in `code_change.py` emits a flat newline-
separated list of every indexed source file. On a moderately large
polyglot repo this is 50-80 KB of prompt — most of which is repeated
parent directory paths.

This helper rolls up siblings under each directory into a single line
showing the file count, so a tree like:

    src/main/java/com/network/core/entities/Token.java
    src/main/java/com/network/core/entities/Refund.java
    src/main/java/com/network/core/entities/Dispute.java
    src/main/java/com/network/core/services/AuthSvc.java
    ...

becomes:

    src/
      main/java/com/network/
        core/  (297 files)

at depth=4. The agent can still grep file names via BM25 retrieval; this
block is only for high-level orientation.

Pure-Python, no DB / no settings dependency — caller does any filtering
or repo-scoping upstream.
"""
from __future__ import annotations


# When a directory has more than this many descendants we collapse it to a
# `(N files)` summary line. Below the threshold we list filenames.
_LIST_THRESHOLD = 8


def format_tree_summary(paths: list[str], depth: int = 3) -> str:
    """Roll up flat paths into a depth-limited directory tree summary.

    Args:
        paths: List of source-file paths. Empty list returns "".
        depth: Max number of directory levels to expand explicitly.
               Subtrees deeper than this collapse to `(N files)`.

    Returns:
        Multi-line indented tree. Two spaces per level.
    """
    if not paths:
        return ""
    if depth < 1:
        depth = 1

    # Build the tree. Every node carries:
    #   - 'dirs':  dict[str, node]   subdirectories
    #   - 'files': list[str]          terminal filenames at this level
    root = _new_node()
    for raw in paths:
        if not raw:
            continue
        parts = [p for p in raw.split("/") if p]
        if not parts:
            continue
        _insert(root, parts)

    lines: list[str] = []
    _render(root, name=None, depth_remaining=depth, indent=0, lines=lines)
    return "\n".join(lines)


def _new_node() -> dict:
    return {"dirs": {}, "files": []}


def _insert(node: dict, parts: list[str]) -> None:
    if not parts:
        return
    if len(parts) == 1:
        node["files"].append(parts[0])
        return
    head, *rest = parts
    child = node["dirs"].setdefault(head, _new_node())
    _insert(child, rest)


def _aggregate_count(node: dict) -> int:
    """Total descendant file count from this node."""
    n = len(node["files"])
    for sub in node["dirs"].values():
        n += _aggregate_count(sub)
    return n


def _render(node: dict, name: str | None, depth_remaining: int, indent: int, lines: list[str]) -> None:
    pad = "  " * indent
    if name is not None:
        # Directory header. Show count only when collapsing OR when there's
        # genuinely more than one descendant.
        total = _aggregate_count(node)
        if depth_remaining <= 0 and total > 0:
            lines.append(f"{pad}{name}/  ({total} files)")
            return
        if total > 1:
            lines.append(f"{pad}{name}/  ({total} files)")
        else:
            lines.append(f"{pad}{name}/")

    # Children get indented one deeper IF we printed a header at this level.
    child_indent = indent + 1 if name is not None else indent
    child_pad = "  " * child_indent

    # Subdirectories first, sorted.
    for dname in sorted(node["dirs"].keys()):
        sub = node["dirs"][dname]
        _render(sub, dname, depth_remaining - 1, child_indent, lines)

    # Then files at this level.
    files = sorted(node["files"])
    if not files:
        return
    if len(files) <= _LIST_THRESHOLD:
        for fname in files:
            lines.append(f"{child_pad}{fname}")
    else:
        lines.append(f"{child_pad}({len(files)} files)")
