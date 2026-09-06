# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""DeckOutline → .pptx renderer.

Single public entry point: `render(outline, output_path)`.

Calls the same shape-drawing helpers in `templates/_layout_builders`
that `build_master.py` uses, so the generated reference deck and any
LLM-driven deck stay visually identical until a designer replaces the
template.

Failure model (key contract):
  * `dot` failure on a diagram slide → fall back to a code-block text
    box of the source. Log a WARN with `slide_no`. The deck still
    renders.
  * Any other unexpected exception → propagate. Phase A's caller
    (D6) is responsible for catching and downgrading the deck failure
    to a docx-only Product Kit.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from app.docgen.deck.diagrams import DiagramRenderError, render_graphviz_to_png
from app.docgen.deck.schema import DeckOutline, SlideLayout
from app.docgen.deck.templates._layout_builders import (
    build_bullet_list_slide,
    build_diagram_slide,
    build_numbered_flow_slide,
    build_section_slide,
    build_table_slide,
    build_three_column_slide,
    build_title_slide,
    build_two_column_slide,
    new_presentation,
)

logger = logging.getLogger(__name__)


def render(outline: DeckOutline, output_path: str | Path) -> Path:
    """Render `outline` to a `.pptx` file at `output_path`.

    Returns the resolved Path on success.
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    prs = new_presentation()
    total = len(outline.slides)

    diagram_failures = 0
    for slide in outline.slides:
        common = dict(
            slide_no=slide.slide_no,
            total=total,
            feature_name=outline.feature_name,
            speaker_notes=slide.speaker_notes,
        )
        L = SlideLayout
        match slide.layout:
            case L.TITLE:
                build_title_slide(
                    prs, title=slide.title, subtitle=slide.subtitle, **common,
                )
            case L.SECTION:
                build_section_slide(prs, title=slide.title, **common)
            case L.BULLET_LIST:
                build_bullet_list_slide(
                    prs, title=slide.title, bullets=slide.bullets or [], **common,
                )
            case L.TWO_COLUMN:
                build_two_column_slide(
                    prs, title=slide.title, columns=slide.columns or [], **common,
                )
            case L.THREE_COLUMN:
                build_three_column_slide(
                    prs, title=slide.title, columns=slide.columns or [], **common,
                )
            case L.NUMBERED_FLOW:
                build_numbered_flow_slide(
                    prs, title=slide.title, steps=slide.steps or [], **common,
                )
            case L.DIAGRAM:
                png = _try_render_diagram(slide.diagram_text, slide.slide_no)
                if png is None and slide.diagram_text:
                    diagram_failures += 1
                build_diagram_slide(
                    prs, title=slide.title,
                    image_bytes=png,
                    fallback_text=slide.diagram_text,
                    caption=slide.subtitle,
                    **common,
                )
            case L.TABLE:
                build_table_slide(
                    prs, title=slide.title,
                    headers=slide.table.headers if slide.table else [],
                    rows=slide.table.rows if slide.table else [],
                    **common,
                )

    prs.save(str(out))
    logger.info(
        "rendered deck: slides=%d diagram_failures=%d output=%s",
        total, diagram_failures, out,
    )
    return out


def _try_render_diagram(source: Optional[str], slide_no: int) -> Optional[bytes]:
    if not source:
        return None
    try:
        return render_graphviz_to_png(source)
    except DiagramRenderError as exc:
        logger.warning(
            "diagram render failed on slide %d (will use text fallback): %s",
            slide_no, exc,
        )
        return None
