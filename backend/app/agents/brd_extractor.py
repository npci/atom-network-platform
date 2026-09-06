# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""BRD requirement extractor + classifier.

Reads a change's BRD markdown and returns a list of discrete requirements,
each classified as mandatory or optional with a one-line rationale and a
category drawn from the fixed negotiation taxonomy. The PM reviews/edits the
result in the Negotiation Hub; the negotiation_classifier agent then uses the
mandatory flag + tolerance to auto-dispose partner counter-proposals.

Pure function over text → list[dict]; persistence is the caller's job.
"""
import json
import logging
import re

from app.core.domain.contract import cert_vocabulary_of, change_operations_of, participants_of
from app.core.domain.registry import get_active_pack, prompt_block
from app.core.llm import call_llm
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE

logger = logging.getLogger(__name__)

# Must match RequestCategory enum values + the "general" catch-all the
# classifier filters on.
_ALLOWED_CATEGORIES = {
    "timeline", "scope", "limits", "api_contract", "dependency", "cert_role", "general",
}

_SYSTEM = (
    f'You are a change-management analyst at {prompt_block("authority", "the platform operator")} '
    'reading a Business Requirements Document (BRD) for a change that partners must implement.\n'
) + """

Extract the DISTINCT, negotiable requirements the BRD imposes on partners. For each one decide whether it is MANDATORY (non-negotiable — a regulatory, security, interoperability, or hard-deadline constraint that a partner cannot be allowed to change) or OPTIONAL (a target a partner could reasonably propose to adjust, e.g. a soft timeline, a configurable limit, an optional scope item).

Assign each requirement exactly one category:
  - timeline      go-live dates, milestones, cutover windows
  - scope         features / flows in or out of the change
  - limits        numeric thresholds, transaction/amount limits, rates
  - api_contract  request/response schemas, fields, endpoints, error codes
  - dependency    upstream/downstream systems, third parties, prerequisites
  - cert_role     certification responsibilities, test ownership
  - general       anything that doesn't fit the above

For OPTIONAL requirements that are quantitative, optionally suggest a tolerance the classifier can auto-check:
  - {"date_shift_days": N}    a go-live date may move by up to N days
  - {"percent_change": N}     a numeric value may change by up to N%
  - {"absolute_delta": N}     a numeric value may change by up to ±N
Omit tolerance for mandatory or non-quantitative requirements.

""" + ANTI_INJECTION_CLAUSE + """

Respond with ONLY a JSON array (no prose, no markdown fences). Each element:
{
  "label": "short noun phrase (<= 12 words)",
  "description": "one sentence of the requirement as stated in the BRD",
  "category": "one of the categories above",
  "is_mandatory": true|false,
  "rationale": "one sentence: why mandatory or why optional",
  "tolerance_config": {...} | null
}
Return between 3 and 15 requirements — the most decision-relevant ones. If the text has no usable requirements, return []."""


def _coerce(items: list, max_items: int = 15) -> list[dict]:
    """Validate + normalize the model's array into clean requirement dicts."""
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        label = (it.get("label") or "").strip()
        if not label:
            continue
        category = (it.get("category") or "general").strip().lower()
        if category not in _ALLOWED_CATEGORIES:
            category = "general"
        tol = it.get("tolerance_config")
        if not isinstance(tol, dict) or not tol:
            tol = None
        out.append({
            "label":            label[:200],
            "description":      (it.get("description") or "").strip() or None,
            "category":         category,
            "is_mandatory":     bool(it.get("is_mandatory", False)),
            "rationale":        (it.get("rationale") or "").strip() or None,
            "tolerance_config": tol,
        })
        if len(out) >= max_items:
            break
    return out


def _parse_array(raw: str) -> list:
    """Tolerant JSON-array parse: strip ``` fences, fall back to first [...]."""
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s).strip()
    try:
        data = json.loads(s)
        return data if isinstance(data, list) else data.get("requirements", [])
    except (json.JSONDecodeError, AttributeError):
        m = re.search(r"\[.*\]", s, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                return []
        return []


async def extract_brd_requirements(brd_text: str) -> list[dict]:
    """Extract + classify requirements from BRD markdown.

    Returns a list of normalized requirement dicts (see _coerce). Empty list
    on no usable content or LLM/parse failure — the caller surfaces that.
    """
    text = (brd_text or "").strip()
    if not text:
        return []
    # Cap the prompt to stay well inside the model context. Real BRDs run
    # 70-85 KB; the old 24 KB cap silently dropped ~70% of the document (and
    # any requirement defined past char 24 000). 120 KB (~30 K tokens) covers
    # the production BRDs with headroom and still leaves ample room for the
    # response inside Sonnet's 200 K window. Larger BRDs would want chunking.
    snippet = text[:120000]
    if len(text) > 120000:
        logger.warning(
            "BRD extraction: input truncated %d→120000 chars; requirements past "
            "the cap are not seen. Consider chunking for BRDs this large.",
            len(text),
        )
    wrapped_snippet = wrap_untrusted(snippet, "BRD_DOCUMENT", max_chars=120000)
    try:
        raw = await call_llm(
            system=_SYSTEM,
            messages=[{"role": "user", "content": f"BRD document:\n\n{wrapped_snippet}"}],
            # The model needs ~2.1 K tokens to emit a full 15-item array with
            # descriptions + rationales. The old 2 000 cap clipped the JSON
            # mid-array, so `_parse_array` returned [] — a SILENT total failure
            # on every real BRD. 8 000 leaves comfortable headroom.
            max_tokens=8000,
            agent_name="brd_extractor",
        )
    except Exception as exc:
        logger.warning("BRD extraction LLM call failed: %s", exc)
        return []
    reqs = _coerce(_parse_array(raw))
    # Surface the truncation/format failure mode instead of masquerading as
    # "no requirements in the BRD": if the model emitted output we couldn't
    # parse into any requirement, that's a bug to investigate, not an empty BRD.
    if not reqs and raw.strip():
        logger.warning(
            "BRD extraction: model returned %d chars but 0 requirements parsed "
            "(truncated or malformed JSON?). Output tail: %r",
            len(raw), raw.strip()[-120:],
        )
    logger.info("BRD extraction: parsed %d requirement(s)", len(reqs))
    return reqs


# ── v1 feature-criteria extractor ────────────────────────────────────────────
#
# Second-pass extraction that pulls per-FR "what new feature-specific tag/value
# does this FR introduce" markers out of the BRD. Feeds the excel testcase
# engine's Writer so its DETAILS prose references the specific tag + PM-
# confirmed error code, rather than generic "happy path" boilerplate.

# Canonical vocabulary — sourced from the active pack's cert vocabulary +
# authority (was a hardcoded UPI-only set; see docs/genericization sweep).
# For UPI this evaluates to the exact prior literal set, byte-for-byte.
_pack = get_active_pack()
_authority_participant = next((p for p in participants_of(_pack) if p.is_authority), None)
_authority_key = _authority_participant.label.upper() if _authority_participant else None
_FC_PARTIES = {k for k, _lbl in cert_vocabulary_of(_pack).parties()} | (
    {_authority_key} if _authority_key else set()
)
_FC_OPERATIONS = {o.key for o in change_operations_of(_pack)} | {"meta_query"}

_fc_parties_str = ", ".join(sorted(_FC_PARTIES))
_fc_operations_str = ", ".join(sorted(_FC_OPERATIONS))

_FEATURE_CRITERIA_SYSTEM = (
    f"You are a test-case designer at {prompt_block('authority', 'the platform operator')} reading a "
    f"Business Requirements Document (BRD) for a {prompt_block('domain_name', 'platform')} change.\n\n"
    "Your job: identify the NEW FEATURE-SPECIFIC tags, fields, or values this BRD introduces that a "
    "specific party must send / echo / validate. These are the criteria a cert test case must verify.\n\n"
    f"Focus ONLY on NEW behaviour — do not extract requirements about existing "
    f"{prompt_block('domain_name', 'platform')} behaviour that would already be certified by "
    f"{prompt_block('authority', 'the platform operator')}'s baseline cert pack.\n\n"
    "For each new feature-specific criterion, output:\n"
) + """{
  "fr_id":                "FR-05",   // the human-facing FR-NN id from BRD Section 6 (Functional Requirements) — or a short synthetic id if BRD doesn't number them
  "fr_label":             "one-line description of the FR",
  "tag_name":             "newFieldName",   // the new XML tag / JSON field / value marker the FR introduces
  "expected_value_shape": "integer, > 0",   // one-line describing what the value must look like
  "responsible_party":    "EXAMPLE_PARTY",   // ONE of the canonical parties listed below — the party that must SEND the tag
  "operation":            "EXAMPLE_OPERATION",   // ONE of the canonical operations listed below
  "success_criterion":    "one sentence describing the passing behaviour",
  "failure_scenario":     "one sentence describing the failing behaviour",
  "error_code_placeholder": "PM_CONFIRM_FEATURE_DECLINE"   // literal string — PM fills this during Clarification
}

""" + (
    f"Rules:\n"
    f"- ONLY extract FRs that introduce a NEW tag/field/value/behavior. Skip FRs that only modify "
    f"existing behavior (e.g. \"change the timeout from 30s to 45s\") — those don't need per-tag criteria.\n"
    f"- ONLY use the canonical party vocabulary ({_fc_parties_str}) and operation vocabulary "
    f"({_fc_operations_str}) above. If the FR names an actor or operation outside those lists, use the "
    f"closest canonical mapping; if none fits, drop the row rather than fabricate.\n"
    "- LEAVE `error_code_placeholder` as the literal string \"PM_CONFIRM_FEATURE_DECLINE\" — it's a "
    "marker the PM must resolve during Clarification.\n"
    "- If the BRD introduces zero new tags/fields, return an empty array. Do NOT fabricate criteria.\n\n"
) + ANTI_INJECTION_CLAUSE + """

Respond with ONLY a JSON array (no prose, no markdown fences). Return between 0 and 10 criteria — one per NEW tag/field introduced. If nothing new is introduced, return []."""


def _coerce_feature_criteria(items: list, max_items: int = 10) -> list[dict]:
    """Validate + normalize per-FR feature criteria dicts."""
    out: list[dict] = []
    seen_fr_ids: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        fr_id = str(it.get("fr_id") or "").strip()
        tag_name = str(it.get("tag_name") or "").strip()
        # Must have at least an fr_id + tag_name to be meaningful.
        if not fr_id or not tag_name:
            continue
        # Dedupe on fr_id — only keep the first criterion per FR.
        if fr_id in seen_fr_ids:
            continue
        seen_fr_ids.add(fr_id)

        # Normalise whitespace/hyphens to underscores so "Issuer Bank",
        # "issuer-bank" and "ISSUER_BANK" all hit the same alias key.
        party_raw = str(it.get("responsible_party") or "").strip().upper()
        party = re.sub(r"[\s\-]+", "_", party_raw)
        if party not in _FC_PARTIES:
            # Best-effort remap for common aliases the LLM might emit — the
            # alias table is pack data (`party_aliases`), so a library-network
            # BRD is never remapped through a payments synonym. An alias that
            # resolves outside the canonical set is treated as unknown.
            from app.core.domain.contract import party_aliases_of
            party = party_aliases_of(_pack).get(party, "")
            if party not in _FC_PARTIES:
                party = ""
            # If still unknown, drop the row rather than fabricate.
            if not party:
                logger.warning(
                    "feature_criteria: dropping fr_id=%s with unknown party=%r",
                    fr_id, it.get("responsible_party"),
                )
                continue

        operation = str(it.get("operation") or "").strip().lower()
        if operation not in _FC_OPERATIONS:
            _op_aliases = {
                "reversal":    "debit_reversal",   # ambiguous — default to debit side
                "authorisation": "auth",
                "authorization": "auth",
                "meta":        "meta_query",
                "query":       "meta_query",
            }
            operation = _op_aliases.get(operation, "")
            # An alias that resolves outside the pack's operation set is
            # unknown — never emit another domain's operation key.
            if operation not in _FC_OPERATIONS:
                operation = ""
            if not operation:
                logger.warning(
                    "feature_criteria: dropping fr_id=%s with unknown operation=%r",
                    fr_id, it.get("operation"),
                )
                continue

        out.append({
            "fr_id":                fr_id[:32],
            "fr_label":             (str(it.get("fr_label") or "").strip() or None),
            "tag_name":             tag_name[:120],
            "expected_value_shape": (str(it.get("expected_value_shape") or "").strip() or None),
            "responsible_party":    party,
            "operation":            operation,
            "success_criterion":    (str(it.get("success_criterion") or "").strip() or None),
            "failure_scenario":     (str(it.get("failure_scenario") or "").strip() or None),
            "error_code_placeholder": "PM_CONFIRM_FEATURE_DECLINE",
        })
        if len(out) >= max_items:
            break
    return out


async def extract_brd_feature_criteria(brd_text: str) -> list[dict]:
    """Extract per-FR feature criteria from a BRD.

    Returns a list of `{fr_id, tag_name, responsible_party, operation, ...}`
    dicts — one per NEW feature-specific tag/field/value the BRD introduces.
    Empty list when the BRD doesn't introduce new tags OR on LLM / parse failure
    — callers should treat empty as "generic-behaviour case, no new-tag prose"
    per v1 backward-compat rules.
    """
    text = (brd_text or "").strip()
    if not text:
        return []
    snippet = text[:120000]
    if len(text) > 120000:
        logger.warning(
            "feature_criteria extraction: input truncated %d->120000 chars; "
            "criteria past the cap are not seen.",
            len(text),
        )
    wrapped_snippet = wrap_untrusted(snippet, "BRD_DOCUMENT", max_chars=120000)
    try:
        raw = await call_llm(
            system=_FEATURE_CRITERIA_SYSTEM,
            messages=[{"role": "user", "content": f"BRD document:\n\n{wrapped_snippet}"}],
            # 10 criteria * ~250 tokens each = 2500 tokens; 6000 gives headroom.
            max_tokens=6000,
            agent_name="brd_feature_criteria",
        )
    except Exception as exc:
        logger.warning("feature_criteria extraction LLM call failed: %s", exc)
        return []
    criteria = _coerce_feature_criteria(_parse_array(raw))
    if not criteria and raw.strip():
        logger.warning(
            "feature_criteria: model returned %d chars but 0 criteria parsed. "
            "Output tail: %r",
            len(raw), raw.strip()[-120:],
        )
    logger.info("feature_criteria extraction: parsed %d criterion(a)", len(criteria))
    return criteria
