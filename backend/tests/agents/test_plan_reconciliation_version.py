# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""record_reconciliation_version — fold uploaded-BRD reconciliation resolutions
into a new plan version: brd_wins/custom change the plan, plan_wins does not."""
from types import SimpleNamespace

import app.agents.upload_reconciler as UR
from app.agents.plan_versioning import record_reconciliation_version


class _Q:
    def __init__(self, r): self._r = r
    def filter(self, *a, **k): return self
    def order_by(self, *a, **k): return self
    def first(self): return self._r
    def all(self): return []          # ChangeImpactedPath dedup query (S1.2) — none pre-existing


class _DB:
    def __init__(self, prior):
        self._prior = prior
        self.added = []
        self.committed = False
    def query(self, *a, **k): return _Q(self._prior)
    def add(self, o): self.added.append(o)
    def commit(self): self.committed = True
    def rollback(self): pass


def _prior(**kw):
    base = dict(version=3, status="ratified", run_id="run-1",
                technical_analysis={"data_model_changes": "x"}, functional_plan={"overview": "o"},
                flow_spec={"messages": []}, analysis_sha={"r": "sha"},
                validated_against_brd_id="brd-old", validated_against_brd_version=1,
                validated_against_brd_hash="h", pm_ratified_by="pm", pm_ratified_at="t",
                tech_ratified_by="tl", tech_ratified_at="t")
    base.update(kw)
    return SimpleNamespace(**base)


def _recon(resolutions, doc_id="brd-2", doc_version=2):
    return SimpleNamespace(
        conflicts=[{"id": "c1", "text": "Rs 5,000 cap"}, {"id": "c2", "text": "new API /x"}],
        resolutions=resolutions, doc_id=doc_id, doc_version=doc_version)


def test_brd_wins_creates_new_plan_version():
    prior = _prior(); db = _DB(prior)
    recon = _recon({"c1": {"chosen_option_id": "brd_wins"}, "c2": {"chosen_option_id": "plan_wins"}})
    v = record_reconciliation_version(db, change_request_id="cr", reconciliation=recon, decided_by="u1")
    assert v == 4
    assert prior.status == "superseded" and db.committed and len(db.added) == 1
    new = db.added[0]
    assert new.version == 4 and new.status == "ratified"              # ratification carried forward
    assert new.validated_against_brd_id == "brd-2" and new.validated_against_brd_version == 2
    rev = new.technical_analysis["plan_revisions"][-1]
    assert rev["kind"] == "upload_reconciliation" and len(rev["deltas"]) == 1   # c2 (plan_wins) excluded
    assert new.technical_analysis["upload_reconciliation_addenda"]


def test_custom_resolution_folds_directive():
    db = _DB(_prior())
    recon = _recon({"c1": {"custom_answer": "cap is Rs 5,000 but only for P2P"}})
    assert record_reconciliation_version(db, change_request_id="cr", reconciliation=recon) == 4
    deltas = db.added[0].technical_analysis["plan_revisions"][-1]["deltas"]
    assert deltas[0]["resolution"] == "custom" and "P2P" in deltas[0]["directive"]


def test_all_plan_wins_no_new_version():
    prior = _prior(); db = _DB(prior)
    recon = _recon({"c1": {"chosen_option_id": "plan_wins"}, "c2": {"chosen_option_id": "plan_wins"}})
    assert record_reconciliation_version(db, change_request_id="cr", reconciliation=recon) is None
    assert db.added == [] and prior.status == "ratified"             # plan untouched


def test_no_prior_plan_returns_none():
    db = _DB(None)
    recon = _recon({"c1": {"chosen_option_id": "brd_wins"}})
    assert record_reconciliation_version(db, change_request_id="cr", reconciliation=recon) is None


# ── S1.2: a brd-wins delta whose wire entity EXISTS registers a ChangeImpactedPath,
#         and the revision records the exact commit it was grounded against ──────────
def test_fold_registers_impacted_paths_and_grounded_sha(monkeypatch):
    monkeypatch.setattr(UR, "_analysis_checkouts", lambda db, cid, allow_clone=False: ["/fake"])
    monkeypatch.setattr(UR, "_checkout_heads", lambda co: {"repo-x": "a" * 40})
    monkeypatch.setattr(UR, "_wire_entity_in_code", lambda co, name: True)          # entity exists
    monkeypatch.setattr(UR, "_wire_entity_paths", lambda co, name: [("repo-x", "ReqTransfer.xsd")])
    from app.models.change_analysis import ChangeImpactedPath
    prior = _prior(); db = _DB(prior)
    recon = SimpleNamespace(
        conflicts=[{"id": "c1", "text": "BRD introduces ReqTransfer wire message"}],
        resolutions={"c1": {"chosen_option_id": "brd_wins"}}, doc_id="brd-2", doc_version=2)
    v = record_reconciliation_version(db, change_request_id="cr", reconciliation=recon)
    assert v == 4
    cips = [o for o in db.added if isinstance(o, ChangeImpactedPath)]
    assert cips and cips[0].path == "ReqTransfer.xsd" and cips[0].repo_id == "repo-x" and cips[0].kind == "xsd"
    ca = next(o for o in db.added if not isinstance(o, ChangeImpactedPath))
    rev = ca.technical_analysis["plan_revisions"][-1]
    assert rev["grounded_sha"] == {"repo-x": "a" * 40} and rev["grounded_at"]
    assert rev["code_validation"][0]["in_code"] is True


def test_fold_merges_delta_grounding(monkeypatch):
    # S2.4: precomputed grounding on the recon merges STRUCTURALLY — schema additions
    # extend the inventory (tagged origin) + the grounding rides the revision entry.
    monkeypatch.setattr(UR, "_analysis_checkouts", lambda db, cid, allow_clone=False: [])
    monkeypatch.setattr(UR, "_checkout_heads", lambda co: {})
    monkeypatch.setattr(UR, "_wire_entity_in_code", lambda co, name: None)
    monkeypatch.setattr(UR, "_wire_entity_paths", lambda co, name: [])
    prior = _prior(technical_analysis={"schema_inventory": [{"path": "existing.xsd"}],
                                       "data_model_changes": "x"})
    db = _DB(prior)
    recon = SimpleNamespace(
        conflicts=[{"id": "c1", "text": "add ReqNonce message"}],
        resolutions={"c1": {"chosen_option_id": "brd_wins"}}, doc_id="brd-2", doc_version=2,
        grounding={"status": "ok", "deltas": [{"directive": "add ReqNonce", "impact": "i",
                   "schema_inventory_add": [{"path": "ReqNonce.xsd", "note": "new"}],
                   "data_model_changes_add": ["store nonce per account"],
                   "reuse": ["XmlUtil.registerJaxb"]}]})
    assert record_reconciliation_version(db, change_request_id="cr", reconciliation=recon) == 4
    from app.models.change_analysis import ChangeImpactedPath
    ca = next(o for o in db.added if not isinstance(o, ChangeImpactedPath))
    ta = ca.technical_analysis
    paths = [i["path"] for i in ta["schema_inventory"]]
    assert "existing.xsd" in paths and "ReqNonce.xsd" in paths           # merged, not replaced
    added = next(i for i in ta["schema_inventory"] if i["path"] == "ReqNonce.xsd")
    assert added["origin"] == "reconciliation v4"                        # tagged so it's never confused with original
    assert ta["plan_revisions"][-1]["grounding"][0]["directive"] == "add ReqNonce"
    # gap-1 fix: data-model + reuse insights reach the authoritative addenda (→ downstream)
    addenda = ta["upload_reconciliation_addenda"]
    assert any("store nonce per account" in a for a in addenda)
    assert any("XmlUtil.registerJaxb" in a for a in addenda)
    # gap-2 fix: the planned-NEW schema is registered for collision detection too
    cips = [o for o in db.added if isinstance(o, ChangeImpactedPath)]
    assert cips and cips[0].path == "ReqNonce.xsd" and cips[0].repo_id == "" and cips[0].kind == "xsd"


def test_fold_dedups_grounding_schema_against_existing(monkeypatch):
    # gap-5 fix: grounding proposing a schema already in the inventory must not duplicate it.
    monkeypatch.setattr(UR, "_analysis_checkouts", lambda db, cid, allow_clone=False: [])
    monkeypatch.setattr(UR, "_checkout_heads", lambda co: {})
    monkeypatch.setattr(UR, "_wire_entity_in_code", lambda co, name: None)
    monkeypatch.setattr(UR, "_wire_entity_paths", lambda co, name: [])
    prior = _prior(technical_analysis={"schema_inventory": [{"path": "ReqTransfer.xsd"}]})
    db = _DB(prior)
    recon = SimpleNamespace(conflicts=[{"id": "c1", "text": "touch ReqTransfer"}],
                            resolutions={"c1": {"chosen_option_id": "brd_wins"}},
                            doc_id="b", doc_version=2,
                            grounding={"status": "ok", "deltas": [{"directive": "d",
                                       "schema_inventory_add": [{"path": "ReqTransfer.xsd"}]}]})
    assert record_reconciliation_version(db, change_request_id="cr", reconciliation=recon) == 4
    inv = db.added[0].technical_analysis["schema_inventory"]
    assert [i["path"] for i in inv].count("ReqTransfer.xsd") == 1      # not doubled


def test_fold_ignores_failed_grounding(monkeypatch):
    # grounding status != ok → no merge, falls back to today's behaviour (S1 presence-check).
    monkeypatch.setattr(UR, "_analysis_checkouts", lambda db, cid, allow_clone=False: [])
    monkeypatch.setattr(UR, "_checkout_heads", lambda co: {})
    monkeypatch.setattr(UR, "_wire_entity_in_code", lambda co, name: None)
    monkeypatch.setattr(UR, "_wire_entity_paths", lambda co, name: [])
    prior = _prior(technical_analysis={"schema_inventory": [{"path": "existing.xsd"}]})
    db = _DB(prior)
    recon = SimpleNamespace(conflicts=[{"id": "c1", "text": "x"}],
                            resolutions={"c1": {"chosen_option_id": "brd_wins"}},
                            doc_id="b", doc_version=2, grounding={"status": "failed"})
    assert record_reconciliation_version(db, change_request_id="cr", reconciliation=recon) == 4
    assert [i["path"] for i in db.added[0].technical_analysis["schema_inventory"]] == ["existing.xsd"]
    assert "grounding" not in db.added[0].technical_analysis["plan_revisions"][-1]


def test_fold_no_impacted_path_when_entity_absent(monkeypatch):
    monkeypatch.setattr(UR, "_analysis_checkouts", lambda db, cid, allow_clone=False: ["/fake"])
    monkeypatch.setattr(UR, "_checkout_heads", lambda co: {})
    monkeypatch.setattr(UR, "_wire_entity_in_code", lambda co, name: False)         # absent → no path
    monkeypatch.setattr(UR, "_wire_entity_paths", lambda co, name: [])
    from app.models.change_analysis import ChangeImpactedPath
    db = _DB(_prior())
    recon = SimpleNamespace(
        conflicts=[{"id": "c1", "text": "BRD introduces ReqMadeUp999"}],
        resolutions={"c1": {"chosen_option_id": "brd_wins"}}, doc_id="brd-2", doc_version=2)
    assert record_reconciliation_version(db, change_request_id="cr", reconciliation=recon) == 4
    assert not any(isinstance(o, ChangeImpactedPath) for o in db.added)   # nothing to register
