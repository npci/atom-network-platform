# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Source-document injection for Phase A prompts.

A PM can attach a detailed BRD/requirements document at change creation
(`ChangeRequest.source_doc_text`). This module renders it as ONE consistent
prompt block appended wherever the change intent enters Phase A — enhancer,
deep research, product canvas, BRD generation — so every stage works from the
PM's facts instead of assuming.

Design constraints (docs + review, 2026-07-22):
* SEED, not substitute — every Phase A stage still runs; the platform's
  generated BRD remains the canonical artifact. This block is input material.
* Untrusted — the document is user-supplied; it is wrapped with the standard
  delimiters so embedded instructions read as DATA (prompt-injection defense).
* Bounded — capped so a huge upload cannot blow a stage's prompt budget; the
  cap note tells the model the text is truncated rather than complete.
"""
from __future__ import annotations
from app.core.prompts import load_prompt

import re

from app.agents._prompt_safety import wrap_untrusted

# ~150K chars ≈ 37K tokens: fits comfortably in every Phase A prompt alongside
# research/canvas context, and covers a 60-100 page BRD's extracted text.
SOURCE_DOC_MAX_CHARS = 150_000

_PREFACE = load_prompt("services/source_material/preface.md")


def source_block(cr, *, max_chars: int = SOURCE_DOC_MAX_CHARS) -> str:
    """The uploaded source document as a prompt block, or '' when none was attached.
    Append to a Phase A prompt: `intent + source_block(cr)`."""
    text = (getattr(cr, "source_doc_text", None) or "").strip()
    if not text:
        return ""
    name = (getattr(cr, "source_doc_name", None) or "uploaded document").strip()
    # The filename is user-supplied too, and this line sits OUTSIDE the untrusted-data
    # delimiters below — a 500-char name with newlines would smuggle instruction text
    # into every Phase A prompt. Keep it one short inert line.
    name = re.sub(r"[\x00-\x1f\x7f]+", " ", name).strip()[:120] or "uploaded document"
    note = ""
    if len(text) > max_chars:
        note = (f"\n[NOTE: document truncated at {max_chars} chars of "
                f"{len(text)} — the omitted tail is not visible to you]")
    return (f"{_PREFACE}\nFile: {name}{note}\n"
            + wrap_untrusted(text, "SOURCE_DOCUMENT", max_chars=max_chars))
