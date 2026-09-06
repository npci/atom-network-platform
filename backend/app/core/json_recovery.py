# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""4-stage JSON recovery for LLM outputs.

LLMs frequently return invalid JSON when asked for structured data —
trailing commas, unescaped newlines, ```json fences, or just truncated.
This module tries progressively more lenient recovery steps before giving up.

Stages:
  1. Strip markdown fences + strict json.loads
  1b. Sanitize (escape newlines in strings, strip trailing commas) + strict
  2. Ask the LLM to fix its own JSON with the specific error message
  3. Lenient extraction — find the first {...} or [...] block
  Fallback. Return the caller-provided default (or None).
"""
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


_FENCE_RE = re.compile(r"^```(?:json|javascript|js)?\s*\n?|\n?```\s*$", re.MULTILINE)
_FENCED_BLOCK_RE = re.compile(r"```(?:json|javascript|js)?\s*\n(.*?)```", re.DOTALL)
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")

# Stage-2 repair bounds, coupled: the repair re-emits the corrected JSON in full, so the
# char cap must fit inside the token budget (~3 chars/token for JSON → 24K chars ≈ 8K
# tokens). Raise them TOGETHER or the repaired output truncates into fresh malformed JSON.
_REPAIR_MAX_CHARS = 24_000
_REPAIR_MAX_TOKENS = 8_000


def _strip_fences(raw: str) -> str:
    """Remove ```json / ``` markdown fences if present."""
    if not raw:
        return raw
    return _FENCE_RE.sub("", raw).strip()


def _sanitize(raw: str) -> str:
    """Best-effort cleanup of common LLM JSON mistakes.

    - Strip trailing commas before } or ]
    - Escape raw newlines that appear INSIDE string literals
      (naive: only replaces \n inside quoted regions)
    """
    if not raw:
        return raw

    # Strip trailing commas
    cleaned = _TRAILING_COMMA_RE.sub(r"\1", raw)

    # Escape literal newlines inside strings — walk char by char tracking quote state
    out = []
    in_string = False
    escape = False
    for ch in cleaned:
        if escape:
            out.append(ch)
            escape = False
            continue
        if ch == "\\":
            out.append(ch)
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if ch == "\n" and in_string:
            out.append("\\n")
            continue
        if ch == "\r" and in_string:
            out.append("\\r")
            continue
        if ch == "\t" and in_string:
            out.append("\\t")
            continue
        out.append(ch)
    return "".join(out)


def _extract_first_block(raw: str, expect_array: bool = False) -> str | None:
    """Find the first balanced {...} or [...] substring."""
    pattern = _ARRAY_RE if expect_array else _OBJECT_RE
    m = pattern.search(raw)
    return m.group(0) if m else None


def _parse_fenced_blocks(raw: str, expect_array: bool = False) -> Any:
    """Stage 1c: the model reasoned in PROSE and appended its answer as a fenced ```json
    block. Parse each fence separately, LAST first (the final verdict block is the answer).
    Neither earlier stage survives this shape: fence-STRIPPING leaves the prose in place, and
    the greedy first-`[`-to-last-`]` regex spans from bracketed prose (e.g. a literal "[D1]"
    directive reference) — a reviewer run burned its whole fix budget on exactly that.
    Returns the first candidate matching the expected top-level type, else None."""
    for cand in reversed(_FENCED_BLOCK_RE.findall(raw or "")):
        cand = cand.strip()
        for attempt in (cand, _sanitize(cand)):
            try:
                val = json.loads(attempt)
            except ValueError:      # JSONDecodeError + CPython's int-digit-limit on giant numeric blobs
                continue
            if expect_array and not isinstance(val, list):
                break                    # parsed, but not the array — try an earlier fence
            return val
    return None


async def parse_llm_json(
    raw: str,
    *,
    expect_array: bool = False,
    fallback: Any = None,
    llm_self_correct: bool = True,
) -> Any:
    """Parse LLM-produced JSON with progressive recovery.

    Args:
        raw: The raw LLM response string.
        expect_array: Set True when you expect a top-level JSON array.
        fallback: Value returned if all recovery stages fail (default: None).
        llm_self_correct: If True (default), try an extra LLM call to fix the
            output when strict + sanitized parsing both fail. Set False in hot
            paths where the extra LLM call isn't acceptable.

    Returns:
        Parsed dict / list, or the fallback on total failure.
    """
    if not raw:
        logger.warning("parse_llm_json: empty input")
        return fallback

    stripped = _strip_fences(raw)

    # Stage 1: strict
    try:
        return json.loads(stripped)
    except ValueError as e1:    # JSONDecodeError + CPython's int-digit-limit on giant numeric blobs
        stage1_err = str(e1)

    # Stage 1b: sanitize + strict
    sanitized = _sanitize(stripped)
    try:
        return json.loads(sanitized)
    except ValueError:      # JSONDecodeError + CPython's int-digit-limit on giant numeric blobs
        pass

    # Stage 1c: prose-wrapped fenced block(s) — needs the ORIGINAL raw (fences intact),
    # and runs BEFORE the LLM repair call: no point paying a repair turn (whose input is
    # head-truncated, losing a trailing verdict block anyway) for an answer already present.
    fenced = _parse_fenced_blocks(raw, expect_array=expect_array)
    if fenced is not None:
        return fenced

    # Stage 2: ask the LLM to fix its own JSON — ONLY when the whole payload fits.
    # The repair model must RE-EMIT the corrected JSON verbatim, so the input is bounded
    # by the call's own output budget. Never truncate to fit: a repair over a cut-off
    # payload either fails (wasted call) or — worse — helpfully closes the brackets early
    # and returns VALID JSON with the tail data silently dropped. Oversized payloads skip
    # straight to stage 3.
    if llm_self_correct and len(stripped) <= _REPAIR_MAX_CHARS:
        try:
            # Lazy import to avoid circular: json_recovery is imported early in agents
            from app.core.llm import call_llm

            system = (
                "You are a JSON repair tool. The user will give you malformed JSON "
                "and the parse error. Return ONLY the corrected JSON, no explanation, "
                "no markdown fences, no commentary. Preserve all data as-is."
            )
            user_msg = (
                f"Parse error: {stage1_err}\n\n"
                f"Malformed JSON:\n{stripped}"
            )
            corrected = await call_llm(
                system=system,
                messages=[{"role": "user", "content": user_msg}],
                max_tokens=_REPAIR_MAX_TOKENS,
                agent_name="json_recovery",
            )
            corrected = _strip_fences(corrected)
            try:
                return json.loads(corrected)
            except ValueError:      # JSONDecodeError + CPython's int-digit-limit on giant numeric blobs
                # One more chance with sanitization on the corrected output
                try:
                    return json.loads(_sanitize(corrected))
                except ValueError:      # JSONDecodeError + CPython's int-digit-limit on giant numeric blobs
                    pass
        except Exception as e:
            logger.warning("JSON recovery stage 2 (LLM self-correct) failed: %s", e)

    # Stage 3: lenient extraction of first object/array block
    block = _extract_first_block(sanitized, expect_array=expect_array)
    if block:
        try:
            return json.loads(block)
        except ValueError:      # JSONDecodeError + CPython's int-digit-limit on giant numeric blobs
            try:
                return json.loads(_sanitize(block))
            except ValueError:      # JSONDecodeError + CPython's int-digit-limit on giant numeric blobs
                pass

    logger.error(
        "JSON recovery exhausted all stages. First 500 chars: %s",
        stripped[:500],
    )
    return fallback


def parse_llm_json_sync(
    raw: str,
    *,
    expect_array: bool = False,
    fallback: Any = None,
) -> Any:
    """Synchronous variant — skips stage 2 (LLM self-correct).

    Use this in code paths that are already synchronous and can't await.
    """
    if not raw:
        return fallback

    stripped = _strip_fences(raw)

    try:
        return json.loads(stripped)
    except ValueError:      # JSONDecodeError + CPython's int-digit-limit on giant numeric blobs
        pass

    sanitized = _sanitize(stripped)
    try:
        return json.loads(sanitized)
    except ValueError:      # JSONDecodeError + CPython's int-digit-limit on giant numeric blobs
        pass

    fenced = _parse_fenced_blocks(raw, expect_array=expect_array)
    if fenced is not None:
        return fenced

    block = _extract_first_block(sanitized, expect_array=expect_array)
    if block:
        try:
            return json.loads(block)
        except ValueError:      # JSONDecodeError + CPython's int-digit-limit on giant numeric blobs
            try:
                return json.loads(_sanitize(block))
            except ValueError:      # JSONDecodeError + CPython's int-digit-limit on giant numeric blobs
                pass

    logger.error("JSON recovery (sync) exhausted. First 500 chars: %s", stripped[:500])
    return fallback
