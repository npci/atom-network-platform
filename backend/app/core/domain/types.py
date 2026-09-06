# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Document-structure types — the shape of a blueprint, not its content.

This is the first seam between document MACHINERY (which stays in core and is
domain-neutral) and document CONTENT (which is NET-specific and becomes pack
data). The types moved here verbatim from `app.agents.blueprints`; the
blueprint dicts themselves deliberately did not.

A blueprint describes the canonical section structure for a document type, and
is used in three places:

  1. System prompts: the LLM receives an authoritative section list to follow.
  2. Validator: post-generation checks verify every required section appears
     (and, for BRD, the blueprint helps enforce min FR count).
  3. UI (future): surface the expected structure before generation.

Schema of each section:
  key:              stable identifier (not shown to users)
  heading:          display heading — must match what the LLM emits
  instructions:     one-line description of what the section must contain
  min_paragraphs:   recommended minimum body paragraphs
  include_table:    True if section must contain a markdown table
  numbered_list:    True if section must contain a numbered list (FR-##)
  required:         if False, the section is optional

────────────────────────────────────────────────────────────────────────────
IMPORTANT — there is a SECOND, incompatible document schema in this codebase.

`app.docgen.document_guides` drives the LangGraph .docx pipeline and uses a
different and much richer shape. It is NOT a stale duplicate of this one; the
two describe different pipelines:

    this module              app.docgen.document_guides
    ───────────              ──────────────────────────
    key                      section_key
    instructions             content_instructions + prompt_instruction
    min_paragraphs           (absent)
    numbered_list            (absent)
    required                 (absent)
    (absent)                 level, render_style, include_diagram,
                             diagram_type, diagram_description
    (absent)                 document-level: subtitle, tone, audience,
                             include_cover_page, include_toc, plus a whole
                             circular/signatory vocabulary

They share only `title`, `doc_type`, `sections`, `heading` and `include_table`.
Both define a constant named `BRD_BLUEPRINT`, with different content AND a
different schema.

Do NOT unify them by widening this TypedDict until someone decides which
pipeline is authoritative — that decision is the whole of PR #6. Forcing one
type over both now would encode the ambiguity into the pack contract instead
of resolving it.

Note that no type checker runs in CI today (mypy/pyright are not dependencies
and no config exists), so these TypedDicts are documentation and IDE support
rather than an enforced constraint. That is worth changing, but it is not this
PR's job.
"""
from __future__ import annotations

from typing_extensions import TypedDict


class Section(TypedDict, total=False):
    key: str
    heading: str
    instructions: str
    min_paragraphs: int
    include_table: bool
    numbered_list: bool
    required: bool


class Blueprint(TypedDict, total=False):
    title: str
    doc_type: str
    sections: list[Section]
