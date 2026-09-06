# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""BRD/TSD plan-fidelity: the ratified plan is injected as a binding contract (Part A) and a
consistency gate flags docs that invent wire APIs/schemas the plan never defined (Part B)."""
import asyncio
import json
from types import SimpleNamespace

from app.agents import doc_consistency as DC
from app.agents.doc_consistency import check_doc_against_plan
from app.agents.plan_contract import build_plan_contract


# ── Part A: the plan contract block ────────────────────────────────────────────────
class _Q:
    def __init__(self, r): self._r = r
    def filter(self, *a, **k): return self
    def order_by(self, *a, **k): return self
    def first(self): return self._r


class _DB:
    def __init__(self, ca): self._ca = ca
    def query(self, *a, **k): return _Q(self._ca)


def _ca(**kw):
    base = dict(version=1,
                technical_analysis={"data_model_changes": "NEW internal REST endpoint; NO change to ReqTransfer.xsd / network-common.xsd",
                                    "approach_decision": {"approach": "new", "chosen_title": "Internal config API",
                                                          "target_api": "UserSpendLimitController", "why": "no wire change needed"}},
                functional_plan={"overview": "daily cap enforced at debit gate"},
                flow_spec={"messages": ["internal REST GET/POST /upi/spendlimit"]})
    base.update(kw)
    return SimpleNamespace(**base)


def test_contract_emits_rule_and_plan_surface():
    block = build_plan_contract(_DB(_ca()), "cr")
    assert "SOLUTION DESIGN CONTRACT" in block
    # The hard rule's domain word comes from the active pack (plan_contract
    # renders `{{DOMAIN_NAME}} wire message types`), so assert the invariant
    # phrasing rather than one domain's name.
    assert "wire message types" in block and "Do NOT introduce new" in block
    assert "Internal config API" in block and "UserSpendLimitController" in block
    assert "NO change to ReqTransfer.xsd" in block                     # the wire decision is loud


def test_contract_empty_when_no_plan():
    assert build_plan_contract(_DB(None), "cr") == ""
    assert build_plan_contract(None, "cr") == ""


# ── Part B: the consistency gate ───────────────────────────────────────────────────
def _patch_llm(monkeypatch, text):
    # The gate calls `call_llm_structured`, which returns the ALREADY-PARSED tool-call
    # payload — not the raw JSON string a text completion would emit. Cases stay authored
    # as JSON for readability, so parse here. (Patching `call_llm` instead silently lets
    # the real client run and fail-open to "consistent", which makes the negative cases
    # look like passes.)
    async def _f(*a, **kw): return json.loads(text)
    monkeypatch.setattr(DC, "call_llm_structured", _f)


def test_gate_flags_invented_wire_message_as_blocker(monkeypatch):
    _patch_llm(monkeypatch, """{
        "consistent": false,
        "findings": [
            {"severity": "blocker", "kind": "wire_message", "item": "ReqSetSpendLimit",
             "detail": "BRD defines a new wire message pair; plan uses an internal REST endpoint, no wire change"},
            {"severity": "blocker", "kind": "schema", "item": "ReqTransfer.txnAmount",
             "detail": "BRD adds a ReqTransfer field; plan says no schema change"}
        ]
    }""")
    r = asyncio.run(check_doc_against_plan(doc_kind="BRD", doc_content="...wire messages...",
                                          plan_contract="internal REST only; no wire change"))
    assert r["has_blocker"] is True and r["consistent"] is False
    assert {f["item"] for f in r["findings"]} == {"ReqSetSpendLimit", "ReqTransfer.txnAmount"}


def test_gate_passes_a_consistent_doc(monkeypatch):
    _patch_llm(monkeypatch, '{"consistent": true, "findings": []}')
    r = asyncio.run(check_doc_against_plan(doc_kind="TSD", doc_content="internal REST endpoint",
                                          plan_contract="internal REST only"))
    assert r["consistent"] is True and r["has_blocker"] is False and r["findings"] == []


def test_gate_no_plan_is_consistent_by_default():
    # No ratified plan yet → nothing to reconcile against; never blocks generation.
    r = asyncio.run(check_doc_against_plan(doc_kind="BRD", doc_content="anything", plan_contract=""))
    assert r["consistent"] is True and r["has_blocker"] is False


def test_gate_fails_open_on_llm_error(monkeypatch):
    async def _boom(**kw): raise RuntimeError("llm down")
    monkeypatch.setattr(DC, "call_llm", _boom)
    r = asyncio.run(check_doc_against_plan(doc_kind="BRD", doc_content="x", plan_contract="y"))
    assert r["consistent"] is True and r["has_blocker"] is False    # never block on checker failure


def test_gate_downgrades_unknown_severity(monkeypatch):
    _patch_llm(monkeypatch, '{"consistent": false, "findings": [{"severity": "catastrophic", "kind": "endpoint", "item": "X", "detail": "d"}]}')
    r = asyncio.run(check_doc_against_plan(doc_kind="BRD", doc_content="x", plan_contract="y"))
    assert r["findings"][0]["severity"] == "warning" and r["has_blocker"] is False


# ── Auto-correction: retry-until-clean, no hard block ───────────────────────────────
_BLOCK = {"consistent": False, "has_blocker": True,
          "findings": [{"severity": "blocker", "kind": "wire_message", "item": "ReqSetSpendLimit", "detail": "d"}]}
_CLEAN = {"consistent": True, "has_blocker": False, "findings": []}


def _queue_checks(monkeypatch, results):
    """Make check_doc_against_plan return each queued result in turn (last one repeats)."""
    seq = list(results)
    async def _f(**kw):
        return seq.pop(0) if len(seq) > 1 else seq[0]
    monkeypatch.setattr(DC, "check_doc_against_plan", _f)


def test_enforce_repairs_until_clean(monkeypatch):
    _queue_checks(monkeypatch, [_BLOCK, _CLEAN])    # divergent, then clean after one repair
    calls = []
    async def repair(instruction, attempt, content, items):
        calls.append(attempt)
        return "CORRECTED DOC"
    r = asyncio.run(DC.enforce_plan_consistency(
        doc_kind="BRD", doc_content="DIVERGENT DOC", plan_contract="p", repair_fn=repair))
    assert r["content"] == "CORRECTED DOC" and r["repaired"] is True and r["attempts"] == 1
    assert r["consistency"]["has_blocker"] is False
    assert r["consistency"]["auto_repaired"] is True and r["consistency"]["auto_repair_attempts"] == 1
    assert calls == [1]


def test_enforce_exhausts_attempt_budget_then_ships_no_hard_block(monkeypatch):
    _queue_checks(monkeypatch, [_BLOCK])            # never gets clean
    async def repair(instruction, attempt, content, items):
        return f"attempt-{attempt}"                 # changes each pass so the loop keeps going
    r = asyncio.run(DC.enforce_plan_consistency(
        doc_kind="TSD", doc_content="DIVERGENT", plan_contract="p", repair_fn=repair))
    assert r["attempts"] == DC.MAX_REPAIR_ATTEMPTS    # capped at the retry budget
    assert r["content"] == f"attempt-{DC.MAX_REPAIR_ATTEMPTS}"   # last attempt is shipped...
    assert r["consistency"]["has_blocker"] is True   # ...even though still divergent (no hard block)
    assert r["consistency"]["auto_repair_attempts"] == DC.MAX_REPAIR_ATTEMPTS


def test_enforce_skips_repair_when_already_clean(monkeypatch):
    _queue_checks(monkeypatch, [_CLEAN])
    called = False
    async def repair(instruction, attempt, content, items):
        nonlocal called; called = True
        return "x"
    r = asyncio.run(DC.enforce_plan_consistency(
        doc_kind="BRD", doc_content="GOOD DOC", plan_contract="p", repair_fn=repair))
    assert r["attempts"] == 0 and r["repaired"] is False and called is False
    assert r["content"] == "GOOD DOC"


def test_enforce_stops_when_repair_makes_no_change(monkeypatch):
    _queue_checks(monkeypatch, [_BLOCK])
    async def repair(instruction, attempt, content, items):
        return content                               # no-op repair → don't spin the full budget
    r = asyncio.run(DC.enforce_plan_consistency(
        doc_kind="BRD", doc_content="SAME", plan_contract="p", repair_fn=repair))
    assert r["attempts"] == 1 and r["repaired"] is False and r["content"] == "SAME"


def test_enforce_stops_when_repair_raises(monkeypatch):
    _queue_checks(monkeypatch, [_BLOCK])
    async def repair(instruction, attempt, content, items):
        raise RuntimeError("editor down")
    r = asyncio.run(DC.enforce_plan_consistency(
        doc_kind="BRD", doc_content="DOC", plan_contract="p", repair_fn=repair))
    assert r["repaired"] is False and r["content"] == "DOC"   # fail-open, keep the doc


def test_gate_flags_plan_decision_inversion_as_blocker(monkeypatch):
    # The real TSD failure: plan says fail-OPEN, doc says fail-CLOSED → a blocking plan_decision gap.
    _patch_llm(monkeypatch, '{"consistent": false, "findings": [{"severity": "blocker", '
               '"kind": "plan_decision", "item": "fail-open vs fail-closed", '
               '"detail": "plan ratified fail-open; TSD specifies fail-closed (U28)"}]}')
    r = asyncio.run(check_doc_against_plan(doc_kind="TSD", doc_content="...fail-closed...",
                                          plan_contract="fail-open"))
    assert r["has_blocker"] is True
    assert r["findings"][0]["kind"] == "plan_decision"


def test_gate_flags_internal_contradiction_as_blocker(monkeypatch):
    # The real TSD failure: two error-code tables disagree for the same event.
    _patch_llm(monkeypatch, '{"consistent": false, "findings": [{"severity": "blocker", '
               '"kind": "contradiction", "item": "hold-timeout decline code", '
               '"detail": "section 1 says U30, section 2 says U67 for the same event"}]}')
    r = asyncio.run(check_doc_against_plan(doc_kind="TSD", doc_content="...U30...U67...",
                                          plan_contract="plan"))
    assert r["has_blocker"] is True and r["findings"][0]["kind"] == "contradiction"


# ── WS3c/3d: persistence/config surfaces + behavioural contradictions ───────────────
def test_system_prompt_covers_persistence_config_and_behavioural_contradiction():
    # Guard the prompt hardening so a future edit can't silently drop these surfaces.
    sys = DC._SYSTEM
    assert "kind 'persistence'" in sys and "column" in sys           # invented DB column/table
    assert "kind 'config'" in sys and "redeploy" in sys              # invented config / feature-flag
    assert "BEHAVIOURAL contradiction" in sys                        # not just clashing text
    assert "persistence|config" in sys                              # kinds present in the JSON schema


def test_gate_flags_invented_persistence_column_as_warning(monkeypatch):
    # The real fa4631e3 TSD failure: claims a spend_category column the plan/code never add.
    _patch_llm(monkeypatch, '{"consistent": false, "findings": [{"severity": "warning", '
               '"kind": "persistence", "item": "spend_category column", '
               '"detail": "TSD adds a DB column; plan persists only via the serialized-XML log"}]}')
    r = asyncio.run(check_doc_against_plan(doc_kind="TSD", doc_content="...add spend_category column...",
                                          plan_contract="no new column; XML log only"))
    assert r["consistent"] is False and r["has_blocker"] is False    # warning, not a hard block
    assert r["findings"][0]["kind"] == "persistence"


def test_gate_flags_invented_config_mechanism_as_warning(monkeypatch):
    _patch_llm(monkeypatch, '{"consistent": false, "findings": [{"severity": "warning", '
               '"kind": "config", "item": "network.validator.allowedProdTypes", '
               '"detail": "TSD describes a config-only rollback; code ships a hard-coded Set.of constant"}]}')
    r = asyncio.run(check_doc_against_plan(doc_kind="TSD", doc_content="...config-only rollback...",
                                          plan_contract="hard-coded constant; redeploy to change"))
    assert r["findings"][0]["kind"] == "config" and r["has_blocker"] is False


def test_gate_flags_behavioural_contradiction_as_blocker(monkeypatch):
    # The real fa4631e3 TSD failure: Config section says redeploy-required, Rollback says config-only.
    _patch_llm(monkeypatch, '{"consistent": false, "findings": [{"severity": "blocker", '
               '"kind": "contradiction", "item": "allowedProdTypes hot-reload vs redeploy", '
               '"detail": "Configuration section says compile-time/redeploy; Rollback section says config-only, no redeploy"}]}')
    r = asyncio.run(check_doc_against_plan(doc_kind="TSD", doc_content="...redeploy...config-only...",
                                          plan_contract="plan"))
    assert r["has_blocker"] is True and r["findings"][0]["kind"] == "contradiction"


def test_reconcile_fails_open_to_original(monkeypatch):
    async def _boom(**kw): raise RuntimeError("llm down")
    monkeypatch.setattr(DC, "call_llm", _boom)
    out = asyncio.run(DC.reconcile_doc_to_plan(
        doc_kind="BRD", doc_content="ORIGINAL", plan_contract="p", instruction="fix it"))
    assert out == "ORIGINAL"


def test_divergent_items_extracts_only_blocker_item_names():
    # The docgen repair locates divergent sections by these names — only BLOCKER items,
    # trimmed, with empty names dropped.
    consistency = {"findings": [
        {"severity": "blocker", "kind": "wire_message", "item": "ReqSetSpendLimit", "detail": "d"},
        {"severity": "warning", "kind": "endpoint", "item": "GET /x", "detail": "d"},      # not a blocker
        {"severity": "blocker", "kind": "schema", "item": "  ReqTransfer.txnAmount  ", "detail": "d"},
        {"severity": "blocker", "kind": "contradiction", "item": "", "detail": "d"},        # empty → dropped
    ]}
    assert DC.divergent_items(consistency) == ["ReqSetSpendLimit", "ReqTransfer.txnAmount"]
    assert DC.divergent_items({}) == []
