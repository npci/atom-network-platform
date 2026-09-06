# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Hierarchical markdown chunker (Slice 7).

Parses a markdown document into a sequence of heading sections and emits one
chunk per section (the "parent") plus one chunk per meaningful paragraph
within that section (the "children"). Children carry:

  - `parent_chunk_id`  → UUID of their parent section chunk
  - `title_breadcrumb` → "H1 > H2 > H3" path of enclosing headings

**Section body semantics (non-overlapping).** Each section's body spans
from right after its heading line to the position of the *next heading of
any level* (or EOF for the last). This gives flat, non-overlapping chunks.
The "retrieve children, return parents" enhancement described in plan
§4.1:166 — where the parent covers the whole subtree — is a separate
retrieval-time concern left as a future micro-slice.

Plan §4.1 also asks for parent-child linkage: Slice 7 emits `parent_chunk_id`
on children. A retrieval-time substitution that returns parent content when
a child is retrieved is deferred.

This module is pure — no DB, no I/O. Input is a path + raw markdown text +
optional `last_modified` datetime. Output is a list[dict] matching the
legacy ingestion contract (`path`, `content`, `chunk_index`) plus the new
Slice 7 fields (`id`, `title_breadcrumb`, `parent_chunk_id`, `last_modified`,
`is_parent`).

ID assignment: parents get a fresh UUID at emit time so children can
reference it. Ingestion must respect the pre-assigned `id` when present.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import uuid4

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$", re.MULTILINE)
MIN_PARAGRAPH_CHARS = 40
_PARA_SPLIT_RE = re.compile(r"\n\s*\n")


@dataclass
class _Section:
    level: int                   # 0 for implicit root; 1-6 for ATX headings
    title: str
    heading_start: int           # byte offset of the first '#' (0 for root)
    body_start: int              # byte offset where the section body begins
    body_end: int = -1           # set in a post-processing pass
    breadcrumb: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Section extraction (non-overlapping bodies)
# ──────────────────────────────────────────────────────────────────────────────

def _extract_sections(content: str) -> list[_Section]:
    """Return sections in document order with non-overlapping body spans."""
    headings = list(_HEADING_RE.finditer(content))

    # No headings → single root section covering the whole document.
    if not headings:
        if not content.strip():
            return []
        return [_Section(
            level=0, title="<root>",
            heading_start=0, body_start=0, body_end=len(content),
            breadcrumb=[],
        )]

    # Build one section per heading, tracking the breadcrumb stack.
    stack: list[_Section] = []
    sections: list[_Section] = []

    # Implicit root — only emitted if there's leading content before the first heading.
    first_heading_start = headings[0].start()
    if first_heading_start > 0:
        root = _Section(
            level=0, title="<root>",
            heading_start=0, body_start=0, body_end=first_heading_start,
            breadcrumb=[],
        )
        sections.append(root)

    for m in headings:
        level = len(m.group(1))
        title = m.group(2).strip()
        heading_start = m.start()
        # Body starts on the line after the heading. m.end() is after the heading
        # text; the newline is consumed by the regex match boundary, so body_start
        # = m.end() + 1 when the next char is '\n', else m.end().
        body_start = m.end() + 1 if m.end() < len(content) else m.end()

        # Pop breadcrumb stack back to this heading's parent level.
        while stack and stack[-1].level >= level:
            stack.pop()
        breadcrumb = [s.title for s in stack] + [title]

        section = _Section(
            level=level, title=title,
            heading_start=heading_start, body_start=body_start,
            breadcrumb=breadcrumb,
        )
        sections.append(section)
        stack.append(section)

    # Compute body_end: each section ends at the NEXT section's heading_start
    # (regardless of level). Last section ends at EOF.
    for i, s in enumerate(sections):
        if i + 1 < len(sections):
            s.body_end = sections[i + 1].heading_start
        else:
            s.body_end = len(content)

    return sections


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _split_paragraphs(text: str) -> list[str]:
    paragraphs = _PARA_SPLIT_RE.split(text)
    return [p.strip() for p in paragraphs if p.strip()]


def _format_breadcrumb(path: list[str]) -> Optional[str]:
    if not path:
        return None
    return " > ".join(path)


# ──────────────────────────────────────────────────────────────────────────────
# Public entry
# ──────────────────────────────────────────────────────────────────────────────

def chunk_markdown(
    path: str,
    content: str,
    *,
    last_modified: Optional[datetime] = None,
) -> list[dict]:
    """Produce parent + child chunks from a markdown document.

    Chunk dict schema (fields in addition to the legacy ingestion contract):
      - id                pre-assigned UUID
      - path
      - content
      - chunk_index
      - title_breadcrumb  "H1 > H2 > ..." or None (implicit root)
      - parent_chunk_id   UUID of parent section (children only); None for parents
      - last_modified     forwarded from caller (may be None)
      - is_parent         True for parent section chunks

    Behaviour:
      - Documents with no headings → one parent chunk with full content, no children.
      - Leading content before first heading → one implicit-root parent with no children.
      - Paragraphs shorter than MIN_PARAGRAPH_CHARS fold into the parent only.
    """
    if not content or not content.strip():
        return []

    sections = _extract_sections(content)
    chunks: list[dict] = []
    chunk_index = 0

    for section in sections:
        body = content[section.body_start : section.body_end].strip()
        if not body:
            continue

        is_root = section.level == 0
        parent_id = str(uuid4())
        breadcrumb = _format_breadcrumb(section.breadcrumb)

        chunks.append({
            "id":               parent_id,
            "path":             path,
            "content":          body,
            "chunk_index":      chunk_index,
            "title_breadcrumb": breadcrumb,
            "parent_chunk_id":  None,
            "last_modified":    last_modified,
            "is_parent":        True,
        })
        chunk_index += 1

        # The implicit root has no meaningful breadcrumb for children to attribute
        # to, so we don't emit child paragraphs for it.
        if is_root:
            continue

        for paragraph in _split_paragraphs(body):
            if len(paragraph) < MIN_PARAGRAPH_CHARS:
                continue
            chunks.append({
                "id":               str(uuid4()),
                "path":             path,
                "content":          paragraph,
                "chunk_index":      chunk_index,
                "title_breadcrumb": breadcrumb,
                "parent_chunk_id":  parent_id,
                "last_modified":    last_modified,
                "is_parent":        False,
            })
            chunk_index += 1

    return chunks
