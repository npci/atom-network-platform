# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase A/B split (THE BOOK v3.4): XSD generation (Phase A, human-gated) →
code generation (Phase B, adopts Phase A's workspace) → one combined MR.

Covers the deterministic pieces without a live DB/network: the new transition,
the XsdScope handoff round-trip, the workspace-id indirection + GC parent/child
guard, the kind-branched _step, and _phase_freeze_xsd persisting the handoff."""
import asyncio
from types import SimpleNamespace

from app.agents import agentic_orchestrator as O
from app.agents import workspace_local as W
from app.agents.agentic_state import _can_transition, VALID_TRANSITIONS
from app.agents.agentic_subagents import XsdScope, xsd_scope_to_dict, xsd_scope_from_dict
from app.models.agentic import AgenticPhase as P


# ── State machine ──────────────────────────────────────────────────────────────

def test_phase_a_gate_transitions():
    # Phase A stops at the XSD-approval gate; from there it completes or (forward-compat) codes.
    assert _can_transition("xsd_discovery", P.AWAITING_XSD_APPROVAL)
    assert _can_transition("awaiting_xsd_approval", P.COMPLETED)
    assert _can_transition("awaiting_xsd_approval", P.CODE_CHANGE)
    # A full/code run still chains XSD → CODE_CHANGE directly (legacy unchanged).
    assert _can_transition("xsd_discovery", P.CODE_CHANGE)
    # The new phase is in the table and is not a sink mid-flow.
    assert P.AWAITING_XSD_APPROVAL in VALID_TRANSITIONS
    assert VALID_TRANSITIONS[P.AWAITING_XSD_APPROVAL]


# ── XsdScope handoff round-trip ──────────────────────────────────────────────────

def test_xsd_scope_roundtrip_preserves_decisions():
    scope = XsdScope(decisions=[{"path": "A.xsd", "decision": "extend"}],
                     edits_applied=["r1:A.xsd"], diff_record={"r1:A.xsd": {"new": ["E"]}},
                     java_links=[{"el": "E", "java": "Foo"}], determinism_ok=True, final_text="done")
    d = xsd_scope_to_dict(scope)
    back = xsd_scope_from_dict(d)
    assert back.decisions == scope.decisions
    assert back.edits_applied == ["r1:A.xsd"]
    assert back.diff_record == {"r1:A.xsd": {"new": ["E"]}}
    assert back.java_links and back.determinism_ok and back.final_text == "done"


def test_xsd_scope_from_dict_tolerates_missing():
    empty = xsd_scope_from_dict(None)
    assert empty.decisions == [] and empty.edits_applied == [] and empty.determinism_ok is True


# ── Workspace-id indirection ─────────────────────────────────────────────────────

def test_ws_id_self_for_full_and_xsd():
    assert O._ws_id(SimpleNamespace(id="run-a", workspace_run_id=None)) == "run-a"


def test_ws_id_adopts_parent_for_phase_b():
    # Phase B edits/verifies/pushes Phase A's tree → disk ops route to the parent run id.
    assert O._ws_id(SimpleNamespace(id="run-b", workspace_run_id="run-a")) == "run-a"


# ── _step branches on kind ───────────────────────────────────────────────────────

def _record_advance(monkeypatch):
    calls = []
    monkeypatch.setattr(O.S, "advance", lambda db, run, to: calls.append(to))
    monkeypatch.setattr(O, "emit_event", lambda *a, **k: None)
    return calls


def test_step_xsd_kind_stops_at_xsd_gate(monkeypatch):
    calls = _record_advance(monkeypatch)
    async def fake_xsd(db, run, art, model): art["xsd_scope"] = XsdScope()
    monkeypatch.setattr(O, "_phase_xsd", fake_xsd)
    monkeypatch.setattr(O, "_phase_freeze_xsd", lambda db, run, art, **k: None)
    # Approach already decided → apply pass (skips the propose gate); xsd kind stops at XSD approval.
    run = SimpleNamespace(id="r", kind="xsd", phase="xsd_discovery", selected_repo_ids=[],
                          handoff_json={"approach_decision": {"selected_option_id": "reuse"}})
    asyncio.run(O._step(None, run, {"intent": "x"}, None))
    assert calls == [P.AWAITING_XSD_APPROVAL]          # gated; never advanced to CODE_CHANGE


def test_step_full_kind_chains_to_code(monkeypatch):
    calls = _record_advance(monkeypatch)
    async def fake_xsd(db, run, art, model): art["xsd_scope"] = XsdScope()
    monkeypatch.setattr(O, "_phase_xsd", fake_xsd)
    run = SimpleNamespace(id="r", kind="full", phase="xsd_discovery",
                          handoff_json={"approach_decision": {"selected_option_id": "reuse"}})
    asyncio.run(O._step(None, run, {"intent": "x"}, None))
    assert calls == [P.CODE_CHANGE]


# ── _phase_freeze_xsd persists the handoff ───────────────────────────────────────

def test_phase_freeze_xsd_persists_scope_and_xsd_contents(monkeypatch):
    monkeypatch.setattr(O, "emit_event", lambda *a, **k: None)
    monkeypatch.setattr(O.agentic_push, "branch_name", lambda title: "agentic/x")
    monkeypatch.setattr(O, "_title", lambda db, run: "feature")
    monkeypatch.setattr(O.M, "freeze_manifest", lambda db, rid, man, diffs=None: None)
    monkeypatch.setattr(O.M, "build_manifest", lambda **k: {"manifest_hash": "h123", **k})
    ops = [SimpleNamespace(op="add", repo_id="r1", path="A.xsd", content="<xsd/>"),
           SimpleNamespace(op="add", repo_id="r1", path="B.xjb", content="<bindings/>")]
    monkeypatch.setattr(O, "_disk_change_set", lambda db, run: SimpleNamespace(operations=ops))

    run = SimpleNamespace(id="r", selected_repo_ids=["r1"], handoff_json=None, manifest_hash=None)
    art = {"repo_base_sha": {"r1": "sha1"}, "xsd_scope": XsdScope(edits_applied=["r1:A.xsd"])}
    O._phase_freeze_xsd(None, run, art)

    assert run.manifest_hash == "h123"
    assert run.handoff_json["xsd_scope"]["edits_applied"] == ["r1:A.xsd"]
    paths = {f["path"] for f in run.handoff_json["xsd_files"]}
    assert paths == {"A.xsd", "B.xjb"}                  # full XSD/.xjb contents carried to Phase B


# ── _rehydrate_art restores XsdScope for Phase B ─────────────────────────────────

def test_rehydrate_restores_xsd_scope_for_code_run(monkeypatch):
    monkeypatch.setattr(O.workspace_local, "read_base_sha", lambda ws, rid: "sha-" + rid)
    monkeypatch.setattr(O.context_assembler, "assemble_context_pack",
                        lambda db, **k: SimpleNamespace(tag="CTX"))
    parent = SimpleNamespace(id="run-a", handoff_json={"xsd_scope": {"edits_applied": ["r1:A.xsd"]}})
    db = SimpleNamespace(get=lambda model, rid: parent)
    run = SimpleNamespace(id="run-b", kind="code", parent_run_id="run-a",
                          workspace_run_id="run-a", phase="code_change",
                          change_request_id="cr", selected_repo_ids=["r1"])
    art = {"intent": "x"}
    O._rehydrate_art(db, run, art)
    assert art["xsd_scope"].edits_applied == ["r1:A.xsd"]
    assert art["repo_base_sha"] == {"r1": "sha-r1"}     # base SHA read from the ADOPTED (parent) tree


# ── materialize_files (Phase-B fallback restore) ─────────────────────────────────

def test_materialize_files_writes_repo_scoped_contents(tmp_path, monkeypatch):
    monkeypatch.setattr(W.settings, "agentic_workspace_root", str(tmp_path))
    (tmp_path / "run-a" / "r1").mkdir(parents=True)
    files = [{"repo_id": "r1", "path": "schemas/A.xsd", "content": "<xsd/>"},
             {"repo_id": "other", "path": "Z.xsd", "content": "skip"}]   # other repo → skipped
    n = W.materialize_files("run-a", "r1", files)
    assert n == 1
    assert (tmp_path / "run-a" / "r1" / "schemas" / "A.xsd").read_text() == "<xsd/>"
    assert not (tmp_path / "run-a" / "r1" / "Z.xsd").exists()


# ── GC parent/child guard ────────────────────────────────────────────────────────

class _FakeQuery:
    def __init__(self, result): self._r = result
    def filter(self, *a, **k): return self
    def first(self): return self._r


class _FakeDB:
    def __init__(self, dependent): self._dep = dependent
    def query(self, *a, **k): return _FakeQuery(self._dep)


def test_has_active_dependent():
    assert W._has_active_dependent(_FakeDB("child-id"), "run-a") is True
    assert W._has_active_dependent(_FakeDB(None), "run-a") is False
    assert W._has_active_dependent(None, "run-a") is False


def test_is_collectable_keeps_parent_with_active_child():
    from datetime import timedelta
    from app.models.base import utcnow
    now = utcnow()
    # kind='xsd': a Phase-A parent (never pushes itself) — keeps this test on the
    # dependent-child guard; the deferred-push GC guard has its own tests.
    run = SimpleNamespace(status="completed", updated_at=now - timedelta(hours=99),
                          lease_owner=None, lease_expires_at=None, id="run-a", kind="xsd")
    # Terminal + old + lease-free, but a non-terminal Phase-B child still needs the tree → keep.
    assert W._is_collectable(run, now, 1, db=_FakeDB("child")) is False
    # No active dependent → collectable.
    assert W._is_collectable(run, now, 1, db=_FakeDB(None)) is True


def test_has_active_dependent_protects_recently_terminal_child():
    # Regression (run 74a785ac): the hourly GC collected the Phase-A parent tree the
    # moment its Phase-B child went terminal — 2 minutes before a manual Resume that
    # continues from that same tree. A terminal child inside its own resume TTL must
    # still protect the parent workspace; past the TTL it must not.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from datetime import timedelta
    import app.models  # register every model so FKs resolve
    from app.core.database import Base
    from app.models.agentic import AgenticRun
    from app.models.base import utcnow

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[AgenticRun.__table__])
    s = sessionmaker(bind=engine)()
    now = utcnow()
    s.add(AgenticRun(id="child-1", change_request_id="cr-1", phase="failed", status="failed",
                     kind="code", parent_run_id="parent-1", workspace_run_id="parent-1"))
    s.commit()

    # freshly-terminal child → parent tree protected for the child's resume window
    assert W._has_active_dependent(s, "parent-1", now=now, ttl_hours=24) is True
    # child past its resume TTL → parent collectable again
    s.query(AgenticRun).filter_by(id="child-1").update(
        {"updated_at": now - timedelta(hours=48)}, synchronize_session=False)
    s.commit()
    assert W._has_active_dependent(s, "parent-1", now=now, ttl_hours=24) is False
    # legacy call shape (no TTL): terminal children never count
    assert W._has_active_dependent(s, "parent-1") is False
    s.close()
