# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Split a product-deck LLM response into (markdown_script, DeckOutline).

Contract: the LLM emits the markdown slide-by-slide script first, then
a single fenced ```json fenced block at the end whose content parses
as `DeckOutline`. The parser:

  * Returns the markdown with the JSON fence stripped (so the existing
    .docx renderer produces the script unchanged).
  * Parses the JSON via Pydantic so any malformed shape becomes a
    typed `ValidationError` the caller can downgrade to docx-only.
  * Returns `(markdown, None)` when no JSON block is present —
    back-compat for cached responses generated before D5.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from pydantic import ValidationError

from app.docgen.deck.schema import DeckOutline

logger = logging.getLogger(__name__)


# Match the LAST ```json … ``` fenced block in the response. Greedy on
# the body, anchored at end-of-string-or-trailing-whitespace so we
# don't accidentally capture an earlier block embedded in the
# markdown body (which the LLM occasionally produces as inline
# example fragments).
_JSON_FENCE_RE = re.compile(
    r"\n```json\s*\n(?P<body>.*?)\n```\s*\Z",
    re.DOTALL,
)


def split_markdown_and_json(text: str) -> tuple[str, Optional[DeckOutline]]:
    """Return (markdown_only, parsed_outline_or_None).

    `markdown_only` is the input minus the trailing JSON fence (if
    present). The parsed outline is None when:
      * no JSON fence is present (legacy / older response shapes), or
      * the fence contents fail Pydantic validation.

    Validation failures are logged WARN with the field path so
    Phase A can surface "deck rendered as script-only" diagnostics
    in the UI without parsing logs themselves.
    """
    if not text:
        return text, None

    m = _JSON_FENCE_RE.search(text)
    if not m:
        return text, None

    markdown = text[: m.start()].rstrip() + "\n"
    body = m.group("body").strip()

    try:
        outline = DeckOutline.model_validate_json(body)
    except ValidationError as exc:
        logger.warning(
            "product_deck JSON failed schema validation; falling back to "
            "script-only. errors=%s",
            exc.errors()[:3],
        )
        return markdown, None

    return markdown, outline
