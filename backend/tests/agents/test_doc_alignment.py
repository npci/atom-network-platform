# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""doc_alignment — the BRD→plan axis: extract the doc's implementable commitments,
align them against the plan contract, surface material extensions + implementation-story
divergences (mechanism/sequence/actor/scope/value). Plus the pure conflict mappers in
upload_reconciler that turn alignment findings into resolvable conflicts."""
import asyncio
import json

import app.agents.doc_alignment as DA
from app.agents.doc_alignment import extract_doc_commitments, align_commitments
from app.agents.upload_reconciler import _alignment_conflict, _is_dup_conflict, _options


# ── extract_doc_commitments ──────────────────────────────────────────────────
def test_extract_commitments_shape_and_dedup(monkeypatch):
    async def _llm(*a, **kw):
        return json.loads('{"commitments":[{"id":"x","text":"After 3 failed biometric attempts, CL falls back to PIN","category":"rule"},'
                '{"id":"y","text":"After 3 failed biometric attempts, CL falls back to PIN","category":"rule"},'
                '{"id":"z","text":"Issuer bank validates nonce signature using stored public key","category":"flow_step"}]}')
    monkeypatch.setattr(DA, "call_llm_structured", _llm)
    out = asyncio.run(extract_doc_commitments("some doc text", "brd"))
    assert [c["id"] for c in out] == ["c1", "c2"]          # renumbered + duplicate text dropped
    assert out[0]["category"] == "rule" and "3 failed" in out[0]["text"]


def test_extract_commitments_fails_open(monkeypatch):
    async def _boom(*a, **kw): raise RuntimeError("down")
    monkeypatch.setattr(DA, "call_llm_structured", _boom)
    assert asyncio.run(extract_doc_commitments("doc", "brd")) == []


# ── align_commitments ────────────────────────────────────────────────────────
def test_align_filters_sorts_and_joins(monkeypatch):
    async def _llm(*a, **kw):
        return json.loads('{"findings":['
                '{"commitment_id":"c2","relation":"brd_only","item":"90-day disablement","detail":"plan does not cover this","severity":"warning"},'
                '{"commitment_id":"c1","relation":"mechanism_conflict","item":"nonce vs PIN","detail":"doc says nonce replaces PIN; plan keeps PIN","severity":"blocker"},'
                '{"commitment_id":"c3","relation":"not_a_relation","item":"junk","detail":"x","severity":"warning"},'
                '{"commitment_id":"c4","relation":"actor_conflict","item":"consent storage","detail":"","severity":"blocker"}]}')
    monkeypatch.setattr(DA, "call_llm_structured", _llm)
    cms = [{"id": "c1", "text": "Issuer validates nonce instead of PIN", "category": "flow_step"},
           {"id": "c2", "text": "Disable biometrics after 90 days inactivity", "category": "rule"}]
    out = asyncio.run(align_commitments("PLAN CONTRACT TEXT", cms, "brd"))
    # invalid relation + empty detail dropped; conflict sorted before brd_only
    assert [f["relation"] for f in out] == ["mechanism_conflict", "brd_only"]
    assert out[0]["severity"] == "blocker"
    assert out[0]["commitment_text"].startswith("Issuer validates")   # joined from the commitment set
    assert out[1]["commitment_text"].startswith("Disable biometrics")


def test_align_empty_inputs_and_fail_open(monkeypatch):
    assert asyncio.run(align_commitments("", [{"id": "c1", "text": "x"}])) == []
    assert asyncio.run(align_commitments("plan", [])) == []
    async def _boom(*a, **kw): raise RuntimeError("down")
    monkeypatch.setattr(DA, "call_llm_structured", _boom)
    assert asyncio.run(align_commitments("plan", [{"id": "c1", "text": "x"}])) == []


# ── conflict mappers (upload_reconciler, pure) ───────────────────────────────
def test_alignment_conflict_brd_only_maps_to_extends_plan():
    c = _alignment_conflict({"relation": "brd_only", "commitment_text": "90-day inactivity disablement",
                             "item": "90-day disablement", "detail": "plan does not cover this",
                             "severity": "warning"})
    assert c["jurisdiction"] == "extends_plan" and c["kind"] == "brd_only"
    assert "adds this beyond the plan" in c["text"]
    labels = [o["label"] for o in c["options"]]
    assert any("add this to the plan" in l for l in labels)          # extends-specific wording
    assert any("remove it from the" in l for l in labels)


def test_alignment_conflict_divergence_maps_to_contradicts_plan():
    c = _alignment_conflict({"relation": "actor_conflict", "commitment_text": "PSP stores consent",
                             "item": "consent storage", "detail": "doc says PSP stores consent; plan says issuer stores it",
                             "severity": "blocker"})
    assert c["jurisdiction"] == "contradicts_plan" and c["kind"] == "actor_conflict"
    assert c["severity"] == "blocker"
    assert "doc says PSP" in c["text"]                               # detail (both sides) IS the text
    assert c["evidence"]["item"] == "consent storage"


def test_is_dup_conflict_containment_both_ways():
    existing = [{"evidence": {"item": "ReqSplitEnum"}}]
    assert _is_dup_conflict("reqsplitenum", existing)                # case-insensitive exact
    assert _is_dup_conflict("ReqSplitEnum wire message", existing)   # containment either way
    assert not _is_dup_conflict("ReqOther", existing)
    assert not _is_dup_conflict("", existing)


def test_options_extends_plan_wording():
    labels = [o["label"] for o in _options("extends_plan")]
    assert labels[0].startswith("Keep it")
    assert "free_text" in _options("extends_plan")[2]
