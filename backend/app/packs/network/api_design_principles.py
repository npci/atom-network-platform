# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Distilled network API/flow/test-case/error-code design principles.

Pack content. This lived in `core/` until the domain-term cleanup: it is
domain design judgement, not platform machinery, and core must not carry it.
Reached through the pack contract as the `api_design_principles` prompt
block. The markdown moved with it, byte-identical -- a prompt block that
changes bytes changes model output.

Loaded as an always-on system-prompt block for the Phase A design agents, the
same way the pack's `hard_rules` block is. The text lives in the sibling
`api_design_principles.md` (editable markdown source of truth) rather than being
hard-coded here, so the principles can be maintained without touching Python.

The full reference catalogues remain in RAG under the `api_design_knowledge`
category — this block is the distilled "always follow" rules; depth comes from
retrieval. See `knowledge_base/api_design_knowledge/`.
"""
from pathlib import Path

_MD_PATH = Path(__file__).with_name("api_design_principles.md")

try:
    API_DESIGN_PRINCIPLES = _MD_PATH.read_text(encoding="utf-8").strip()
except OSError:
    # Defensive: never break agent prompt assembly if the file is missing.
    # An empty block is a no-op in the f-strings that interpolate it.
    API_DESIGN_PRINCIPLES = ""
