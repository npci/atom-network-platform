# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Shared `build_impact_block` helper (sub-slice 21a).

Extracted from `code_change._build_impact_block` so BRD + Tech Spec
agents can inject the same "Files Likely Related" markdown block
into their system prompts. With the polyglot LSP stack in place
(Slices 23 / 24 / 24a) the impact analyzer's blast radius spans the
whole codebase across Java + Python + TS + JS — the value to design
docs is now substantial.

Single source of truth for the format: if we tune the block layout
or cap, every agent inherits the change.
"""
from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy.orm import Session

from app.core.config import settings

logger = logging.getLogger(__name__)


def build_impact_block(
    db: Session | None,
    description_sources: Iterable[str],
    *,
    block_title: str = "Blast Radius — Files Likely Related to This Change",
    max_files: int = 25,
    max_description_chars: int = 5000,
) -> str:
    """Run `analyze_impact` against a description string and format the
    resulting `files_affected` as a markdown block.

    Args:
        db: SQLAlchemy session. None → returns empty string (no block).
        description_sources: ordered list of candidate description strings
            (tech_spec, BRD, enriched_prompt, etc.). The first one with
            ≥10 non-whitespace chars is used; remainder ignored.
        block_title: header text. BRD/TSD callers may want different titles
            ("Code Likely Affected by This Design", etc.).
        max_files: cap on listed paths (block gets visually noisy beyond
            this; LLMs start ignoring the list).
        max_description_chars: clamp on the description fed to seed
            extraction. Bigger = more LLM cost (the analyzer enriches
            internally) for diminishing seed-quality returns.

    Fail-open (returns empty string) on:
        - flag off
        - db is None
        - all description_sources empty / too short
        - analyze_impact exception
        - no targets resolved
        - no files_affected in report
    """
    if not settings.use_impact_analyzer:
        return ""
    if db is None:
        return ""

    description = ""
    for s in description_sources:
        if s and len((s or "").strip()) >= 10:
            description = s.strip()
            break
    if not description:
        return ""

    try:
        from app.kg.impact_analyzer import analyze_impact
        report = analyze_impact(
            db=db,
            change_description=description[:max_description_chars],
        )
    except Exception as e:
        logger.warning("impact_analyzer pre-flight failed: %s", e)
        return ""

    if not report.targets or not report.files_affected:
        return ""

    files = report.files_affected[:max_files]
    lines: list[str] = [
        "",
        f"## {block_title}",
        "",
        "The knowledge graph identified the following files as containing symbols that",
        "CALL, EXTEND, IMPLEMENT, or DESCRIBE the code targeted by this change. Review",
        "them for coordinated modifications, updated tests, or stale documentation:",
        "",
    ]
    for f in files:
        lines.append(f"- {f}")
    if len(report.files_affected) > max_files:
        lines.append(f"- …and {len(report.files_affected) - max_files} more")
    lines.append("")
    lines.append(
        f"(Summary: {len(report.targets)} target symbol(s), "
        f"{report.total_impacted()} impacted chunk(s) across "
        f"{len(report.files_affected)} file(s).)"
    )
    logger.info(
        "impact_block: %d targets, %d files, %d impacted chunks",
        len(report.targets), len(report.files_affected), report.total_impacted(),
    )
    return "\n".join(lines)
