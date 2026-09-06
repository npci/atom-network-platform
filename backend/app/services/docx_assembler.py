# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Assemble a .docx from the agent's markdown output.

Takes the final markdown (as streamed by BRD / Tech Spec / Product Kit agents),
parses it into sections, and drives `DocxBuilder` to produce a styled Word file.

Supported markdown features:
  - Headings (`#`, `##`, `###`)
  - Paragraphs (plain text runs)
  - Unordered lists (`-`, `*`)
  - Ordered lists (numbered lines — including our FR-01 / FR-02 style)
  - Tables (GitHub-flavored pipe syntax)
  - Fenced code blocks (```lang ... ```)

Circular documents get the serif-style `CircularDocxBuilder` + letterhead.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from app.core.config import settings
from app.services.docx_builder import DocxBuilder, CircularDocxBuilder

logger = logging.getLogger(__name__)


_FENCE_RE = re.compile(r"^```(\w+)?\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_NUMBERED_RE = re.compile(r"^\s*(?:\d+\.|FR-\d+:)\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")


def _parse_table(lines: list[str], start: int) -> tuple[list[str], list[list[str]], int] | None:
    """Detect and parse a GFM table starting at `start` (header line)."""
    if start + 1 >= len(lines):
        return None
    header_line = lines[start].strip()
    sep_line = lines[start + 1].strip()
    if "|" not in header_line or not _TABLE_SEP_RE.match(sep_line):
        return None

    def _cells(line: str) -> list[str]:
        parts = [c.strip() for c in line.strip().strip("|").split("|")]
        return parts

    headers = _cells(header_line)
    rows: list[list[str]] = []
    i = start + 2
    while i < len(lines):
        row_line = lines[i].rstrip()
        if not row_line.strip() or "|" not in row_line:
            break
        rows.append(_cells(row_line))
        i += 1
    return headers, rows, i


def _consume_code_block(lines: list[str], start: int) -> tuple[str, int]:
    """Consume a fenced code block; returns (code_text, next_index)."""
    out: list[str] = []
    i = start + 1
    while i < len(lines):
        if _FENCE_RE.match(lines[i].strip()):
            return "\n".join(out), i + 1
        out.append(lines[i])
        i += 1
    # Unterminated fence — treat rest of doc as code
    return "\n".join(out), len(lines)


def _is_circular_like(doc_type: str, doc_subtype: str | None) -> bool:
    """Return True if the document should use the Circular template."""
    return (doc_type or "").lower() == "circular" or (doc_subtype or "").lower() == "circular"


# ──────────────────────────────────────────────────────────────────────────────
# Main entry
# ──────────────────────────────────────────────────────────────────────────────

def build_docx_from_markdown(
    markdown: str,
    *,
    title: str,
    subtitle: str = "",
    doc_type: str = "",
    doc_subtype: str | None = None,
    version: str = "1.0",
    revision_history: list[dict] | None = None,
    output_path: str | Path,
) -> Path:
    """Parse markdown and write a styled .docx.

    Args:
        markdown:         Raw markdown from the LLM.
        title:            Top-level title shown on cover page.
        subtitle:         Sub-title (e.g. the change request title).
        doc_type:         One of BRD / "Technical Specification" / "Product Kit" / "Circular".
        doc_subtype:      For Product Kit docs, the specific sub-type (e.g. "circular", "faq").
        version:          Displayed on cover page.
        revision_history: [{version, date, author, changes}, ...] — table on cover page.
        output_path:      Destination .docx path.

    Returns:
        Path to the written file.
    """
    is_circular = _is_circular_like(doc_type, doc_subtype)
    builder: DocxBuilder = CircularDocxBuilder() if is_circular else DocxBuilder()

    # Circular: letterhead only, no TOC / cover
    if is_circular:
        today = datetime.utcnow().strftime("%d %b %Y")
        reference = revision_history[0].get("changes") if (revision_history and revision_history[0].get("changes")) else ""
        builder.add_letterhead(reference=reference, date=today)
    else:
        builder.add_cover_page(
            title=title,
            subtitle=subtitle,
            doc_type=doc_type,
            version=version,
            revision_history=revision_history or [{
                "version": version, "date": datetime.utcnow().strftime("%Y-%m-%d"),
                "author": "the Authority Platform", "changes": "Initial draft",
            }],
        )
        builder.add_toc()

    # Parse markdown line-by-line, dispatching to builder helpers
    lines = (markdown or "").splitlines()
    i = 0
    buffer: list[str] = []

    def _flush_para():
        if buffer:
            text = " ".join(s.strip() for s in buffer if s.strip())
            if text:
                builder.add_paragraph(text)
            buffer.clear()

    bullets: list[str] = []
    numbers: list[str] = []

    def _flush_bullets():
        nonlocal bullets
        if bullets:
            builder.add_bullets(bullets)
            bullets = []

    def _flush_numbers():
        nonlocal numbers
        if numbers:
            builder.add_numbered(numbers)
            numbers = []

    def _flush_all():
        _flush_para()
        _flush_bullets()
        _flush_numbers()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Fenced code block
        if _FENCE_RE.match(stripped):
            _flush_all()
            code, i = _consume_code_block(lines, i)
            builder.add_code_block(code)
            continue

        # Blank line = paragraph/list boundary
        if not stripped:
            _flush_all()
            i += 1
            continue

        # Heading
        m = _HEADING_RE.match(stripped)
        if m:
            _flush_all()
            hashes, text = m.group(1), m.group(2).strip()
            builder.add_heading(text, level=len(hashes))
            i += 1
            continue

        # Table
        table = _parse_table(lines, i)
        if table is not None:
            _flush_all()
            headers, rows, i = table
            builder.add_styled_table(headers=headers, rows=rows)
            continue

        # Numbered (FR-01 or "1.")
        nm = _NUMBERED_RE.match(stripped)
        if nm:
            _flush_para()
            _flush_bullets()
            numbers.append(nm.group(1).strip())
            i += 1
            continue

        # Bullet
        bm = _BULLET_RE.match(stripped)
        if bm:
            _flush_para()
            _flush_numbers()
            bullets.append(bm.group(1).strip())
            i += 1
            continue

        # Plain paragraph line — accumulate
        buffer.append(stripped)
        i += 1

    _flush_all()

    out = Path(output_path)
    builder.save(out)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Path helper
# ──────────────────────────────────────────────────────────────────────────────

def _path_safe(part: str) -> str:
    """Reduce one filename component to characters that cannot traverse.

    Collapses separators and dots rather than rejecting, because both callers
    feed this human-ish labels and a hard failure on an odd doc_type would be a
    regression; the goal is only that the result stays a single component.
    """
    cleaned = re.sub(r"[^a-z0-9._-]+", "_", (part or "").lower())
    cleaned = cleaned.replace("..", "_")          # no parent-dir component
    return cleaned.strip("._-") or "artifact"


def artifact_path(change_id: str, doc_type: str, version: int = 1, subtype: str | None = None) -> Path:
    """Build a session-scoped output path under artifacts/sessions/{change_id}/."""
    base = Path(settings.artifacts_dir) / "sessions" / change_id
    base.mkdir(parents=True, exist_ok=True)
    slug = _path_safe(doc_type)
    name_parts = [slug]
    if subtype:
        # `subtype` is a QUERY parameter on the download route and is validated
        # only for product_kit — the brd/tech_spec/xsd/canvas branches pass it
        # through untouched. It used to be slash-stripped here while doc_type
        # was, one line apart, so `?subtype=../../<other>/brd` walked out of the
        # session directory and overwrote another change's artifact.
        name_parts.append(_path_safe(subtype))
    name_parts.append(f"v{version}.docx")
    return base / "_".join(name_parts)
