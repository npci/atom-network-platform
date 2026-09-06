# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""v1 tests for clarification_loader — scope-signal extraction from the decision
ledger + backward compat.

PM scope signals are captured through the agentic clarification stage
(AnalysisPanel → decide-clarifications → decision ledger) and read back here.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import app.services.decision_ledger as decision_ledger
from app.services.clarification_loader import (
    _operation_gap_to_key,
    _party_gap_to_key,
    _recover_signal_value,
    get_scope_signals,
    SCOPE_SIGNAL_QK_PREFIX,
)
from app.core.domain.contract import cert_vocabulary_of, change_operations_of
from app.core.domain.registry import get_active_pack


# ── vocabulary tables ────────────────────────────────────────────────────────


def test_party_gap_map_is_complete():
    """Every pack-scoped party key must have a gap-key mapping (UPI: 4)."""
    expected = {k for k, _ in cert_vocabulary_of(get_active_pack()).parties()}
    assert expected, "active pack scopes no parties — test needs a party-scoping pack"
    assert set(_party_gap_to_key().values()) == expected


def test_operation_gap_map_is_complete():
    """Every pack change-operation must have a gap-key mapping (UPI: 6)."""
    expected = {o.key for o in change_operations_of(get_active_pack())}
    assert expected, "active pack declares no operations — test needs an operation pack"
    assert set(_operation_gap_to_key().values()) == expected


# ── _recover_signal_value — chosen label → option id ─────────────────────────

_YN = [{"id": "yes", "label": "Yes — in scope"}, {"id": "no", "label": "No — out of scope"}]


class TestRecoverSignalValue:
    def test_maps_chosen_label_to_option_id(self):
        e = MagicMock(chosen="Yes — in scope", options=_YN)
        assert _recover_signal_value(e) == "yes"

    def test_preserves_mixed_case_value(self):
        opts = [{"id": "RBI-mandated", "label": "RBI-mandated — driven by circular"}]
        e = MagicMock(chosen="RBI-mandated — driven by circular", options=opts)
        assert _recover_signal_value(e) == "RBI-mandated"   # not lowercased

    def test_empty_chosen_is_none(self):
        assert _recover_signal_value(MagicMock(chosen="", options=_YN)) is None

    def test_unmatched_label_falls_back_to_raw(self):
        e = MagicMock(chosen="custom text", options=_YN)
        assert _recover_signal_value(e) == "custom text"


# ── get_scope_signals — reads the decision ledger ────────────────────────────
#
# The v2 fixtures below use UPI's per-party/per-operation gap keys, which are
# pack data — pin the UPI pack for them rather than assume it.

import pytest as _pytest
from pathlib import Path as _Path

_UPI_PACK = str(_Path(__file__).resolve().parents[2] / "app" / "packs" / "network" / "network.yaml")


@_pytest.fixture(autouse=True)
def _pin_upi_pack(monkeypatch):
    monkeypatch.setenv("DOMAIN_PACK", _UPI_PACK)
    from app.core.domain import registry
    registry._load.cache_clear()
    yield
    registry._load.cache_clear()


def _entry(gap_key, chosen, options):
    qk = f"{SCOPE_SIGNAL_QK_PREFIX}{gap_key}"
    return MagicMock(question_key=qk, chosen=chosen, options=options)


_RISK = [{"id": "low", "label": "Low — x"},
         {"id": "standard", "label": "Standard — y"},
         {"id": "high", "label": "High — z"}]
_COMP = [{"id": "standard", "label": "Standard — a"},
         {"id": "RBI-mandated", "label": "RBI-mandated — b"}]


def _patch_ledger(monkeypatch, entries):
    monkeypatch.setattr(decision_ledger, "active_entries", lambda db, cid: entries)


class TestGetScopeSignals:
    def test_full_answers_extract_correctly(self, monkeypatch):
        _patch_ledger(monkeypatch, [
            _entry("party_payer_psp_in_scope", "Yes — in scope", _YN),
            _entry("party_payee_psp_in_scope", "Yes — in scope", _YN),
            _entry("party_remitter_bank_in_scope", "No — out of scope", _YN),
            _entry("party_beneficiary_bank_in_scope", "No — out of scope", _YN),
            _entry("op_init_in_scope", "Yes — in scope", _YN),
            _entry("op_auth_in_scope", "Yes — in scope", _YN),
            _entry("op_debit_in_scope", "Yes — in scope", _YN),
            _entry("op_credit_in_scope", "No — out of scope", _YN),
            _entry("risk_profile", "High — z", _RISK),
            _entry("compliance_sensitivity", "RBI-mandated — b", _COMP),
        ])
        signals = get_scope_signals("cr-1", MagicMock())
        assert signals["parties_in_scope"] == ["PAYER_PSP", "PAYEE_PSP"]
        assert signals["feature_operations"] == ["init", "auth", "debit"]
        assert signals["risk_profile"] == "high"
        assert signals["compliance_sensitivity"] == "RBI-mandated"
        assert signals["scope_error_codes"] == {}

    def test_no_signal_entries_returns_defaults(self, monkeypatch):
        """Backward compat: a change with no signal ledger entries → safe defaults."""
        _patch_ledger(monkeypatch, [])
        signals = get_scope_signals("legacy-cr", MagicMock())
        assert signals["parties_in_scope"] is None
        assert signals["feature_operations"] is None
        assert signals["risk_profile"] == "standard"
        assert signals["compliance_sensitivity"] == "standard"
        assert signals["scope_error_codes"] == {}

    def test_non_signal_ledger_entries_ignored(self, monkeypatch):
        """Ordinary clarification/decision entries must not leak into signals."""
        _patch_ledger(monkeypatch, [
            MagicMock(question_key="some-clarification-qid", chosen="Whatever", options=[]),
            _entry("party_payer_psp_in_scope", "Yes — in scope", _YN),
        ])
        signals = get_scope_signals("cr-2", MagicMock())
        assert signals["parties_in_scope"] == ["PAYER_PSP"]

    def test_parties_asked_but_all_no_is_empty_not_none(self, monkeypatch):
        """Distinguish 'asked, none in scope' ([]) from 'never asked' (None)."""
        _patch_ledger(monkeypatch, [
            _entry("party_payer_psp_in_scope", "No — out of scope", _YN),
        ])
        signals = get_scope_signals("cr-3", MagicMock())
        assert signals["parties_in_scope"] == []      # asked → empty list
        assert signals["feature_operations"] is None   # never asked → None

    def test_v3_multi_select_parties_answer_recovers_to_canonical_keys(self, monkeypatch):
        """v3 — the singular `scope_signal::parties_in_scope` question stores
        `chosen` as a JSON list of option labels. Loader parses it back."""
        parties_opts = [
            {"id": "PAYER_PSP",        "label": "Payer PSP"},
            {"id": "PAYEE_PSP",        "label": "Payee PSP"},
            {"id": "REMITTER_BANK",    "label": "Remitter Bank"},
            {"id": "BENEFICIARY_BANK", "label": "Beneficiary Bank"},
        ]
        _patch_ledger(monkeypatch, [
            _entry("parties_in_scope", '["Payer PSP", "Payee PSP"]', parties_opts),
        ])
        signals = get_scope_signals("cr-v3", MagicMock())
        assert set(signals["parties_in_scope"] or []) == {"PAYER_PSP", "PAYEE_PSP"}

    def test_v3_multi_select_takes_precedence_over_legacy_yesno(self, monkeypatch):
        """If BOTH the new singular answer AND legacy per-party answers exist,
        the new path wins (active_entries returns the latest supersession per
        question_key already; here we just assert the recovery preference)."""
        parties_opts = [
            {"id": "PAYER_PSP",        "label": "Payer PSP"},
            {"id": "PAYEE_PSP",        "label": "Payee PSP"},
            {"id": "REMITTER_BANK",    "label": "Remitter Bank"},
            {"id": "BENEFICIARY_BANK", "label": "Beneficiary Bank"},
        ]
        _patch_ledger(monkeypatch, [
            _entry("party_payer_psp_in_scope", "Yes — in scope", _YN),
            _entry("party_remitter_bank_in_scope", "Yes — in scope", _YN),
            _entry("parties_in_scope", '["Payee PSP"]', parties_opts),  # newer
        ])
        signals = get_scope_signals("cr-v3-mixed", MagicMock())
        assert signals["parties_in_scope"] == ["PAYEE_PSP"]

    def test_v3_corrupt_json_parties_answer_recovers_as_empty(self, monkeypatch):
        parties_opts = [{"id": "PAYER_PSP", "label": "Payer PSP"}]
        _patch_ledger(monkeypatch, [
            _entry("parties_in_scope", "not json at all", parties_opts),
        ])
        signals = get_scope_signals("cr-v3-corrupt", MagicMock())
        assert signals["parties_in_scope"] == []

    def test_ledger_read_failure_fails_soft(self, monkeypatch):
        def _boom(db, cid):
            raise RuntimeError("db down")
        monkeypatch.setattr(decision_ledger, "active_entries", _boom)
        signals = get_scope_signals("cr-4", MagicMock())
        assert signals["parties_in_scope"] is None
        assert signals["risk_profile"] == "standard"

    def test_scope_error_codes_parsed_when_present(self, monkeypatch):
        _patch_ledger(monkeypatch, [
            _entry("scope_error_codes", '{"debit_failure": ["ZM"]}',
                   [{"id": '{"debit_failure": ["ZM"]}', "label": '{"debit_failure": ["ZM"]}'}]),
        ])
        signals = get_scope_signals("cr-5", MagicMock())
        assert signals["scope_error_codes"] == {"debit_failure": ["ZM"]}
