# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Split detected gaps into PM-blocking vs safely assumable.

Applied to the output of `ambiguity_detector.detect()`. A gap is considered
BLOCKING if either:
  1. Its key contains a substring the active domain pack lists under
     `clarification_must_ask_keywords` (the curated set of things the PM must
     own — for UPI: mandate type, auth factor, transaction limit, settlement,
     etc.), OR
  2. The detector marked it `critical=True`.

Everything else is *assumable* — a safe default is attached (the pack's
`default_assumptions` entry for that key, else a generic follow-the-authority
default) and the gap is recorded but not asked.

Which questions block a PM and what the platform silently assumes are DOMAIN
judgement calls, so both lists are pack data, resolved per call (the process is
long-lived and `DOMAIN_PACK` comes from the environment — caching at import
would pin the first domain a worker ever saw). A pack that declares neither
gets no domain-specific blockers and no invented assumptions, which is the
honest reading of "this domain has not said".
"""
import logging

logger = logging.getLogger(__name__)


def _must_ask_keywords() -> tuple[str, ...]:
    from app.core.domain.contract import clarification_must_ask_keywords_of
    from app.core.domain.registry import get_active_pack

    return tuple(clarification_must_ask_keywords_of(get_active_pack()))


def _default_assumptions() -> dict[str, str]:
    from app.core.domain.contract import default_assumptions_of
    from app.core.domain.registry import get_active_pack

    return dict(default_assumptions_of(get_active_pack()))


def _fallback_default() -> str:
    from app.core.domain.registry import prompt_block

    return f"Use {prompt_block('authority', 'platform')} standard conventions"


def apply(gaps: list[dict]) -> tuple[list[str], list[dict]]:
    """Partition gaps into blocking and assumable.

    Args:
        gaps: list of {key, description, critical} from ambiguity_detector.detect()

    Returns:
        (blocking_keys, assumed_gaps)
          blocking_keys: list of gap keys the PM must answer
          assumed_gaps:  list of {key, default, reason} for gaps we're defaulting
    """
    must_ask = _must_ask_keywords()
    defaults = _default_assumptions()
    fallback = _fallback_default()

    blocking: list[str] = []
    assumed: list[dict] = []

    for gap in gaps:
        key = gap.get("key", "")
        if not key:
            continue

        is_must_ask = any(kw in key for kw in must_ask)
        is_critical = bool(gap.get("critical"))

        if is_must_ask or is_critical:
            blocking.append(key)
        else:
            assumed.append({
                "key":     key,
                "default": defaults.get(key, fallback),
                "reason":  "Non-critical; using platform default",
            })

    logger.info("assumption_handler: %d blocking, %d assumed (of %d gaps)",
                len(blocking), len(assumed), len(gaps))
    return blocking, assumed
