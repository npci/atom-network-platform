# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""upload_reconciler — detection foundation for uploaded-doc vs plan reconciliation.

Pure classification + the reconcile short-circuits (no ratified plan / clean /
empty / fail-open) + the conflict-persist path + the downstream gate. Fully
self-contained: fakes the DB and monkeypatches the plan contract + auditor, so no
real DB or LLM is touched (mirrors tests/agents/test_doc_plan_fidelity.py)."""
import asyncio

from app.agents.upload_reconciler import (
    classify_findings, reconcile_upload, has_unresolved_reconciliation, overturns_needs_ack, validate_resolutions,
)


# ── fakes ────────────────────────────────────────────────────────────────────
class _Q:
    def __init__(self, r, update_rowcount=1): self._r = r; self._update_rowcount = update_rowcount
    def filter(self, *a, **k): return self
    def order_by(self, *a, **k): return self
    def first(self): return self._r
    def all(self): return [self._r] if self._r is not None else []
    # The conditional flip 'checking' → 'pending' is a rowcount-gated UPDATE (M3): 1 = the
    # row was still 'checking' (normal), 0 = a concurrent upload superseded it mid-detection.
    def update(self, *a, **k): return self._update_rowcount


class _DB:
    def __init__(self, ca=None, pending=None, update_rowcount=1):
        self._ca = ca
        self._pending = pending
        self._update_rowcount = update_rowcount
        self.added = []
        self.deleted = []
        self.committed = False
        self.rolled_back = False
    def query(self, model, *a, **k):
        if getattr(model, "__name__", "") == "DocumentReconciliation":
            return _Q(self._pending, self._update_rowcount)
        return _Q(self._ca)          # ChangeAnalysis lookup
    def add(self, obj): self.added.append(obj)
    def delete(self, obj): self.deleted.append(obj)
    def commit(self): self.committed = True
    def rollback(self): self.rolled_back = True


class _CA:
    def __init__(self, version): self.version = version


def _patch(monkeypatch, *, plan="PLAN", findings=None, uncovered=None):
    monkeypatch.setattr("app.agents.plan_contract.build_plan_contract", lambda db, cid: plan)
    async def _audit(**kw):
        return {"consistent": not findings, "findings": findings or [], "has_blocker": False}
    monkeypatch.setattr("app.agents.doc_consistency.check_doc_against_plan", _audit)
    # Neutralize the omission pass by default; tests opt in via `uncovered=`.
    async def _extract(pc):
        return [{"id": "r1", "text": "req"}] if uncovered else []
    async def _uncovered(reqs, doc):
        return uncovered or []
    monkeypatch.setattr("app.agents.plan_coverage.extract_plan_requirements", _extract)
    monkeypatch.setattr("app.agents.plan_coverage.find_uncovered_requirements", _uncovered)


# ── pure classification ──────────────────────────────────────────────────────
def test_classify_buckets_jurisdiction():
    conflicts = classify_findings([
        {"severity": "blocker", "kind": "contradiction", "item": "cap", "detail": "BRD says 5000, plan says none"},
        {"severity": "warning", "kind": "endpoint", "item": "/x", "detail": "BRD invents endpoint /x"},
        {"severity": "warning", "kind": "missing_requirement", "item": "confirm", "detail": "REQ_TXN_CONFIRMATION absent from BRD"},
        {"severity": "warning", "kind": "weird", "item": "z", "detail": "unclear"},
    ])
    assert [c["jurisdiction"] for c in conflicts] == [
        "contradicts_plan", "extends_plan", "drops_requirement", "review",
    ]
    for c in conflicts:
        assert [o["id"] for o in c["options"]] == ["brd_wins", "plan_wins", "custom"]
        assert c["options"][-1].get("free_text") is True   # the open text box
        assert c["id"] and c["text"]


def test_classify_empty():
    assert classify_findings([]) == []
    assert classify_findings(None) == []


def test_drop_requirement_option_copy():
    c = classify_findings([{"kind": "missing", "item": "x", "detail": "absent from BRD"}])[0]
    assert c["jurisdiction"] == "drops_requirement"
    assert "add it back to the BRD" in c["options"][1]["label"]


# ── reconcile short-circuits (fail-open) ─────────────────────────────────────
def test_reconcile_no_plan_returns_none(monkeypatch):
    _patch(monkeypatch, plan="", findings=[{"kind": "x", "item": "y", "detail": "z"}])
    db = _DB()
    assert asyncio.run(reconcile_upload(db, change_id="cr", doc_kind="brd", doc_content="body")) is None
    assert db.added == []


def test_reconcile_clean_returns_none(monkeypatch):
    _patch(monkeypatch, plan="PLAN", findings=[])
    db = _DB()
    assert asyncio.run(reconcile_upload(db, change_id="cr", doc_kind="brd", doc_content="body")) is None
    # a transient 'checking' marker is created (for the UI loader), then removed on clean
    assert len(db.added) == 1 and db.added[0].status == "checking" and db.added[0] in db.deleted


def test_reconcile_empty_content_returns_none(monkeypatch):
    _patch(monkeypatch, plan="PLAN", findings=[{"kind": "x", "item": "y", "detail": "z"}])
    assert asyncio.run(reconcile_upload(_DB(), change_id="cr", doc_kind="brd", doc_content="   ")) is None


def test_reconcile_fails_open_on_audit_error(monkeypatch):
    _patch(monkeypatch, plan="PLAN")                      # omission pass neutralized
    async def _boom(*a, **k): raise RuntimeError("llm down")
    monkeypatch.setattr("app.agents.doc_consistency.check_doc_against_plan", _boom)
    monkeypatch.setattr("app.agents.doc_alignment.extract_doc_commitments", _boom)
    db = _DB()
    assert asyncio.run(reconcile_upload(db, change_id="cr", doc_kind="brd", doc_content="body")) is None
    # fail-open: no findings from any axis → the transient marker is removed, no row survives
    assert len(db.added) == 1 and db.added[0] in db.deleted


def test_reconcile_flags_omissions(monkeypatch):
    _patch(monkeypatch, plan="PLAN", findings=[],
           uncovered=[{"id": "r1", "text": "amount = each participant's share"}])
    db = _DB(ca=_CA(version=2))
    out = asyncio.run(reconcile_upload(
        db, change_id="cr", doc_kind="brd", doc_content="body", doc_id="brd-1", doc_version=1))
    assert out is not None and out.status == "pending" and len(out.conflicts) == 1
    c = out.conflicts[0]
    assert c["jurisdiction"] == "drops_requirement" and c["kind"] == "omission"
    assert "participant's share" in c["text"]
    assert [o["id"] for o in c["options"]] == ["brd_wins", "plan_wins", "custom"]


def test_reconcile_merges_additions_and_omissions(monkeypatch):
    _patch(monkeypatch, plan="PLAN",
           findings=[{"severity": "blocker", "kind": "endpoint", "item": "/x", "detail": "invented /x"}],
           uncovered=[{"id": "r1", "text": "must confirm txn"}])
    out = asyncio.run(reconcile_upload(_DB(ca=_CA(version=1)), change_id="cr", doc_kind="brd", doc_content="body"))
    assert sorted(c["jurisdiction"] for c in out.conflicts) == ["drops_requirement", "extends_plan"]


def test_reconcile_dedups_findings_across_windows(monkeypatch):
    # doc > one window → multiple windows; identical finding each → deduped to 1.
    _patch(monkeypatch, plan="PLAN",
           findings=[{"severity": "blocker", "kind": "endpoint", "item": "/x", "detail": "invented /x"}])
    out = asyncio.run(reconcile_upload(_DB(ca=_CA(version=1)), change_id="cr", doc_kind="brd", doc_content="x" * 45000))
    assert out is not None and len(out.conflicts) == 1


def test_reconcile_persists_conflicts(monkeypatch):
    _patch(monkeypatch, plan="PLAN", findings=[
        {"severity": "blocker", "kind": "contradiction", "item": "cap", "detail": "BRD says 5000, plan says none"},
    ])
    db = _DB(ca=_CA(version=3))
    out = asyncio.run(reconcile_upload(
        db, change_id="cr", doc_kind="brd", doc_content="body", doc_id="brd-1", doc_version=2))
    assert out is not None and out.status == "pending"
    assert out.doc_kind == "brd" and out.doc_id == "brd-1" and out.doc_version == 2
    assert out.plan_version_before == 3
    assert len(out.conflicts) == 1 and out.conflicts[0]["jurisdiction"] == "contradicts_plan"
    assert db.committed is True and db.added and db.added[0] is out


def test_reconcile_discards_when_superseded_mid_detection(monkeypatch):
    # A concurrent upload superseded the 'checking' row while this (slow) detection ran →
    # the rowcount-gated flip matches 0 rows. Don't resurrect it back to 'pending' (M3):
    # discard this run and roll back so the newer upload's flow keeps the gate it set.
    _patch(monkeypatch, plan="PLAN", findings=[
        {"severity": "blocker", "kind": "contradiction", "item": "cap", "detail": "BRD says 5000, plan says none"},
    ])
    db = _DB(ca=_CA(version=3), update_rowcount=0)
    out = asyncio.run(reconcile_upload(
        db, change_id="cr", doc_kind="brd", doc_content="body", doc_id="brd-1", doc_version=2))
    assert out is None and db.rolled_back is True


# ── downstream gate helper ───────────────────────────────────────────────────
def test_gate_true_when_pending():
    from types import SimpleNamespace
    assert has_unresolved_reconciliation(
        _DB(pending=SimpleNamespace(status="pending")), "cr", "brd") is True


def test_gate_false_when_none():
    assert has_unresolved_reconciliation(_DB(pending=None), "cr", "brd") is False


def test_gate_true_when_applying_fresh():
    from datetime import datetime, timezone
    from types import SimpleNamespace
    row = SimpleNamespace(status="applying", updated_at=datetime.now(timezone.utc), created_at=None)
    assert has_unresolved_reconciliation(_DB(pending=row), "cr", "brd") is True


def test_gate_false_when_applying_stale():
    # A worker that never got to flip 'applying' -> resolved/applied (killed, DB error,
    # or a live-broker/no-worker enqueue) must not block the change forever.
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    row = SimpleNamespace(status="applying",
                           updated_at=datetime.now(timezone.utc) - timedelta(minutes=30),
                           created_at=None)
    assert has_unresolved_reconciliation(_DB(pending=row), "cr", "brd") is False


# ── overturns-ratified soft gate (§8.1) ──────────────────────────────────────
def test_overturns_gate_blocks_until_acked():
    from types import SimpleNamespace
    flagged = SimpleNamespace(grounding={"status": "ok", "deltas": [{"overturns_ratified": True}]})
    assert overturns_needs_ack(_DB(pending=flagged), "cr", "brd") is True         # flagged, unacked → block
    acked = SimpleNamespace(grounding={"status": "ok", "overturns_acked": True,
                                       "deltas": [{"overturns_ratified": True}]})
    assert overturns_needs_ack(_DB(pending=acked), "cr", "brd") is False          # acknowledged → clear
    none = SimpleNamespace(grounding={"status": "ok", "deltas": [{"overturns_ratified": False}]})
    assert overturns_needs_ack(_DB(pending=none), "cr", "brd") is False           # nothing overturns
    assert overturns_needs_ack(_DB(pending=None), "cr", "brd") is False           # no reconciliation


# ── resolution validation ────────────────────────────────────────────────────
_OPTS = [{"id": "brd_wins"}, {"id": "plan_wins"}, {"id": "custom", "free_text": True}]
_CONFLICTS = [{"id": "c1", "options": _OPTS}, {"id": "c2", "options": _OPTS}]


def test_validate_ok_chosen_and_custom():
    ok, err = validate_resolutions(_CONFLICTS, {
        "c1": {"chosen_option_id": "brd_wins"},
        "c2": {"chosen_option_id": "custom", "custom_answer": "cap only for P2P"},
    })
    assert ok and err is None


def test_validate_missing_conflict():
    ok, err = validate_resolutions(_CONFLICTS, {"c1": {"chosen_option_id": "brd_wins"}})
    assert not ok and "c2" in err


def test_validate_empty_answer():
    ok, err = validate_resolutions(_CONFLICTS, {"c1": {"chosen_option_id": "brd_wins"}, "c2": {}})
    assert not ok


def test_validate_custom_needs_text():
    ok, err = validate_resolutions([_CONFLICTS[0]], {"c1": {"chosen_option_id": "custom"}})
    assert not ok and "custom" in err.lower()


def test_validate_unknown_option():
    ok, err = validate_resolutions([_CONFLICTS[0]], {"c1": {"chosen_option_id": "nope"}})
    assert not ok and "unknown" in err.lower()


# ── apply-on-approval (A1) ───────────────────────────────────────────────────
class _ApprQ:
    def __init__(self, rows): self._rows = rows
    def filter(self, *a, **k): return self
    def with_for_update(self, *a, **k): return self   # row lock — no-op in the fake
    def all(self): return self._rows


class _ApprDB:
    def __init__(self, rows):
        self._rows = rows
        self.committed = False
    def query(self, *a, **k): return _ApprQ(self._rows)
    def commit(self): self.committed = True
    def rollback(self): pass


def test_apply_on_approval_versions_and_marks_applied(monkeypatch):
    from types import SimpleNamespace
    from app.agents.upload_reconciler import apply_reconciliation_on_brd_approval
    called = []
    monkeypatch.setattr("app.agents.plan_versioning.record_reconciliation_version",
                        lambda db, **kw: called.append(kw) or 4)
    recon = SimpleNamespace(status="resolved", conflicts=[], resolutions={}, doc_id="b", doc_version=2)
    db = _ApprDB([recon])
    n = apply_reconciliation_on_brd_approval(db, "cr", approved_by="u")
    assert n == 1 and recon.status == "applied" and db.committed
    assert called[0]["change_request_id"] == "cr" and called[0]["decided_by"] == "u"


def test_apply_on_approval_noop_when_none(monkeypatch):
    from app.agents.upload_reconciler import apply_reconciliation_on_brd_approval
    monkeypatch.setattr("app.agents.plan_versioning.record_reconciliation_version", lambda db, **kw: 0)
    db = _ApprDB([])
    assert apply_reconciliation_on_brd_approval(db, "cr") == 0
    assert db.committed is False
