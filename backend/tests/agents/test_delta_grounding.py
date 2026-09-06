# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""delta_grounding — code-back reconciliation plan deltas against the real checkout:
grep evidence + one structured pass → per-delta impact / schema / impacted / risk /
question. Fail-open. The LLM is monkeypatched; the grep helper runs on a real tmp repo."""
import asyncio
import json
import subprocess

import app.agents.delta_grounding as DG
from app.agents.delta_grounding import ground_deltas, _grep_evidence, _salient_terms

D = [{"resolution": "brd_wins", "conflict": "issuer validates ReqNonce instead of PIN",
      "directive": "The uploaded BRD is authoritative here — issuer validates ReqNonce instead of PIN"}]


def test_ground_deltas_shape(monkeypatch):
    async def _llm(*a, **kw):
        return json.loads('{"deltas":[{"directive":"nonce","impact":"touches PIN validation",'
                '"schema_inventory_add":[{"path":"ReqTransfer.xsd","note":"add nonce"}],'
                '"data_model_changes_add":["store nonce"],"reuse":["ReqTransfer"],'
                '"impacted_paths":["src/Pay.java"],"risk":"low","risk_note":"touches auth",'
                '"overturns_ratified":false,"question":""}]}')
    monkeypatch.setattr(DG, "call_llm_structured", _llm)
    monkeypatch.setattr(DG, "_grep_evidence", lambda co, terms: ["[repo] ReqTransfer.xsd:1: <schema>"])
    out = asyncio.run(ground_deltas(None, change_id="cr", deltas=D, doc_kind="brd",
                                    plan_contract="PLAN", checkouts=["/x"]))
    assert out["status"] == "ok" and len(out["deltas"]) == 1 and out["grounded_at"]
    g = out["deltas"][0]
    assert g["risk"] == "low" and g["risk_note"] == "touches auth"
    assert g["schema_inventory_add"][0]["path"] == "ReqTransfer.xsd"
    assert g["impacted_paths"] == ["src/Pay.java"] and g["overturns_ratified"] is False


def test_bogus_risk_falls_to_none_and_clears_note(monkeypatch):
    async def _llm(*a, **kw):
        return json.loads('{"deltas":[{"directive":"x","impact":"y","risk":"bogus","risk_note":"leak"}]}')
    monkeypatch.setattr(DG, "call_llm_structured", _llm)
    monkeypatch.setattr(DG, "_grep_evidence", lambda co, terms: [])
    g = asyncio.run(ground_deltas(None, change_id="cr", deltas=D, doc_kind="brd",
                                  plan_contract="PLAN", checkouts=[]))["deltas"][0]
    assert g["risk"] == "none" and g["risk_note"] == ""   # invalid risk → none; note dropped


def test_skipped_and_fail_open(monkeypatch):
    assert asyncio.run(ground_deltas(None, change_id="cr", deltas=[], doc_kind="brd",
                                     plan_contract="PLAN", checkouts=[]))["status"] == "skipped"
    assert asyncio.run(ground_deltas(None, change_id="cr", deltas=D, doc_kind="brd",
                                     plan_contract="", checkouts=[]))["status"] == "skipped"
    async def _boom(*a, **kw): raise RuntimeError("down")
    monkeypatch.setattr(DG, "call_llm_structured", _boom)
    monkeypatch.setattr(DG, "_grep_evidence", lambda co, terms: [])
    assert asyncio.run(ground_deltas(None, change_id="cr", deltas=D, doc_kind="brd",
                                     plan_contract="PLAN", checkouts=[]))["status"] == "failed"


def test_salient_terms_covers_business_words(monkeypatch):
    # gap-2 fix: a business delta with no wire name still yields grep terms.
    terms = [t.lower() for t in _salient_terms([
        {"conflict": "add a 90-day inactivity auto-disable rule for consent",
         "directive": "issuer validates ReqTransfer nonce instead of PIN"}])]
    assert "reqtransfer" in terms                  # wire name (checkout-precise)
    assert "inactivity" in terms and "consent" in terms   # business words, no wire name
    assert "instead" not in terms and "the" not in terms  # stopwords dropped


def test_grep_evidence_real_repo(tmp_path):
    d = tmp_path / "repo"; d.mkdir()
    (d / "ReqTransfer.xsd").write_text("<xs:schema>a nonce field</xs:schema>", encoding="utf-8")
    for cmd in (["git", "init", "-q"], ["git", "add", "."],
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"]):
        subprocess.run(cmd, cwd=str(d), check=True, capture_output=True)
    ev = _grep_evidence([d], ["nonce"])
    assert ev and "ReqTransfer.xsd" in ev[0] and d.name in ev[0]
    assert _grep_evidence([d], ["totallyabsentterm"]) == []
    assert _grep_evidence([], ["nonce"]) == []
