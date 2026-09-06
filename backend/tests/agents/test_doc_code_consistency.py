# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""WS1 — Document↔CODE consistency gate. After codegen the CODE is the source of truth; the gate
flags TSD claims the diff does not support (doc over-claims → reconcile the TSD; code_missing → block).
Models the real fa4631e3 failures: an invented spend_category column, a fictional
network.validator.allowedProdTypes config key, and U16/U09 codes the code never emits."""
import asyncio

from app.agents import doc_code_consistency as DCC
from app.agents.doc_code_consistency import (
    check_doc_against_code, doc_fabrication_findings, code_gap_findings, repair_instruction,
)


def _patch_llm(monkeypatch, text):
    async def _f(**kw): return text
    monkeypatch.setattr(DCC, "call_llm", _f)


def test_system_prompt_covers_implementation_surfaces():
    sys = DCC._SYSTEM
    for token in ("persistence", "config", "error_code", "method", "code_missing", "source of truth"):
        assert token in sys


def test_gate_flags_doc_overclaims_as_warnings(monkeypatch):
    # The real fa4631e3 TSD over-claims: a DB column, a config key, and error codes the code lacks.
    _patch_llm(monkeypatch, """{
        "consistent": false,
        "findings": [
            {"severity": "warning", "kind": "persistence", "item": "spend_category column",
             "detail": "TSD adds a DB column; diff has no DDL/migration/field"},
            {"severity": "warning", "kind": "config", "item": "network.validator.allowedProdTypes",
             "detail": "TSD: config-only rollback; code is a hard-coded Set.of constant"},
            {"severity": "warning", "kind": "error_code", "item": "U16",
             "detail": "TSD names U16; code derives errorCd via substring(0,2)"}
        ]
    }""")
    r = asyncio.run(check_doc_against_code(tsd_content="...column...config...U16...",
                                           diff_text="diff --git a/X b/X\n+Set.of(\"the network\",\"UPILITE\")"))
    assert r["consistent"] is False and r["has_blocker"] is False          # doc over-claims never block
    assert {f["item"] for f in doc_fabrication_findings(r)} == {
        "spend_category column", "network.validator.allowedProdTypes", "U16"}
    assert code_gap_findings(r) == []


def test_gate_flags_code_missing_as_blocker(monkeypatch):
    _patch_llm(monkeypatch, '{"consistent": false, "findings": [{"severity": "blocker", '
               '"kind": "code_missing", "item": "spendCategory enum validation", '
               '"detail": "TSD+plan require rejecting invalid enum; diff has no such check"}]}')
    r = asyncio.run(check_doc_against_code(tsd_content="...reject invalid...", diff_text="diff --git a/Y b/Y\n+x"))
    assert r["has_blocker"] is True
    gaps = code_gap_findings(r)
    assert len(gaps) == 1 and gaps[0]["item"] == "spendCategory enum validation"


def test_non_code_missing_severity_forced_to_warning(monkeypatch):
    # Even if the LLM marks a doc over-claim 'blocker', only a real code gap may block.
    _patch_llm(monkeypatch, '{"consistent": false, "findings": [{"severity": "blocker", '
               '"kind": "persistence", "item": "spend_category column", "detail": "d"}]}')
    r = asyncio.run(check_doc_against_code(tsd_content="x", diff_text="diff --git a/Z b/Z\n+y"))
    assert r["findings"][0]["severity"] == "warning" and r["has_blocker"] is False


def test_gate_passes_when_tsd_matches_code(monkeypatch):
    _patch_llm(monkeypatch, '{"consistent": true, "findings": []}')
    r = asyncio.run(check_doc_against_code(tsd_content="matches", diff_text="diff --git a/A b/A\n+z"))
    assert r["consistent"] is True and r["has_blocker"] is False and r["findings"] == []


def test_gate_empty_when_no_diff_or_tsd():
    assert asyncio.run(check_doc_against_code(tsd_content="", diff_text="d"))["consistent"] is True
    assert asyncio.run(check_doc_against_code(tsd_content="t", diff_text=""))["consistent"] is True


def test_gate_fails_open_on_llm_error(monkeypatch):
    async def _boom(**kw): raise RuntimeError("llm down")
    monkeypatch.setattr(DCC, "call_llm", _boom)
    r = asyncio.run(check_doc_against_code(tsd_content="t", diff_text="diff --git a/A b/A\n+z"))
    assert r["consistent"] is True and r["has_blocker"] is False           # never block on checker failure


def test_repair_instruction_lists_the_overclaims():
    instr = repair_instruction([{"item": "spend_category column", "detail": "no DDL in diff"}])
    assert "DOC↔CODE RECONCILE" in instr and "spend_category column" in instr


def test_reconcile_fails_open_to_original(monkeypatch):
    async def _boom(**kw): raise RuntimeError("llm down")
    monkeypatch.setattr(DCC, "call_llm", _boom)
    out = asyncio.run(DCC.reconcile_doc_to_code(
        tsd_content="ORIGINAL TSD", diff_text="diff --git a/A b/A\n+z", instruction="fix it"))
    assert out == "ORIGINAL TSD"
