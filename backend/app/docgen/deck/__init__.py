# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Product Deck (.pptx) generation pipeline.

Companion to the existing markdown-script `.docx` rendition of
`product_deck` — when a Product Kit is generated, this module
produces a manually-editable PowerPoint deck rendered against an
The Authority master template, with diagrams as embedded PNGs.

Modules:
    schema    — DeckOutline / DeckSlide Pydantic shape; the LLM contract.
    diagrams  — graphviz subprocess wrapper (D2).
    renderer  — python-pptx → file (D4).
"""
from app.docgen.deck.schema import (
    ColumnBlock,
    DeckOutline,
    DeckSlide,
    NumberedStep,
    SlideLayout,
    TableBlock,
)

__all__ = [
    "ColumnBlock",
    "DeckOutline",
    "DeckSlide",
    "NumberedStep",
    "SlideLayout",
    "TableBlock",
]
