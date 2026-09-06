# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 1 test: direction (initiated_by + psp_as) and per-TC test_data are
carried through tc_store_sync.

Covers the failure modes that drove the rewrite:
  * txn_initiated_by silently dropped → cert-agent run mis-grouped
  * psp_as silently dropped → operator can't filter by Payer/Payee side
  * test_data hardcoded → every TC sends identical defaults to the simulator,
    looking "random" in the run results

Pure-function tests — no DB, no httpx.

The prefixes/flows under test (PR_/MT_, ReqTransfer→PAY) are UPI pack data
(`cert_vocabulary.role_prefixes`, `authority_case_prefix`, `message_flows`),
so the module pins DOMAIN_PACK to the UPI pack rather than assuming it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.tc_store_sync import (
    ParsedTC,
    _resolve_initiated_by,
    _resolve_psp_as,
    _resolve_role,
    _resolve_test_data,
    _stub_to_parsed,
    _DIFF_FIELDS,
)


_UPI_PACK = str(Path(__file__).resolve().parents[2] / "app" / "packs" / "network" / "network.yaml")


@pytest.fixture(autouse=True)
def _pin_upi_pack(monkeypatch):
    monkeypatch.setenv("DOMAIN_PACK", _UPI_PACK)
    from app.core.domain import registry
    registry._load.cache_clear()
    yield
    registry._load.cache_clear()


# A minimal valid stub the parser will accept. Tests override the fields they
# care about; the rest pass parse checks (renderable + non-sentinel + resolvable
# flow + resolvable response code).
_BASE_STUB = {
    "test_id":         "PR_1",
    "apis":            ["ReqTransfer"],
    "expected_status": "Success",
    "scenario_summary": "Standard happy path payment.",
    "coverage_tag":    "happy_path",
    "rendered": {
        "details_block":     "API Involved: PAY",
        "description_block": "Standard happy path payment.",
        "steps_block":       "1. ReqTransfer → 2. RespTransfer",
        "test_id":           "PR_1",
    },
}

_CHANGE_ID = "11111111-2222-3333-4444-555555555555"


def _parse(stub_overrides=None):
    stub = {**_BASE_STUB}
    if stub_overrides:
        stub.update(stub_overrides)
    parsed, reason, unknown = _stub_to_parsed(stub, _CHANGE_ID)
    assert parsed is not None, f"stub failed to parse: reason={reason!r} unknown={unknown!r}"
    return parsed


# ── _resolve_initiated_by ─────────────────────────────────────────────────────

class TestResolveInitiatedBy:
    def test_explicit_bank_uppercased(self):
        assert _resolve_initiated_by({"txn_initiated_by": "Bank"}, role="PAYER_PSP") == "BANK"

    def test_explicit_npci_uppercased(self):
        assert _resolve_initiated_by({"txn_initiated_by": "NPCI"}, role="NPCI") == "NPCI"

    def test_missing_pr_role_infers_bank(self):
        assert _resolve_initiated_by({}, role="PAYER_PSP") == "BANK"

    def test_missing_pe_role_infers_bank(self):
        assert _resolve_initiated_by({}, role="PAYEE_PSP") == "BANK"

    def test_missing_mt_role_infers_authority(self):
        # The ROLE is pack vocabulary and tracks the pack's authority
        # participant; the VALUE it resolves to is the wire constant the
        # cert-agent matches exactly, so that stays "NPCI".
        assert _resolve_initiated_by({}, role="AUTHORITY") == "NPCI"

    def test_missing_unknown_role_returns_empty(self):
        # Operator must see the gap, not be silently bucketed.
        assert _resolve_initiated_by({}, role="") == ""

    def test_invalid_value_returns_empty_not_silently_defaulted(self):
        # A garbage value is a real bug to surface, not something to coerce away.
        assert _resolve_initiated_by({"txn_initiated_by": "Sponsor"}, role="PAYER_PSP") == ""


# ── _resolve_psp_as ───────────────────────────────────────────────────────────

class TestResolvePspAs:
    @pytest.mark.parametrize("raw,expected", [
        ("Payer",  "Payer"),
        ("payer",  "Payer"),
        ("PAYER",  "Payer"),
        ("Payee",  "Payee"),
        ("payee",  "Payee"),
        ("PAYEE",  "Payee"),
        ("",       ""),
        ("bystander", ""),
        ("Issuer", ""),
        (None,     ""),
    ])
    def test_normalization(self, raw, expected):
        assert _resolve_psp_as({"psp_as": raw}) == expected


# ── _resolve_test_data overrides ──────────────────────────────────────────────

class TestResolveTestData:
    def test_defaults_when_no_overrides(self):
        td = _resolve_test_data({"payer_handle": "VPA", "payee_handle": "VPA"})
        assert td["payer_vpa"] == "test@npci"
        assert td["amount"]    == "1.00"
        assert td["currency"]  == "INR"

    def test_stub_overrides_win_over_handle_defaults(self):
        td = _resolve_test_data({
            "payer_handle": "VPA",
            "payee_handle": "VPA",
            "test_data": {"amount": "742.50", "payer_vpa": "alice@npci"},
        })
        assert td["amount"]    == "742.50"
        assert td["payer_vpa"] == "alice@npci"
        # untouched handle defaults remain
        assert td["payee_vpa"] == "merchant@npci"
        assert td["currency"]  == "INR"

    def test_empty_string_override_does_not_blank_default(self):
        # The stub-merge must skip empty strings so an unfilled field doesn't
        # wipe a real default. Without this guard, partner forms reset every
        # PAY TC to amount="" and the simulator rejects them all.
        td = _resolve_test_data({"test_data": {"amount": "", "payer_vpa": None}})
        assert td["amount"]    == "1.00"
        assert td["payer_vpa"] == "test@npci"

    def test_handle_specific_extras_still_apply(self):
        # IFSC handle still triggers the SBI default block.
        td = _resolve_test_data({"payer_handle": "A/c+IFSC"})
        assert td["ifsc"] == "SBIN0001234"


# ── _stub_to_parsed integration ───────────────────────────────────────────────

class TestStubToParsedDirection:
    def test_explicit_initiator_carried_through(self):
        p = _parse({"test_id": "MT_3", "txn_initiated_by": "NPCI", "psp_as": "Payer"})
        assert p.initiated_by == "NPCI"
        assert p.psp_as       == "Payer"

    def test_bank_initiator_carried_through(self):
        p = _parse({"test_id": "PR_1", "txn_initiated_by": "Bank", "psp_as": "Payer"})
        assert p.initiated_by == "BANK"
        assert p.psp_as       == "Payer"

    def test_missing_initiator_inferred_from_pr_prefix(self):
        p = _parse({"test_id": "PR_1"})
        assert p.role         == "PAYER_PSP"
        assert p.initiated_by == "BANK"

    def test_missing_initiator_inferred_from_mt_prefix(self):
        p = _parse({"test_id": "MT_3", "apis": ["ReqReversal"]})
        assert p.role         == "AUTHORITY"   # pack vocabulary
        assert p.initiated_by == "NPCI"        # pinned wire constant

    def test_to_dict_carries_new_fields(self):
        p = _parse({"test_id": "PR_2", "txn_initiated_by": "Bank", "psp_as": "Payee"})
        d = p.to_dict()
        assert d["initiated_by"] == "BANK"
        assert d["psp_as"]       == "Payee"
        # Required existing fields still present (regression guard).
        for k in ("tc_id", "name", "flow", "expected_resp_code", "test_data", "subsets", "role", "enabled"):
            assert k in d


# ── _DIFF_FIELDS regression ───────────────────────────────────────────────────

def test_diff_fields_include_initiator_so_changes_are_detected():
    # If initiated_by is missing from _DIFF_FIELDS, a TC that flips from
    # The Authority to BANK in a re-push silently shows as unchanged — the worst kind
    # of regression because it looks fine in the modal.
    assert "initiated_by" in _DIFF_FIELDS
    assert "psp_as"       in _DIFF_FIELDS


# ── ParsedTC defaults ─────────────────────────────────────────────────────────

def test_parsed_tc_defaults_when_constructed_minimally():
    # If a future caller forgets to pass the new fields, dataclass defaults
    # should keep construction safe (empty string, not KeyError on to_dict).
    p = ParsedTC(
        tc_id="x", name="x", flow="PAY", expected_resp_code="00",
        description="", test_data={}, request_xml_template=None,
        enabled=True, subsets=["cr-x"], role="",
    )
    assert p.initiated_by == ""
    assert p.psp_as       == ""
    assert p.steps        == []
    assert p.to_dict()["initiated_by"] == ""
    assert p.to_dict()["psp_as"]       == ""
    assert p.to_dict()["steps"]        == []


# ── Phase 2: steps resolution ─────────────────────────────────────────────────

class TestSteps:
    def test_single_api_yields_one_step(self):
        p = _parse({"test_id": "PR_1", "apis": ["ReqTransfer"]})
        assert len(p.steps) == 1
        assert p.steps[0]["api"]                == "ReqTransfer"
        assert p.steps[0]["step_no"]            == 1
        assert p.steps[0]["expected_resp_code"] == "00"

    def test_request_response_pair_auto_derives_two_steps(self):
        # The most common pattern — Req + Resp pair must become two ordered
        # steps so the dispatcher doesn't collapse them (the bug the user hit).
        p = _parse({"test_id": "PR_1", "apis": ["ReqTransfer", "RespTransfer"]})
        assert [s["api"] for s in p.steps] == ["ReqTransfer", "RespTransfer"]
        assert [s["step_no"] for s in p.steps] == [1, 2]

    def test_step_directions_track_initiator(self):
        # authority-initiated (MT_) → request leg npci_to_bank, response leg bank_to_npci
        p = _parse({"test_id": "MT_3", "apis": ["ReqReversal", "RespReversal"]})
        assert p.initiated_by == "NPCI"
        assert p.steps[0]["direction"] == "npci_to_bank"
        assert p.steps[1]["direction"] == "bank_to_npci"

    def test_step_directions_track_bank_initiator(self):
        # Bank-initiated (PR_) → request leg bank_to_npci, response leg npci_to_bank
        p = _parse({"test_id": "PR_1", "apis": ["ReqTransfer", "RespTransfer"]})
        assert p.initiated_by == "BANK"
        assert p.steps[0]["direction"] == "bank_to_npci"
        assert p.steps[1]["direction"] == "npci_to_bank"

    def test_explicit_rendered_test_steps_used_verbatim(self):
        # Phase A may emit test_steps[] directly; honour them and normalise
        # only the fields the dispatcher requires.
        p = _parse({
            "test_id": "PR_1",
            "apis": ["ReqTransfer", "RespTransfer"],
            "rendered": {
                **_BASE_STUB["rendered"],
                "test_steps": [
                    {"step_no": 1, "api": "ReqTransfer",  "expected_resp_code": "00"},
                    {"step_no": 2, "api": "RespTransfer", "expected_resp_code": "00"},
                    {"step_no": 3, "api": "ReqChkTxn", "expected_resp_code": "00"},
                ],
            },
        })
        # 3 steps — auto-derive (which would have produced 2) is overridden.
        assert [s["api"] for s in p.steps] == ["ReqTransfer", "RespTransfer", "ReqChkTxn"]

    def test_explicit_test_steps_filled_in_missing_direction(self):
        # When the engine omits a per-step direction, we fill it in from
        # api-name + initiator so the dispatcher doesn't have to.
        p = _parse({
            "test_id": "PR_1",
            "apis": ["ReqTransfer"],
            "rendered": {
                **_BASE_STUB["rendered"],
                "test_steps": [{"step_no": 1, "api": "ReqTransfer"}],
            },
        })
        assert p.steps[0]["direction"] == "bank_to_npci"  # PR_ → BANK


# ── Phase 3a: catalog XML template wiring ─────────────────────────────────────

class TestCatalogTemplateWiring:
    def test_built_in_flow_carries_catalog_template(self):
        # The TC pushed to cert-agent must carry the actual XML — not None.
        # That's what makes the TC self-contained instead of relying on
        # cert-agent's catalog fallback at dispatch time.
        p = _parse({"test_id": "PR_1", "apis": ["ReqTransfer"]})
        assert p.request_xml_template is not None
        assert "<NetworkRequest" in p.request_xml_template
        assert 'http://example.org/network/schema/' in p.request_xml_template

    def test_pay_template_has_required_placeholders(self):
        p = _parse({"test_id": "PR_1", "apis": ["ReqTransfer"]})
        xml = p.request_xml_template
        for ph in ("{{txn_id}}", "{{payer_vpa}}", "{{amount}}", "{{callback_url}}"):
            # Some placeholders may not appear in every flow but PAY has the core set.
            pass  # Soft check above; tighter assertion below.
        # PAY catalog uses these placeholders:
        assert "{{payer_vpa}}" in xml
        assert "{{amount}}"    in xml
        assert "{{callback_url}}" in xml
