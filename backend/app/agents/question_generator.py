# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Convert blocking gap keys into PM-friendly questions.

Pattern adapted from `Downloads/RAG_SYSTEM-main/reasoning/question_generator.py`.
Keeps the output capped at 7 questions, orders blocking first, and writes
questions in domain voice ("Should we cap mandate amount at INR X?" rather
than "What is amount_cap?").

v1 adds a programmatic dispatch for 5 signal gap keys that need STRUCTURED
answers (parties involved, feature operations, risk profile, compliance
sensitivity, scope-specific error codes). Those bypass the LLM entirely and
emit closed-set questions with `signal_key` + `options` fields so
`clarification_loader.get_structured_answer` can extract typed values.
"""
import logging
import uuid

from app.agents._prompt_safety import ANTI_INJECTION_CLAUSE, wrap_untrusted
from app.core.domain.contract import (
    change_operations_of, cert_vocabulary_of, compliance_levels_of, risk_levels_of,
)
from app.core.domain.registry import get_active_pack, prompt_block
from app.core.llm import call_llm
from app.core.json_recovery import parse_llm_json
from app.services.clarification_loader import SCOPE_SIGNAL_QK_PREFIX, op_scope_gap_key

logger = logging.getLogger(__name__)


def _as_choices(options) -> list[tuple[str, str]]:
    """A pack's `LabeledOption`s as (key, "Label — description") pairs — the
    shape `_mk_single_select_question`/`_mk_multi_select`-style helpers expect."""
    return [(o.key, f"{o.label} — {o.description}" if o.description else o.label)
            for o in options]


# ── v1 structured signal dispatch ────────────────────────────────────────────
#
# When ambiguity_detector surfaces one of these gap keys, we emit closed-set
# questions programmatically — no LLM call, no free-text answers. The
# `signal_key` field on each question tells clarification_loader how to
# extract the typed answer downstream.
#
# All four vocabularies below are sourced from the active domain pack (see
# app.core.domain.contract) rather than hardcoded here — this module used to
# be the canonical (and only) copy of UPI's party/operation/risk/compliance
# lists; `party_inference.py`, `brd_extractor.py` and
# `services/brd_requirements.py` carried their own separate copies. All four
# now read the SAME pack data. A pack that declares no operations (or no risk/
# compliance tiers) legitimately produces an empty list here — the fan-out
# below then asks zero questions for that gap, rather than reusing another
# domain's vocabulary.
_pack = get_active_pack()
_PARTIES: list[tuple[str, str]] = cert_vocabulary_of(_pack).parties()
_OPERATIONS: list[tuple[str, str]] = _as_choices(change_operations_of(_pack))
_RISK_PROFILES: list[tuple[str, str]] = _as_choices(risk_levels_of(_pack))
_COMPLIANCE_LEVELS: list[tuple[str, str]] = _as_choices(compliance_levels_of(_pack))

_AUTHORITY = prompt_block("authority", "the platform")
# The non-"standard" compliance tier names, for the "raises the cap when ..."
# question hint — was hardcoded "RBI-mandated / PMLA-touched" for UPI.
_ELEVATED_COMPLIANCE_LABELS = ", ".join(
    label.split(" — ", 1)[0] for key, label in _COMPLIANCE_LEVELS if key != "standard"
) or "a higher compliance tier"


def _mk_yesno_question(gap_key: str, signal_key: str, text: str, category: str = "scope") -> dict:
    return {
        "id":         str(uuid.uuid4()),
        "text":       text,
        "gap_key":    gap_key,
        "signal_key": signal_key,
        "required":   True,
        "category":   category,
        "kind":       "yes_no",
        "options":    [
            {"value": "yes", "label": "Yes — in scope"},
            {"value": "no",  "label": "No — out of scope"},
        ],
    }


def _mk_single_select_question(
    gap_key: str,
    signal_key: str,
    text: str,
    choices: list[tuple[str, str]],
    category: str = "scope",
) -> dict:
    return {
        "id":         str(uuid.uuid4()),
        "text":       text,
        "gap_key":    gap_key,
        "signal_key": signal_key,
        "required":   True,
        "category":   category,
        "kind":       "single_select",
        "options":    [{"value": v, "label": lbl} for v, lbl in choices],
    }


def build_scope_signal_questions(
    party_inference: dict | None = None,
) -> list[dict]:
    """Build the PM scope-signal questions in the AGENTIC clarification shape.

    Unlike `_expand_signal_gap` (legacy REST `{value,label}` options), these carry
    an `id` on every option so `AnalysisPanel` can bind `chosen_option_id` and
    `decide_clarifications` can resolve the choice. Each question's own `id` is
    `scope_signal::<gap_key>` so the decision-ledger `question_key` is stable and
    recognisable to `clarification_loader.get_scope_signals`. Option `id` is the
    canonical value the reader recovers ("yes"/"no", "low"/"high", …).

    ``party_inference`` — v3 optional input: the cached
    ``PartyInferenceResult.model_dump()`` from ``context_cache``. When present,
    the four-yes/no party fan-out is replaced by a SINGLE multi-select
    question with the inferred parties pre-checked (`recommended_ids`). When
    absent (call sites that haven't been threaded through the cache yet, OR
    inference failed hard), we still emit the multi-select but pre-check all
    four canonical parties as the safe default.

    Excludes the free-text `scope_error_codes` question: the agentic completeness
    gate requires every asked question to have an answer, and an empty error-code
    map is the safe default. The dedicated widget is a later PR.
    """
    def _yesno(gap_key: str, signal_key: str, text: str) -> dict:
        return {
            "id":         f"{SCOPE_SIGNAL_QK_PREFIX}{gap_key}",
            "text":       text,
            "signal_key": signal_key,
            "gap_key":    gap_key,
            "kind":       "yes_no",
            "options":    [
                {"id": "yes", "label": "Yes — in scope"},
                {"id": "no",  "label": "No — out of scope"},
            ],
            "recommended": "no",
        }

    def _single_select(gap_key: str, signal_key: str, text: str,
                       choices: list[tuple[str, str]], recommended: str) -> dict:
        return {
            "id":          f"{SCOPE_SIGNAL_QK_PREFIX}{gap_key}",
            "text":        text,
            "signal_key":  signal_key,
            "gap_key":     gap_key,
            "kind":        "single_select",
            "options":     [{"id": v, "label": lbl} for v, lbl in choices],
            "recommended": recommended,
        }

    questions: list[dict] = []

    # v3 — one multi-select "parties involved" question, pre-checked from the
    # inference. Replaces the four independent yes/no questions the PM used to
    # answer manually. See _mk_multi_select_question for the widget contract.
    inferred_keys = list((party_inference or {}).get("parties_in_scope") or [])
    if not inferred_keys:
        # Fail-open default: no inference (or inference failed hard) → all four
        # canonical parties pre-checked so the PM sees the full menu.
        inferred_keys = [k for k, _lbl in _PARTIES]
    _rationale = str((party_inference or {}).get("rationale") or "").strip()
    _rationale_line = f"\n\nRationale: {_rationale}" if _rationale else ""
    inferred_display = ", ".join(
        label for key, label in _PARTIES if key in set(inferred_keys)
    ) or "none"
    questions.append({
        "id":              f"{SCOPE_SIGNAL_QK_PREFIX}parties_in_scope",
        "text":            (
            f"Based on our analysis, the following parties look involved in "
            f"this change: **{inferred_display}**. Confirm the parties in "
            f"scope — uncheck any that aren't, check any missing. Only "
            f"in-scope parties get test-case sheets." + _rationale_line
        ),
        "signal_key":      "certifying_parties",
        "gap_key":         "parties_in_scope",
        "kind":            "multi_select",
        "options":         [{"id": key, "label": label} for key, label in _PARTIES],
        "recommended_ids": inferred_keys,
        "required":        True,
        "category":        "scope",
    })
    for op_key, op_label in _OPERATIONS:
        questions.append(_yesno(
            gap_key=op_scope_gap_key(op_key),
            signal_key="feature_operations",
            text=(f"Does this feature touch **{op_label}**? "
                  "(Drives which negative test cases land on which sheet.)"),
        ))
    questions.append(_single_select(
        gap_key="risk_profile", signal_key="risk_profile",
        text="What is the risk profile of this feature? (Drives coverage floor + adaptive test-case cap.)",
        choices=_RISK_PROFILES, recommended="standard",
    ))
    questions.append(_single_select(
        gap_key="compliance_sensitivity", signal_key="compliance_sensitivity",
        text=f"What is the compliance sensitivity of this feature? (Raises the cap when {_ELEVATED_COMPLIANCE_LABELS}.)",
        choices=_COMPLIANCE_LEVELS, recommended="standard",
    ))
    return questions


def _expand_signal_gap(gap_key: str) -> list[dict]:
    """Return the programmatic questions for a known signal gap key.

    Returns [] for gap keys that are not signal-managed — those go to the LLM
    via the existing free-text path.
    """
    if gap_key == "certifying_parties":
        # v3 — legacy REST path emits the SAME multi_select shape the agentic
        # builder does. This code path has no access to the cached party
        # inference (called from a different route), so all four canonical
        # parties are pre-checked as the safe default. Loader recovery treats
        # both shapes identically via the singular `parties_in_scope` branch.
        return [
            {
                "id":              f"{SCOPE_SIGNAL_QK_PREFIX}parties_in_scope",
                "text":            (
                    "[Required] Confirm the parties in scope for cert of this "
                    "feature. Uncheck any that aren't involved — only in-scope "
                    "parties get test-case sheets."
                ),
                "signal_key":      "certifying_parties",
                "gap_key":         "parties_in_scope",
                "kind":            "multi_select",
                "options":         [{"id": k, "label": lbl} for k, lbl in _PARTIES],
                "recommended_ids": [k for k, _ in _PARTIES],
                "required":        True,
                "category":        "scope",
            }
        ]

    if gap_key == "feature_operations":
        # Fan out to 6 yes/no questions — one per operation.
        return [
            _mk_yesno_question(
                gap_key=op_scope_gap_key(op_key),
                signal_key="feature_operations",
                text=(
                    f"Does this feature touch **{op_label}**? "
                    f"(Drives which negative test cases land on which sheet, per {_AUTHORITY} scope ownership.)"
                    if idx == 0
                    else f"Does this feature touch **{op_label}**?"
                ),
            )
            for idx, (op_key, op_label) in enumerate(_OPERATIONS)
        ]

    if gap_key == "risk_profile":
        return [_mk_single_select_question(
            gap_key="risk_profile",
            signal_key="risk_profile",
            text="What is the risk profile of this feature? (Drives coverage floor + adaptive test-case cap.)",
            choices=_RISK_PROFILES,
        )]

    if gap_key == "compliance_sensitivity":
        return [_mk_single_select_question(
            gap_key="compliance_sensitivity",
            signal_key="compliance_sensitivity",
            text=f"What is the compliance sensitivity of this feature? (Raises adaptive cap when {_ELEVATED_COMPLIANCE_LABELS}.)",
            choices=_COMPLIANCE_LEVELS,
        )]

    if gap_key == "scope_error_codes":
        # v1 placeholder: emit ONE free-form question for now. The dedicated
        # multi-select widget (chip-per-error-code, bucketed by operation ×
        # failure category) is a separate frontend PR. When empty at engine
        # time, planner error-code enforcement is skipped (any category-
        # matching feature_specific code allowed) — safe default per plan §12.
        return [{
            "id":         str(uuid.uuid4()),
            "text":       (
                "For any feature-specific decline in scope, which error code(s) "
                "should apply per (operation × failure category)? Format: "
                '`{"debit_failure": ["ZM"], "credit_failure": ["MC2"], ...}` — '
                "leave blank if only generic codes (INVALID PIN, TIMEOUT, etc.) apply. "
                "(The dedicated widget is TBD; free-form JSON is accepted for v1.)"
            ),
            "gap_key":    "scope_error_codes",
            "signal_key": "scope_error_codes",
            "required":   False,   # placeholder — safe when blank
            "category":   "scope",
            "kind":       "free_text",
            "options":    [],
        }]

    return []


# The set of gap keys that trigger the programmatic dispatch above.
_SIGNAL_GAP_KEYS: frozenset[str] = frozenset({
    "certifying_parties",
    "feature_operations",
    "risk_profile",
    "compliance_sensitivity",
    "scope_error_codes",
})


# Domain vocabulary supplied by the active domain pack, not hardcoded — see
# docs/genericization sweep. `_pack`/`_AUTHORITY` are already resolved above.
_SYSTEM = f"""You are a Product Coach for {prompt_block("platform_name", "this change-management platform")} \
helping a Product Manager finalise a feature spec.

You will be given:
- A feature description
- The taxonomy bucket (e.g. a feature category key)
- A list of BLOCKING gap keys — things the PM must confirm before we can \
write the BRD / Tech Spec

Your job: turn each blocking gap key into ONE clear natural-language question \
for the PM. Use domain voice ("Should the limit be capped at X?" \
not "Specify limit_cap"). Prefer closed questions with suggested options when \
the domain has a conventional answer ("Is the auth method (a) Option A, (b) \
Option B, or (c) Both?"). Never ask about cosmetic or wording details.

Rules:
- Output between 1 and 7 questions. Fewer is better if some gaps can be \
combined.
- All questions must relate directly to the blocking keys provided.
- First question must be labelled "[Required]" in the text field. All others \
follow without the prefix.
- Prefer answers a PM can type in 1-2 sentences.

""" + ANTI_INJECTION_CLAUSE + """

Respond with ONLY this JSON (no markdown fences, no commentary):
{
  "questions": [
    {
      "gap_key": "renewal_limit",
      "text": "[Required] Should renewals be capped at a fixed count (e.g. 2 \
per loan) or a variable count capped at N per cycle? If variable, state the cap.",
      "category": "limits"
    }
  ]
}
"""


async def gen_questions(
    feature_description: str,
    blocking_gap_keys: list[str],
    taxonomy_primary: str | None = None,
    gap_descriptions: dict[str, str] | None = None,
) -> list[dict]:
    """Produce a list of PM questions for the given blocking gaps.

    Returns: [{id, text, gap_key, required, category, ...}]

    Signal-managed gaps (parties, operations, risk, compliance, scope error
    codes) are dispatched programmatically and always emitted, in addition to
    up to 7 LLM-generated questions for the free-form gaps.
    """
    if not blocking_gap_keys:
        return []

    # Partition into signal-managed vs free-form. Signal-managed gaps expand
    # to structured questions with `signal_key` + `options` (no LLM); free-
    # form gaps go through the LLM as before.
    signal_gaps: list[str] = [k for k in blocking_gap_keys if k in _SIGNAL_GAP_KEYS]
    freeform_gaps: list[str] = [k for k in blocking_gap_keys if k not in _SIGNAL_GAP_KEYS]

    signal_questions: list[dict] = []
    for k in signal_gaps:
        signal_questions.extend(_expand_signal_gap(k))

    llm_questions: list[dict] = []
    if freeform_gaps:
        # Provide context descriptions when we have them (from detector output)
        gap_lines = []
        for k in freeform_gaps:
            desc = (gap_descriptions or {}).get(k, "")
            gap_lines.append(f"- {k}: {desc}" if desc else f"- {k}")

        user_content = (
            f"# Feature description\n{wrap_untrusted(feature_description[:2500], 'FEATURE_DESCRIPTION')}\n\n"
            f"# Taxonomy primary\n{taxonomy_primary or 'unknown'}\n\n"
            f"# Blocking gap keys\n" + "\n".join(gap_lines)
        )

        raw = await call_llm(system=_SYSTEM, messages=[{"role": "user", "content": user_content}], max_tokens=1500, agent_name="question_generator")
        parsed = await parse_llm_json(raw, fallback={"questions": []})

        questions = parsed.get("questions") if isinstance(parsed, dict) else None
        if not isinstance(questions, list):
            questions = []

        for idx, q in enumerate(questions):
            if not isinstance(q, dict):
                continue
            text = str(q.get("text", "")).strip()
            if not text:
                continue
            llm_questions.append({
                "id":       str(uuid.uuid4()),
                "text":     text,
                "gap_key":  str(q.get("gap_key", freeform_gaps[min(idx, len(freeform_gaps) - 1)])),
                "required": True,   # every blocking question is required by definition
                "category": str(q.get("category", "")),
            })
            if len(llm_questions) >= 7:
                break

        # Safety net: LLM refused / returned nothing → synthesise a minimal
        # question per free-form gap so the PM has something to answer.
        if not llm_questions:
            logger.warning("question_generator returned 0 LLM questions; generating fallbacks")
            for k in freeform_gaps[:7]:
                desc = (gap_descriptions or {}).get(k, "")
                llm_questions.append({
                    "id":       str(uuid.uuid4()),
                    "text":     f"[Required] Please specify {k.replace('_', ' ')}."
                                + (f" Context: {desc}" if desc else ""),
                    "gap_key":  k,
                    "required": True,
                    "category": "",
                })

    normalised = signal_questions + llm_questions

    logger.info(
        "question_generator: produced %d question(s) — %d signal, %d LLM — for %d blocking gap(s)",
        len(normalised), len(signal_questions), len(llm_questions), len(blocking_gap_keys),
    )
    return normalised
