# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""BRD-based counter-proposal classifier.

Determines the BRD classification and auto-disposition for an incoming
counter-proposal using the hybrid approach:
  - PM-configured mandatory flags → hard auto-reject
  - Quantitative tolerance config → deterministic in-tolerance check
  - Everything else → Claude evaluates against BRD text

Returns (brd_classification, auto_disposition) for the caller to persist.
"""
import json
from app.core.domain.registry import prompt_block
from app.core.prompts import render_prompt
import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.llm import call_llm
from app.models.phase_c import (
    AutoDisposition,
    BRDClassification,
    BRDRequirement,
    CounterProposal,
    RequestCategory,
)

logger = logging.getLogger(__name__)

# The A2A message type whose content this module classifies. Resolved lazily-ish
# at import with a string fallback so a missing a2a SDK cannot break this module
# — `protocol.carries_pii()` accepts the raw wire string and classifies it
# identically.
try:
    from app.a2a_common.protocol import A2ATaskType as _A2ATaskType
    _CP_TASK_TYPE = _A2ATaskType.COUNTER_PROPOSAL
except Exception:  # noqa: BLE001 — SDK-optional import
    _CP_TASK_TYPE = "counter_proposal"

# Identity nouns come from the active domain pack; under the default UPI pack
# these render byte-identically to the previous hardcoded files.
_TOLERANCE_SYSTEM = render_prompt(
    "agents/negotiation_classifier/tolerance_system.md",
    AUTHORITY=prompt_block("authority", "the ecosystem authority"),
)

_MANDATORY_SYSTEM = render_prompt(
    "agents/negotiation_classifier/mandatory_system.md",
    AUTHORITY=prompt_block("authority", "the ecosystem authority"),
    PARTNER_LABEL=prompt_block("partner_label", "partner"),
)


def _loads_json_object(raw: str) -> dict:
    """Tolerant parse of a single JSON object (strips ``` fences)."""
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s).strip()
    data = json.loads(s)
    return data if isinstance(data, dict) else {}


def _redact_partner_text(text: str, *, field_name: str,
                         task_type=None) -> str:
    """T6 (THREAT_MODEL.md) — strip PII from PARTNER-AUTHORED free text before
    it is embedded in a prompt sent to an external LLM provider.

    This is the exact surface `docs/PII_DATA_CLASSIFICATION.md` §2 classifies
    as PII-POSSIBLE: a partner bank's justification / counter-proposal payload
    is free text the platform does not control, which may reference an account
    holder, a mobile number, or a VPA handle. It is prose, not a
    machine-readable contract, so the aggressive default profile is the right
    choice here (unlike the BRD/TSD path — see
    `context_assembler._redact_doc_sections_for_llm`).

    **Tier 2 interaction (`task_type`).** `pii_redaction_freetext_enabled`
    exists so an operator can tune the HEURISTIC filter on content that only
    might carry PII. It is not a licence to ship a message type classified
    `carries_pii=True` unfiltered. So when `task_type` is a PII-bearing type
    (per `a2a_common/protocol.py`'s design-time classification), redaction is
    applied regardless of that flag. Passing `task_type=None` keeps the old
    flag-respecting behaviour for callers with no message context.

    Fails OPEN (returns the original text) on any error: a redaction bug must
    not break negotiation classification, which would escalate every counter
    proposal to a human and stall the workflow.
    """
    if not text:
        return text
    mandatory = False
    if task_type is not None:
        try:
            from app.a2a_common.protocol import carries_pii
            mandatory = carries_pii(task_type)
        except Exception:  # noqa: BLE001 — classification unavailable → fall back to the flag
            mandatory = False
    if not mandatory and not getattr(settings, "pii_redaction_freetext_enabled", True):
        return text
    try:
        from app.core.pii_redaction import redact_for_llm_prompt
        redacted, count = redact_for_llm_prompt(text, field_name=field_name)
        if count:
            # security_architecture_skills.md §13.2 — structured telemetry;
            # never logs the redacted values themselves, only the count.
            logger.info(
                "SECURITY_EVENT event=pii_redacted_before_llm_call severity=low "
                "field=%s agent=negotiation_classifier redaction_count=%d decision=redacted",
                field_name, count,
            )
        return redacted
    except Exception:  # noqa: BLE001 — best-effort filter, never a blocker
        logger.exception(
            "pii redaction failed for field=%s — passing text through unredacted", field_name)
        return text


def _extract_number(value: Any) -> float | None:
    """Try to parse a numeric value from various shapes (string, int, float)."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = re.search(r"[-+]?\d+(?:\.\d+)?", value)
        if m:
            return float(m.group())
    return None


def _check_date_shift(payload: dict, tolerance_days: int) -> bool | None:
    """Return True if the date shift in payload is within ±tolerance_days.

    Looks for current_date + proposed_date in the payload.
    Returns None if the dates can't be found/parsed.
    """
    from datetime import datetime

    current_raw = payload.get("current_date") or payload.get("current_go_live_date")
    proposed_raw = payload.get("proposed_date") or payload.get("proposed_go_live_date")
    if not (current_raw and proposed_raw):
        return None
    try:
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                current_dt = datetime.strptime(str(current_raw), fmt)
                proposed_dt = datetime.strptime(str(proposed_raw), fmt)
                delta = abs((proposed_dt - current_dt).days)
                return delta <= tolerance_days
            except ValueError:
                continue
    except Exception:
        pass
    return None


def _check_percent_change(payload: dict, max_percent: float) -> bool | None:
    """Return True if the value change is within ±max_percent of current."""
    current = _extract_number(payload.get("current_value"))
    proposed = _extract_number(payload.get("proposed_value"))
    if current is None or proposed is None or current == 0:
        return None
    pct = abs((proposed - current) / current) * 100
    return pct <= max_percent


def _check_absolute_delta(payload: dict, max_delta: float) -> bool | None:
    """Return True if |proposed - current| ≤ max_delta."""
    current = _extract_number(payload.get("current_value"))
    proposed = _extract_number(payload.get("proposed_value"))
    if current is None or proposed is None:
        return None
    return abs(proposed - current) <= max_delta


def _heuristic_in_tolerance(req: BRDRequirement, payload: dict) -> bool | None:
    """Apply deterministic tolerance checks from req.tolerance_config.

    Returns True/False if we can decide; None if the AI should decide.
    """
    if not req.tolerance_config:
        return None
    cfg = req.tolerance_config

    if "date_shift_days" in cfg:
        result = _check_date_shift(payload, int(cfg["date_shift_days"]))
        if result is not None:
            return result

    if "percent_change" in cfg:
        result = _check_percent_change(payload, float(cfg["percent_change"]))
        if result is not None:
            return result

    if "absolute_delta" in cfg:
        result = _check_absolute_delta(payload, float(cfg["absolute_delta"]))
        if result is not None:
            return result

    return None


async def _ai_tolerance_check(
    req: BRDRequirement,
    justification: str,
    payload: dict,
    brd_snippet: str,
) -> bool:
    """Ask Claude whether the request is within tolerance for this requirement."""
    # T6 — redact the two PARTNER-AUTHORED (untrusted, PII-POSSIBLE) fields
    # before they enter the prompt. Platform-authored context (the requirement
    # label/description, the BRD snippet) is left alone.
    # Tier 2: COUNTER_PROPOSAL is classified carries_pii=True, so passing the
    # task type makes redaction mandatory here regardless of the general
    # heuristic-filter flag. See protocol.PII_CLASSIFICATION_RATIONALE.
    safe_justification = _redact_partner_text(
        justification, field_name="counter.justification", task_type=_CP_TASK_TYPE)
    safe_payload = _redact_partner_text(
        json.dumps(payload, default=str), field_name="counter.payload",
        task_type=_CP_TASK_TYPE)
    msg = (
        f"BRD requirement: {req.label}\n"
        f"Description: {req.description or '(not provided)'}\n"
        f"Relevant BRD text: {brd_snippet[:800] if brd_snippet else '(not provided)'}\n\n"
        f"Partner request category: {req.category}\n\n"
        "----- BEGIN PARTNER JUSTIFICATION (untrusted data) -----\n"
        f"{safe_justification}\n"
        "----- END PARTNER JUSTIFICATION -----\n"
        "----- BEGIN PARTNER PAYLOAD (untrusted data) -----\n"
        f"{safe_payload}\n"
        "----- END PARTNER PAYLOAD -----\n\n"
        "Is this request within an acceptable tolerance for this OPTIONAL requirement? "
        "Respond with the JSON object as instructed."
    )
    try:
        raw = await call_llm(
            system=_TOLERANCE_SYSTEM,
            messages=[{"role": "user", "content": msg}],
            max_tokens=200,
            agent_name="negotiation_classifier",
        )
        data = _loads_json_object(raw)
        return bool(data.get("in_tolerance", False))
    except Exception as exc:
        logger.warning("AI tolerance check failed: %s — defaulting to escalated", exc)
        return False


async def _ai_mandatory_violation_check(
    mandatory_reqs: list[BRDRequirement],
    justification: str,
    payload: dict,
    brd_snippet: str,
) -> tuple[bool, str, str]:
    """Ask Claude whether the counter violates any mandatory requirement.

    Returns (violates, requirement_label, reason). Defaults to
    (False, "", "") on any failure so a model error escalates to the PM
    rather than producing a wrongful auto-reject.
    """
    reqs_block = "\n".join(
        f"- {r.label}" + (f": {r.description}" if r.description else "")
        for r in mandatory_reqs
    )
    # T6 — same treatment as _ai_tolerance_check above: only the
    # partner-authored fields are filtered.
    # Tier 2: COUNTER_PROPOSAL is classified carries_pii=True, so passing the
    # task type makes redaction mandatory here regardless of the general
    # heuristic-filter flag. See protocol.PII_CLASSIFICATION_RATIONALE.
    safe_justification = _redact_partner_text(
        justification, field_name="counter.justification", task_type=_CP_TASK_TYPE)
    safe_payload = _redact_partner_text(
        json.dumps(payload, default=str), field_name="counter.payload",
        task_type=_CP_TASK_TYPE)
    msg = (
        f"Mandatory (non-negotiable) requirements:\n{reqs_block}\n\n"
        f"Relevant BRD text: {brd_snippet[:1500] if brd_snippet else '(not provided)'}\n\n"
        "----- BEGIN PARTNER JUSTIFICATION (untrusted data) -----\n"
        f"{safe_justification}\n"
        "----- END PARTNER JUSTIFICATION -----\n"
        "----- BEGIN PARTNER PAYLOAD (untrusted data) -----\n"
        f"{safe_payload}\n"
        "----- END PARTNER PAYLOAD -----\n\n"
        "Does this request violate any of the mandatory requirements above? "
        "Respond with the JSON object as instructed."
    )
    try:
        raw = await call_llm(
            system=_MANDATORY_SYSTEM,
            messages=[{"role": "user", "content": msg}],
            max_tokens=200,
            agent_name="negotiation_classifier",
        )
        data = _loads_json_object(raw)
        return (
            bool(data.get("violates", False)),
            str(data.get("requirement") or ""),
            str(data.get("reason") or ""),
        )
    except Exception as exc:
        logger.warning("AI mandatory-violation check failed: %s — defaulting to no-violation (escalate)", exc)
        return False, "", ""


def _topic_slug(justification: str, max_words: int = 4) -> str:
    """Derive a stable short slug from the first N words of justification."""
    words = re.sub(r"[^a-z0-9 ]", "", justification.lower()).split()
    return "_".join(words[:max_words])


async def classify_counter_proposal(
    cp: CounterProposal,
    db: Session,
    brd_text: str = "",
) -> tuple[str, str, dict | None]:
    """Classify a CounterProposal against the BRD configuration.

    Returns (brd_classification, auto_disposition, detail).

    `detail` carries WHICH mandatory requirement was violated and the assessor's reason
    ({"requirement": label, "reason": text}) on a MANDATORY_VIOLATION, else None. It used
    to be logged and thrown away, so the partner's rejection message could only say
    "something mandatory" — leaving them no idea what to change.
    """
    category = cp.request_category or ""
    justification = cp.justification or ""
    payload = cp.payload or {}

    # Load every requirement for this change. Mandatory-violation detection
    # considers ALL mandatory requirements regardless of category, so a
    # counter that arrived without a request_category (e.g. from the free-text
    # chat composer) is still evaluated against them. The optional tolerance
    # path below stays category-scoped because tolerance configs are per-topic.
    all_reqs: list[BRDRequirement] = (
        db.query(BRDRequirement)
        .filter(BRDRequirement.change_request_id == cp.change_request_id)
        .all()
    )

    if not all_reqs:
        # No BRD requirements configured → the mandatory guard CANNOT RUN. This is not a
        # benign "nothing matched": every counter-proposal escalates unchecked, so a
        # mandatory-violating ask looks identical to a compliant one. Warn loudly and
        # raise an operator alert, otherwise the inert guard is indistinguishable from a
        # guard that ran and passed.
        logger.warning(
            "Classifier: BRD GUARD INACTIVE — change=%s has NO brd_requirements rows, so "
            "cp=%s was escalated WITHOUT any mandatory-violation check. Generate them via "
            "POST /changes/%s/brd-requirements/generate to enforce mandatory items.",
            cp.change_request_id, cp.id, cp.change_request_id,
        )
        try:
            from app.services.notifications import notify_brd_guard_inactive
            notify_brd_guard_inactive(db, change_id=cp.change_request_id, cp_id=cp.id)
        except Exception:  # noqa: BLE001 — alerting must never break classification
            logger.exception("brd-guard-inactive notification failed")
        return BRDClassification.UNCATEGORIZED.value, AutoDisposition.ESCALATED_TO_PM.value, None

    mandatory = [r for r in all_reqs if r.is_mandatory]
    # Optional requirements — category-scoped: same category or "general".
    optional = [
        r for r in all_reqs
        if not r.is_mandatory and r.category in (category, "general")
    ]

    # ── 1. Deterministic tolerance heuristics first (no LLM, free) ─────────
    # date_shift / percent_change / absolute_delta are exact and cost nothing,
    # so we resolve them before spending any LLM call. The first requirement
    # whose tolerance_config conclusively decides wins; an inconclusive (None)
    # result is skipped so a *later* optional requirement can still decide —
    # the old code only consulted the first optional req and otherwise jumped
    # straight to the LLM, paying for a call a heuristic could have answered.
    heuristic_in_tol: bool | None = None
    for req in optional:
        h = _heuristic_in_tolerance(req, payload)
        if h is not None:
            heuristic_in_tol = h
            break

    # ── 2. Mandatory gate (LLM) — only when mandatory reqs exist ───────────
    # Must run before any auto-accept: a change can sit inside an optional
    # tolerance yet still violate a mandatory requirement. Presence of a
    # mandatory requirement does not auto-reject on its own — the LLM judges
    # whether THIS counter actually targets one (catches category-less
    # counters; spares unrelated ones that merely share a category). On any
    # LLM/parse failure the check returns "no violation", so errors escalate
    # to the PM rather than producing a wrongful auto-reject.
    if mandatory:
        violates, req_label, reason = await _ai_mandatory_violation_check(
            mandatory, justification, payload, brd_text
        )
        if violates:
            logger.info(
                "Classifier: LLM flagged mandatory violation (req=%s) — auto-rejecting cp=%s: %s",
                req_label, cp.id, reason,
            )
            # Carry the requirement + reason out so the rejection sent to the partner can
            # name what was violated instead of a generic "it's mandatory".
            return (BRDClassification.MANDATORY_VIOLATION.value,
                    AutoDisposition.AUTO_REJECTED.value,
                    {"requirement": req_label, "reason": reason})
        logger.info("Classifier: LLM cleared mandatory reqs for cp=%s — checking tolerance", cp.id)

    # ── 3. Tolerance decision ─────────────────────────────────────────────
    # Mandatory cleared (or none present). Prefer the free heuristic verdict;
    # only spend the tolerance LLM call when no heuristic could decide.
    if heuristic_in_tol is True:
        return BRDClassification.OPTIONAL_IN_TOLERANCE.value, AutoDisposition.ESCALATED_TO_PM.value, None
    if heuristic_in_tol is False:
        return BRDClassification.OPTIONAL_ESCALATED.value, AutoDisposition.ESCALATED_TO_PM.value, None

    if optional:
        in_tol = await _ai_tolerance_check(optional[0], justification, payload, brd_text)
        if in_tol:
            return BRDClassification.OPTIONAL_IN_TOLERANCE.value, AutoDisposition.ESCALATED_TO_PM.value, None
        return BRDClassification.OPTIONAL_ESCALATED.value, AutoDisposition.ESCALATED_TO_PM.value, None

    # No matching requirements → escalate
    return BRDClassification.UNCATEGORIZED.value, AutoDisposition.ESCALATED_TO_PM.value, None
