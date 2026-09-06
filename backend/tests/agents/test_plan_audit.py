# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""WS2 — Plan enforcement audit. Conservatively challenges the PLAN's hard enforcement claims before
ratification. Models the real fa4631e3 gap: 'reject invalid enum' delegated entirely to JAXB with no
Java check and no shown schema-validation wiring. Conservative: a sound plan is left byte-identical."""
import asyncio
from types import SimpleNamespace

from app.agents import plan_audit as PA
from app.agents.plan_audit import audit_plan_enforcement, annotate_plan


def _patch_llm(monkeypatch, text):
    async def _f(**kw): return text
    monkeypatch.setattr(PA, "call_llm", _f)


_FP = {"overview": "validate spendCategory and reject invalid values",
       "assumptions": ["Out-of-enum values are rejected at the JAXB/schema layer; no extra Java check is added"]}
_TA = {"risks": ["existing risk"], "constraints": ["HeadType.prodType is xs:string"]}


def test_system_prompt_targets_enforcement_soundness():
    sys = PA._SYSTEM
    for token in ("ENFORCEMENT SOUNDNESS", "JAXB", "ASSUMED", "CONSERVATIVE", "EMPTY list"):
        assert token in sys


def test_audit_flags_unverified_enforcement(monkeypatch):
    _patch_llm(monkeypatch, '{"sound": false, "findings": [{"requirement": "reject invalid spendCategory enum", '
               '"enforcement_point": "JAXB unmarshalling", "verified": false, "severity": "blocker", '
               '"detail": "Enum rejection is assumed at JAXB; no schema is shown wired and the plan adds no Java check"}]}')
    r = asyncio.run(audit_plan_enforcement(functional_plan=_FP, technical_analysis=_TA))
    assert r["sound"] is False and len(r["findings"]) == 1
    assert r["findings"][0]["enforcement_point"] == "JAXB unmarshalling"
    assert r["findings"][0]["verified"] is False


def test_audit_sound_plan_returns_empty(monkeypatch):
    _patch_llm(monkeypatch, '{"sound": true, "findings": []}')
    r = asyncio.run(audit_plan_enforcement(functional_plan=_FP, technical_analysis=_TA))
    assert r["sound"] is True and r["findings"] == []


def test_audit_empty_plan_is_sound_without_llm(monkeypatch):
    # No enforcement-relevant content → sound, and the LLM must not even be called.
    called = {"n": 0}
    async def _f(**kw): called["n"] += 1; return "{}"
    monkeypatch.setattr(PA, "call_llm", _f)
    r = asyncio.run(audit_plan_enforcement(functional_plan={}, technical_analysis={}))
    assert r["sound"] is True and called["n"] == 0


def test_audit_fails_open_on_llm_error(monkeypatch):
    async def _boom(**kw): raise RuntimeError("llm down")
    monkeypatch.setattr(PA, "call_llm", _boom)
    r = asyncio.run(audit_plan_enforcement(functional_plan=_FP, technical_analysis=_TA))
    assert r["sound"] is True and r["findings"] == []     # never block a plan on the auditor


def test_annotate_plan_is_additive_and_non_destructive():
    ca = SimpleNamespace(technical_analysis={"risks": ["existing risk"], "reuse_findings": ["x"]},
                         functional_plan={"overview": "keep me"}, flow_spec={"steps": ["keep me too"]})
    findings = [{"requirement": "reject invalid enum", "severity": "blocker",
                 "detail": "assumed at JAXB, not shown wired", "enforcement_point": "JAXB", "verified": False}]
    assert annotate_plan(ca, findings) is True
    assert ca.technical_analysis["enforcement_audit"] == findings          # recorded
    assert "existing risk" in ca.technical_analysis["risks"]               # original risk preserved
    assert any("ENFORCEMENT AUDIT" in r for r in ca.technical_analysis["risks"])  # audit risk appended
    assert ca.technical_analysis["reuse_findings"] == ["x"]                # other fields untouched
    assert ca.functional_plan == {"overview": "keep me"}                   # NEVER rewrites functional_plan
    assert ca.flow_spec == {"steps": ["keep me too"]}                      # NEVER rewrites flow_spec


def test_annotate_plan_noop_on_empty_findings():
    ca = SimpleNamespace(technical_analysis={"risks": ["r"]})
    assert annotate_plan(ca, []) is False
    assert "enforcement_audit" not in ca.technical_analysis                # a sound plan is left byte-identical
