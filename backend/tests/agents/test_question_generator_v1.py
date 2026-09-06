# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""v1 tests for question_generator — signal-key dispatch + programmatic questions.

These tests assert UPI vocabulary (four parties, six operations), and the
module under test resolves that vocabulary from the ACTIVE pack at import
time. So this file pins the UPI pack for its own duration: the module is
reloaded under UPI before the tests and reloaded under the ambient pack
afterwards, leaving every other test file exactly the module it expects.
Without this the file asserted whatever DOMAIN_PACK the shell happened to
export — the F-1 shape, red on every NLLN-configured host.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

import app.agents.question_generator as _qg_module
from app.core.domain import registry as _registry

_UPI_PACK = str(Path(_qg_module.__file__).resolve().parents[1]
                / "packs" / "network" / "network.yaml")
_REBIND = ("_COMPLIANCE_LEVELS", "_OPERATIONS", "_PARTIES", "_RISK_PROFILES",
           "_SIGNAL_GAP_KEYS", "_expand_signal_gap")


@pytest.fixture(scope="module", autouse=True)
def _pin_upi_pack_for_this_module():
    prior = os.environ.get("DOMAIN_PACK")
    os.environ["DOMAIN_PACK"] = _UPI_PACK
    _registry._load.cache_clear()
    importlib.reload(_qg_module)
    this = sys.modules[__name__]
    for name in _REBIND:
        setattr(this, name, getattr(_qg_module, name))
    yield
    if prior is None:
        os.environ.pop("DOMAIN_PACK", None)
    else:
        os.environ["DOMAIN_PACK"] = prior
    _registry._load.cache_clear()
    importlib.reload(_qg_module)


from app.agents.question_generator import (
    _COMPLIANCE_LEVELS,
    _OPERATIONS,
    _PARTIES,
    _RISK_PROFILES,
    _SIGNAL_GAP_KEYS,
    _expand_signal_gap,
)


# ── Signal dispatch: expand each of the 5 recognized signal keys ─────────────


class TestExpandSignalGap:
    def test_certifying_parties_emits_single_multi_select(self):
        # v3 — legacy REST path now emits ONE multi_select question with all
        # four canonical parties pre-checked (no inference at this call site).
        # Replaces the four independent yes/no fan-out.
        questions = _expand_signal_gap("certifying_parties")
        assert len(questions) == 1
        q = questions[0]
        assert q["signal_key"] == "certifying_parties"
        assert q["kind"] == "multi_select"
        assert q["gap_key"] == "parties_in_scope"
        assert q["required"] is True
        # Options carry canonical party keys as ids.
        assert {o["id"] for o in q["options"]} == {
            "PAYER_PSP", "PAYEE_PSP", "REMITTER_BANK", "BENEFICIARY_BANK",
        }
        # Fail-open default when no inference is fed in: all four pre-checked.
        assert set(q["recommended_ids"]) == {
            "PAYER_PSP", "PAYEE_PSP", "REMITTER_BANK", "BENEFICIARY_BANK",
        }

    def test_feature_operations_fans_out_to_six_yesno(self):
        questions = _expand_signal_gap("feature_operations")
        assert len(questions) == 6
        for q in questions:
            assert q["signal_key"] == "feature_operations"
            assert q["kind"] == "yes_no"

    def test_feature_operations_covers_all_6_canonical_operations(self):
        questions = _expand_signal_gap("feature_operations")
        gap_keys = {q["gap_key"] for q in questions}
        assert gap_keys == {
            "op_init_in_scope",
            "op_auth_in_scope",
            "op_debit_in_scope",
            "op_credit_in_scope",
            "op_debit_reversal_in_scope",
            "op_credit_reversal_in_scope",
        }

    def test_risk_profile_is_single_select_with_3_options(self):
        questions = _expand_signal_gap("risk_profile")
        assert len(questions) == 1
        q = questions[0]
        assert q["kind"] == "single_select"
        assert q["signal_key"] == "risk_profile"
        values = [o["value"] for o in q["options"]]
        assert values == ["low", "standard", "high"]

    def test_compliance_sensitivity_is_single_select_with_3_options(self):
        questions = _expand_signal_gap("compliance_sensitivity")
        assert len(questions) == 1
        q = questions[0]
        assert q["kind"] == "single_select"
        values = [o["value"] for o in q["options"]]
        assert values == ["standard", "RBI-mandated", "PMLA-touched"]

    def test_scope_error_codes_is_freetext_placeholder(self):
        """v1 placeholder — dedicated multi-select widget is a follow-up PR."""
        questions = _expand_signal_gap("scope_error_codes")
        assert len(questions) == 1
        q = questions[0]
        assert q["kind"] == "free_text"
        assert q["signal_key"] == "scope_error_codes"
        # Required=False so a blank answer is safe (planner treats empty as
        # "any category-matching feature_specific code allowed").
        assert q["required"] is False

    def test_unknown_gap_key_returns_empty(self):
        """Unknown gap keys should pass through to the LLM path."""
        assert _expand_signal_gap("random_gap_key") == []
        assert _expand_signal_gap("") == []

    def test_certifying_parties_question_marked_required(self):
        """The single multi_select parties question carries the [Required]
        prefix so the AnalysisPanel treats it as blocking."""
        questions = _expand_signal_gap("certifying_parties")
        assert questions[0]["text"].startswith("[Required]")


# ── Vocabulary constants ─────────────────────────────────────────────────────


def test_signal_gap_keys_set_is_stable():
    """The 5 signal gap keys the programmatic dispatch recognises.

    These are no longer listed in taxonomy.py's required_fields (see
    test_ambiguity_detector_v1) and `capture_scope_signals` defaults False, so
    nothing produces them in normal operation. The dispatch is kept for a domain
    that wires up a real consumer; this pins its key set so a re-enable does not
    silently ask a different question set.
    """
    assert _SIGNAL_GAP_KEYS == frozenset({
        "certifying_parties",
        "feature_operations",
        "risk_profile",
        "compliance_sensitivity",
        "scope_error_codes",
    })


def test_parties_vocab_matches_scope_ownership():
    party_keys = {k for k, _ in _PARTIES}
    assert party_keys == {"PAYER_PSP", "PAYEE_PSP", "REMITTER_BANK", "BENEFICIARY_BANK"}


def test_operations_vocab_matches_scope_ownership():
    op_keys = {k for k, _ in _OPERATIONS}
    assert op_keys == {"init", "auth", "debit", "credit", "debit_reversal", "credit_reversal"}


def test_risk_and_compliance_are_3_options_each():
    assert len(_RISK_PROFILES) == 3
    assert len(_COMPLIANCE_LEVELS) == 3


# ── build_scope_signal_questions: agentic (AnalysisPanel) shape ───────────────


class TestBuildScopeSignalQuestions:
    def _qs(self):
        from app.agents.question_generator import build_scope_signal_questions
        return build_scope_signal_questions()

    def test_count_and_stable_ids(self):
        qs = self._qs()
        # v3 — 1 parties multi_select + 6 operations + risk + compliance = 9.
        # (Was 4 parties yes/no fan-out + 6 + risk + compliance = 12.)
        assert len(qs) == 9
        assert all(q["id"].startswith("scope_signal::") for q in qs)
        # ids are stable + unique so the ledger question_key is recognisable
        assert len(qs) == len({q["id"] for q in qs})
        ids = {q["id"] for q in qs}
        assert "scope_signal::parties_in_scope" in ids
        assert "scope_signal::op_debit_reversal_in_scope" in ids
        assert "scope_signal::risk_profile" in ids

    def test_parties_question_is_multi_select_with_inference_pre_check(self):
        from app.agents.question_generator import build_scope_signal_questions
        qs = build_scope_signal_questions(party_inference={
            "parties_in_scope": ["PAYER_PSP", "PAYEE_PSP"],
            "rationale": "BRD names Payer PSP and Payee PSP only.",
            "confidence": "high",
            "source": "llm",
        })
        parties = next(q for q in qs if q["id"] == "scope_signal::parties_in_scope")
        assert parties["kind"] == "multi_select"
        assert set(parties["recommended_ids"]) == {"PAYER_PSP", "PAYEE_PSP"}
        assert "Rationale:" in parties["text"]

    def test_parties_question_defaults_all_four_when_no_inference(self):
        qs = self._qs()  # no inference passed
        parties = next(q for q in qs if q["id"] == "scope_signal::parties_in_scope")
        assert set(parties["recommended_ids"]) == {
            "PAYER_PSP", "PAYEE_PSP", "REMITTER_BANK", "BENEFICIARY_BANK",
        }

    def test_every_option_has_id_for_analysis_panel(self):
        # M3: AnalysisPanel binds chosen_option_id === option.id, so every option
        # MUST carry an id (the legacy {value,label} shape broke selection).
        for q in self._qs():
            assert q["options"], q["id"]
            for o in q["options"]:
                assert o.get("id"), (q["id"], o)
                assert o.get("label")

    def test_yesno_option_ids_are_canonical_values(self):
        # v3 — parties are multi_select now, but operations remain yes_no.
        qs = self._qs()
        op = next(q for q in qs if q["id"].endswith("op_init_in_scope"))
        assert {o["id"] for o in op["options"]} == {"yes", "no"}

    def test_single_select_option_ids_are_canonical_values(self):
        qs = self._qs()
        risk = next(q for q in qs if q["id"].endswith("risk_profile"))
        assert {o["id"] for o in risk["options"]} == {"low", "standard", "high"}
        compliance = next(q for q in qs if q["id"].endswith("compliance_sensitivity"))
        assert "RBI-mandated" in {o["id"] for o in compliance["options"]}

    def test_error_codes_question_excluded(self):
        # Free-text scope_error_codes is NOT asked here — the agentic completeness
        # gate requires every asked question to have an answer; empty is the safe default.
        assert not any(q["id"].endswith("scope_error_codes") for q in self._qs())
