# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Shared agent helpers: prompt loader and tolerant JSON parsing."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from app.excel_testcase_engine.config import PACKAGE_ROOT

PROMPTS_DIR = PACKAGE_ROOT / "prompts"


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """Read a versioned prompt file from the prompts/ directory."""

    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def retry_message(base: str, correction: str) -> str:
    """Re-send the whole original request with a correction appended.

    WHY this exists (2026-09-05): every agent retry loop in this package used
    to *replace* its user message with the bare correction. The source
    documents and the field-by-field schema description live ONLY in that
    first message, so each retry ran with strictly less information than the
    attempt it was correcting — measured at 46k chars -> 5k in the planner and
    54k -> 3k in the enhancer. Blind, the model would then invent field names
    or return nothing at all, and the run died reporting whatever the last
    attempt produced rather than the real problem.

    Appending keeps the documents and the schema in front of the model on
    every attempt.
    """
    return (
        f"{base}\n\n"
        "## Correction — your previous attempt was rejected\n"
        f"{correction}\n"
        "Re-read the requirements above and return the corrected JSON."
    )


_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def parse_json_response(text: str) -> dict | list:
    """Tolerant JSON parser that strips fences and surrounding prose.

    Falls back to NDJSON / concatenated-object parsing when strict JSON
    fails. Observed in the writer (2026-05-06): the LLM sometimes emits
    one top-level object per line (or `{...}{...}` concatenated) instead
    of a JSON array, producing `JSONDecodeError('Extra data: ...')`.
    The fallback recovers each object independently and returns a list.
    Handles three failure modes seen in production:
      1. ```json … ``` markdown fences (stripped).
      2. Leading prose before the JSON object/array starts (skipped).
      3. Trailing prose / a second JSON object after the first one
         (`JSONDecodeError: Extra data`). We use raw_decode so json.loads
         consumes only the first valid object and ignores the trailing
         garbage. This was tripping the engine writer when the model
         emitted commentary after its rendered_cases JSON.
    """

    text = text.strip()
    match = _FENCE_RE.match(text)
    if match:
        text = match.group(1).strip()
    if not text.startswith("{") and not text.startswith("["):
        first_obj = text.find("{")
        first_arr = text.find("[")
        candidates = [pos for pos in (first_obj, first_arr) if pos != -1]
        if candidates:
            text = text[min(candidates):]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fallback 1 — NDJSON (one JSON object per line).
        ndjson_objs: list = []
        for line in text.splitlines():
            line = line.strip().rstrip(",")
            if not line or line in ("[", "]"):
                continue
            try:
                ndjson_objs.append(json.loads(line))
            except json.JSONDecodeError:
                ndjson_objs = []
                break
        if ndjson_objs:
            return ndjson_objs
        # Fallback 2 — concatenated top-level objects via raw_decode.
        # Handles `{...}{...}{...}` with no separators or trailing `]`.
        decoder = json.JSONDecoder()
        objs: list = []
        idx = 0
        n = len(text)
        while idx < n:
            while idx < n and text[idx].isspace():
                idx += 1
            if idx >= n:
                break
            try:
                obj, end = decoder.raw_decode(text, idx)
            except json.JSONDecodeError:
                break
            objs.append(obj)
            idx = end
        if objs:
            return objs if len(objs) > 1 else objs[0]
        # Re-raise original error so callers see the genuine parse failure.
        raise

    # Use raw_decode so trailing garbage after the first valid JSON value
    # doesn't raise. Matches the "first balanced object wins" intent the
    # callers (planner / writer / validator) all share.
    decoder = json.JSONDecoder()
    obj, _end = decoder.raw_decode(text)
    return obj


def write_run_artifact(name: str, payload: object) -> Path:
    """Persist intermediate stage output for forensic debugging."""

    path = Path("outputs") / "artifacts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    serialised = payload if isinstance(payload, str) else json.dumps(
        payload if not hasattr(payload, "model_dump") else payload.model_dump(),  # type: ignore[arg-type]
        ensure_ascii=True,
        indent=2,
    )
    path.write_text(serialised, encoding="utf-8")
    return path
