# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""v1 tests for brd_requirements — heuristic FR tagging + feature criteria coercion.

The tagging regexes and party vocabulary under test are UPI pack data
(`operation_patterns`/`party_patterns`/`financial_operations`), so the module
pins DOMAIN_PACK to the UPI pack rather than assuming it. (The criteria-
coercion tests additionally depend on brd_extractor's import-time vocabulary,
which only matches when the whole process runs under the UPI pack.)
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.agents.brd_extractor import _coerce_feature_criteria
from app.services.brd_requirements import (
    _heuristic_tag_fr,
    get_functional_requirements,
    reconcile_criteria_to_frs,
)


_UPI_PACK = str(Path(__file__).resolve().parents[2] / "app" / "packs" / "network" / "network.yaml")


@pytest.fixture(autouse=True)
def _pin_upi_pack(monkeypatch):
    import importlib

    import app.agents.brd_extractor as brd_extractor
    from app.core.domain import registry

    monkeypatch.setenv("DOMAIN_PACK", _UPI_PACK)
    registry._load.cache_clear()
    # brd_extractor snapshots its vocabulary at import time; re-execute it
    # under the pinned pack (same module object, so the imported functions
    # see the reloaded globals), and restore afterwards.
    importlib.reload(brd_extractor)
    yield
    registry._load.cache_clear()
    importlib.reload(brd_extractor)


# ── Heuristic FR tagging ─────────────────────────────────────────────────────


class TestHeuristicTagFr:
    def test_payee_specific_fr_tagged_correctly(self):
        r = _heuristic_tag_fr(
            "PreAuthLimit tag in RespTransfer",
            "Payee PSP shall include preAuthLimit tag in RespTransfer with the pre-authorised amount.",
        )
        assert "PAYEE_PSP" in r["parties"]
        assert r["flow_type"] == "meta"   # no money-movement op word

    def test_debit_reversal_flow_type_financial(self):
        r = _heuristic_tag_fr(
            "Debit reversal on ReqTransfer timeout",
            "Debit reversal shall trigger on ReqTransfer timeout. Remitter Bank must unwind.",
        )
        assert "debit_reversal" in r["operations"]
        assert "REMITTER_BANK" in r["parties"]
        assert r["flow_type"] == "financial"

    def test_credit_op_beneficiary_party(self):
        r = _heuristic_tag_fr(
            "Credit leg with new merchant tag",
            "Beneficiary Bank shall verify the credit leg carries the new merchant category code.",
        )
        assert "credit" in r["operations"]
        assert "BENEFICIARY_BANK" in r["parties"]
        assert r["flow_type"] == "financial"

    def test_reversal_does_not_double_tag_base_op(self):
        # L5: "debit reversal" must tag only debit_reversal, not plain debit
        # (which would widen the sheet's floor/allowed-ops beyond the FR).
        for txt in ("debit reversal", "debit-reversal", "debit rollback"):
            ops = _heuristic_tag_fr(txt, None)["operations"]
            assert "debit_reversal" in ops and "debit" not in ops, (txt, ops)
        for txt in ("credit reversal", "credit-reversal", "credit rollback"):
            ops = _heuristic_tag_fr(txt, None)["operations"]
            assert "credit_reversal" in ops and "credit" not in ops, (txt, ops)
        # plain debit/credit still tag (no regression)
        assert "debit" in _heuristic_tag_fr("remitter debit leg", None)["operations"]
        assert "credit" in _heuristic_tag_fr("beneficiary credit leg", None)["operations"]

    def test_descriptive_fr_no_tags(self):
        """FRs about docs/copy/UI have no operations or parties."""
        r = _heuristic_tag_fr(
            "Documentation update",
            "The FAQ shall be updated with new terminology.",
        )
        assert r["operations"] == []
        assert r["parties"] == []
        assert r["flow_type"] == "meta"


# ── get_functional_requirements — DB-backed helper ───────────────────────────


class _Attr:
    def __eq__(self, other): return True
    def asc(self): return self
    def desc(self): return self


class TestGetFunctionalRequirements:
    @staticmethod
    def _mock_db_with_rows(rows):
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = rows
        return db

    def test_synthesises_fr_ids_from_row_order(self):
        rows = []
        for i, (label, desc) in enumerate([
            ("PreAuthLimit tag", "Payee PSP shall include preAuthLimit tag."),
            ("Debit reversal", "Debit reversal on ReqTransfer timeout by Remitter Bank."),
            ("Doc update", "Update FAQ terminology."),
        ], start=1):
            r = MagicMock()
            r.id = f"uuid-{i}"
            r.label = label
            r.description = desc
            r.category = "api_contract"
            r.is_mandatory = True
            r.change_request_id = "cr"
            rows.append(r)
        frs = get_functional_requirements("cr", self._mock_db_with_rows(rows))
        assert [fr["fr_id"] for fr in frs] == ["FR-01", "FR-02", "FR-03"]

    def test_augments_with_heuristic_tags(self):
        r = MagicMock()
        r.id = "uuid-x"
        r.label = "Debit reversal on ReqTransfer timeout"
        r.description = "Remitter Bank must unwind."
        r.category = "api_contract"
        r.is_mandatory = True
        r.change_request_id = "cr"
        frs = get_functional_requirements("cr", self._mock_db_with_rows([r]))
        assert len(frs) == 1
        fr = frs[0]
        assert "debit_reversal" in fr["operations"]
        assert "REMITTER_BANK" in fr["parties"]
        assert fr["flow_type"] == "financial"

    def test_empty_when_no_rows(self):
        """Legacy change with no BRDRequirement rows returns empty list."""
        db = self._mock_db_with_rows([])
        assert get_functional_requirements("legacy-cr", db) == []


# ── Feature criteria coercion ────────────────────────────────────────────────


class TestCoerceFeatureCriteria:
    def test_valid_criterion_passes_through(self):
        result = _coerce_feature_criteria([{
            "fr_id": "FR-05",
            "fr_label": "Payee sends preAuthLimit",
            "tag_name": "preAuthLimit",
            "expected_value_shape": "integer paise",
            "responsible_party": "PAYEE_PSP",
            "operation": "debit",
            "success_criterion": "Payee sends preAuthLimit=5000",
            "failure_scenario": "Payee omits preAuthLimit",
        }])
        assert len(result) == 1
        assert result[0]["responsible_party"] == "PAYEE_PSP"
        assert result[0]["error_code_placeholder"] == "PM_CONFIRM_FEATURE_DECLINE"

    def test_issuer_bank_aliases_to_remitter(self):
        result = _coerce_feature_criteria([{
            "fr_id": "FR-06",
            "tag_name": "issuerFlag",
            "responsible_party": "Issuer Bank",   # alias
            "operation": "debit",
        }])
        assert result[0]["responsible_party"] == "REMITTER_BANK"

    def test_authorization_aliases_to_auth(self):
        result = _coerce_feature_criteria([{
            "fr_id": "FR-07",
            "tag_name": "authTag",
            "responsible_party": "PAYER_PSP",
            "operation": "authorization",   # US spelling
        }])
        assert result[0]["operation"] == "auth"

    def test_unknown_party_or_operation_dropped(self):
        result = _coerce_feature_criteria([
            {"fr_id": "FR-8", "tag_name": "x", "responsible_party": "MARTIAN", "operation": "debit"},
            {"fr_id": "FR-9", "tag_name": "y", "responsible_party": "PAYER_PSP", "operation": "juggling"},
        ])
        assert result == []

    def test_dedup_on_fr_id(self):
        result = _coerce_feature_criteria([
            {"fr_id": "FR-1", "tag_name": "a", "responsible_party": "PAYER_PSP", "operation": "init"},
            {"fr_id": "FR-1", "tag_name": "b", "responsible_party": "PAYER_PSP", "operation": "init"},
        ])
        assert len(result) == 1
        assert result[0]["tag_name"] == "a"

    def test_missing_tag_name_dropped(self):
        result = _coerce_feature_criteria([
            {"fr_id": "FR-1", "responsible_party": "PAYER_PSP", "operation": "init"},
        ])
        assert result == []

    def test_error_code_placeholder_locked_to_marker(self):
        """LLM cannot pre-fill the error code — PM must resolve during clarification."""
        result = _coerce_feature_criteria([{
            "fr_id": "FR-1",
            "tag_name": "t",
            "responsible_party": "PAYEE_PSP",
            "operation": "credit",
            "error_code_placeholder": "ZM",   # LLM tried to fill
        }])
        assert result[0]["error_code_placeholder"] == "PM_CONFIRM_FEATURE_DECLINE"

    def test_max_items_cap_enforced(self):
        """Cap at 10 items — protects downstream token budget."""
        items = [
            {"fr_id": f"FR-{i}", "tag_name": f"t{i}", "responsible_party": "PAYER_PSP", "operation": "init"}
            for i in range(20)
        ]
        assert len(_coerce_feature_criteria(items)) == 10


# ── reconcile_criteria_to_frs (B2: fr_id namespace join) ──────────────────────


class TestReconcileCriteriaToFrs:
    FRS = [
        {"fr_id": "FR-01", "operations": ["debit"], "parties": ["PAYEE_PSP"]},
        {"fr_id": "FR-02", "operations": ["credit"], "parties": ["BENEFICIARY_BANK"]},
    ]

    def test_remaps_disjoint_llm_id_by_op_and_party(self):
        # Criterion carries the LLM's Section-6 id (FR-05) — disjoint from the
        # functional-requirement synthetic space. Must remap to FR-01 so the
        # Writer's criterion_map.get(fr_ref) resolves.
        crit = [{"fr_id": "FR-05", "tag_name": "preAuthLimit",
                 "operation": "debit", "responsible_party": "PAYEE_PSP"}]
        out = reconcile_criteria_to_frs(crit, self.FRS)
        assert out[0]["fr_id"] == "FR-01"
        assert out[0]["tag_name"] == "preAuthLimit"   # other fields preserved
        assert crit[0]["fr_id"] == "FR-05"            # input not mutated

    def test_already_aligned_id_kept(self):
        out = reconcile_criteria_to_frs(
            [{"fr_id": "FR-02", "operation": "credit", "responsible_party": "BENEFICIARY_BANK"}],
            self.FRS,
        )
        assert out[0]["fr_id"] == "FR-02"

    def test_op_only_fallback_when_party_unmatched(self):
        out = reconcile_criteria_to_frs(
            [{"fr_id": "X", "operation": "credit", "responsible_party": "NPCI"}], self.FRS,
        )
        assert out[0]["fr_id"] == "FR-02"

    def test_no_match_leaves_fr_id_unchanged(self):
        out = reconcile_criteria_to_frs(
            [{"fr_id": "X", "operation": "auth", "responsible_party": "NPCI"}], self.FRS,
        )
        assert out[0]["fr_id"] == "X"

    def test_empty_inputs_passthrough(self):
        assert reconcile_criteria_to_frs([], self.FRS) == []
        crit = [{"fr_id": "FR-05", "operation": "debit", "responsible_party": "PAYEE_PSP"}]
        assert reconcile_criteria_to_frs(crit, []) == crit
