# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Layout builders + the generated `npci_master.pptx` reference deck.

The bland v1 template is the *code* in `_layout_builders.py` — pure
shape-drawing helpers that the renderer (D4) will call slide-by-slide.
`build_master.py` runs each helper once with sample content to produce
`npci_master.pptx`, a reference deck that:

  * Designers can open to see what each layout looks like and
    replace with branded versions.
  * Acts as a quick visual diff — re-run `build_master.py` after
    changing any builder and inspect the output.

The renderer does NOT load `npci_master.pptx` at runtime; it calls
the same builders. So the `.pptx` and the renderer can never drift
out of sync as long as both go through `_layout_builders.py`.
"""
from app.docgen.deck.templates._layout_builders import (
    SLIDE_HEIGHT,
    SLIDE_WIDTH,
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

__all__ = [
    "SLIDE_HEIGHT",
    "SLIDE_WIDTH",
    "build_bullet_list_slide",
    "build_diagram_slide",
    "build_numbered_flow_slide",
    "build_section_slide",
    "build_table_slide",
    "build_three_column_slide",
    "build_title_slide",
    "build_two_column_slide",
    "new_presentation",
]
