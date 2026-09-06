# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""LLM contract for the Product Deck JSON outline.

The product_deck agent emits both:
  1. The existing markdown script (renders to .docx, unchanged).
  2. A fenced ```json``` block matching `DeckOutline` (renders to .pptx).

`SlideLayout` is a closed enum the LLM picks from. The renderer maps
each value to a master-slide layout in `templates/npci_master.pptx`.
The LLM does NOT pick pixel positions — it picks a kind of slide and
fills semantic fields. Layout flexibility: any order, EXCEPT slide 1
must be `title` (validator enforces).

Slide cap: 16. The earlier markdown-script prompt said 12; the new
contract permits up to 16 to match the longer of the two sample decks
(e-RUPI Deck v12 has 17 pages — 16 covers the realistic ceiling).
"""
from __future__ import annotations

import enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


MAX_SLIDES = 16


class SlideLayout(str, enum.Enum):
    TITLE         = "title"            # title + subtitle + presenter
    SECTION       = "section"          # large headline only (chapter divider)
    BULLET_LIST   = "bullet_list"      # title + 3-7 bullets
    TWO_COLUMN    = "two_column"       # title + L bullets + R bullets
    THREE_COLUMN  = "three_column"     # title + 3 iconified blocks
    NUMBERED_FLOW = "numbered_flow"    # title + N numbered step boxes in a row
    DIAGRAM       = "diagram"          # title + full-width diagram + caption
    TABLE         = "table"            # title + header row + body rows


class ColumnBlock(BaseModel):
    """One block of a two/three-column slide. `icon_hint` is a
    semantic name (e.g. "shield", "globe", "lock") the renderer maps
    to a built-in glyph; unknown hints fall back to a bullet."""
    icon_hint: Optional[str] = None
    heading:   str
    body:      str


class NumberedStep(BaseModel):
    """One step of a `numbered_flow` slide."""
    n:     int = Field(ge=1, le=12)
    label: str
    body:  Optional[str] = None


class TableBlock(BaseModel):
    """Tabular content (RACI / matrix). Headers form the header row;
    `rows` is a list of cell-lists, each the same length as headers."""
    headers: list[str] = Field(min_length=1, max_length=8)
    rows:    list[list[str]] = Field(min_length=1)

    @model_validator(mode="after")
    def _row_widths_match(self) -> "TableBlock":
        n = len(self.headers)
        for i, row in enumerate(self.rows):
            if len(row) != n:
                raise ValueError(
                    f"row {i} has {len(row)} cells; expected {n} (= header count)"
                )
        return self


class DeckSlide(BaseModel):
    """One slide. The fields populated depend on `layout`; a validator
    enforces the per-layout requirements so a malformed LLM output
    fails fast at parse time rather than producing a half-rendered
    deck."""
    slide_no: int = Field(ge=1, le=MAX_SLIDES)
    layout:   SlideLayout
    title:    str

    subtitle: Optional[str] = None
    bullets:  Optional[list[str]] = None
    columns:  Optional[list[ColumnBlock]] = None
    steps:    Optional[list[NumberedStep]] = None
    table:    Optional[TableBlock] = None

    diagram_text: Optional[str] = None
    diagram_kind: Optional[Literal["graphviz"]] = None

    speaker_notes: str = ""

    @model_validator(mode="after")
    def _layout_fields_consistent(self) -> "DeckSlide":
        L = SlideLayout
        match self.layout:
            case L.TITLE:
                # subtitle optional but typical
                pass
            case L.SECTION:
                pass
            case L.BULLET_LIST:
                if not self.bullets:
                    raise ValueError("bullet_list slide requires non-empty `bullets`")
                if not 1 <= len(self.bullets) <= 12:
                    raise ValueError("bullet_list expects 1-12 bullets")
            case L.TWO_COLUMN:
                if not self.columns or len(self.columns) != 2:
                    raise ValueError("two_column slide requires exactly 2 `columns`")
            case L.THREE_COLUMN:
                if not self.columns or len(self.columns) != 3:
                    raise ValueError("three_column slide requires exactly 3 `columns`")
            case L.NUMBERED_FLOW:
                if not self.steps or not 2 <= len(self.steps) <= 7:
                    raise ValueError("numbered_flow expects 2-7 `steps`")
            case L.DIAGRAM:
                if not self.diagram_text or not self.diagram_kind:
                    raise ValueError(
                        "diagram slide requires `diagram_text` AND `diagram_kind`"
                    )
            case L.TABLE:
                if self.table is None:
                    raise ValueError("table slide requires `table`")
        return self


class DeckOutline(BaseModel):
    """Top-level structure the LLM emits. `feature_name` is the name
    of the network change and is used in the title slide and footer.
    `slides[0].layout` MUST be `title` so every deck opens
    consistently — flexibility everywhere else."""
    title:        str
    subtitle:     str
    feature_name: str
    slides:       list[DeckSlide] = Field(min_length=3, max_length=MAX_SLIDES)

    @model_validator(mode="after")
    def _slide_invariants(self) -> "DeckOutline":
        if self.slides[0].layout != SlideLayout.TITLE:
            raise ValueError("slide 1 must use the `title` layout")
        for i, s in enumerate(self.slides, start=1):
            if s.slide_no != i:
                raise ValueError(
                    f"slide {i} declares slide_no={s.slide_no}; must match position"
                )
        return self
