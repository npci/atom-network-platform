# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Governance orchestrator — deterministic pieces on an in-memory sqlite.

The async phase bodies (review/fix loops) ride machinery covered elsewhere
(run_review directive coverage, run_agent_loop, verifier); here we lock in the
governance-specific logic: stage creation pins the skill version, sequencing
order, the fix-delta baseline filter, the deferred-push overlay merge, the
derived status resolver (incl. parent-rerun invalidation), and fail-loud skill
loading.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 — register every model on Base before create_all
# Import EVERY model module (several FK targets aren't in app.models.__init__ —
# code_repo, phase_b, partner_agents, …) so create_all sees the full graph.
import importlib as _importlib
import pkgutil as _pkgutil
for _m in _pkgutil.iter_modules(__import__("app.models", fromlist=["x"]).__path__):
    _importlib.import_module(f"app.models.{_m.name}")
from app.agents import governance_orchestrator as G
from app.agents.agentic_tools import FileOp
from app.core.database import Base
from app.models.agentic import AgenticRun, AgenticRunRepo, ChangeManifest
from app.models.governance_skill import GovernanceSkill


@pytest.fixture()
def db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _skill(db, stype="ea", version=1, content="## RULE X-1: t\nb"):
    from app.agents import governance_skills as GS
    row = GovernanceSkill(skill_type=stype, version=version, content=content,
                          checksum=GS.checksum(content), rules_json=[{"id": "X-1", "title": "t"}])
    db.add(row)
    db.flush()
    return row


def _parent(db, change_id="chg-1", *, approved=True, status="completed", pushed=False):
    run = AgenticRun(change_request_id=change_id, phase="completed", status=status,
                     kind="code", selected_repo_ids=["r1"], attempts_json={},
                     handoff_json={"feature_branch": "atom/x", "push_deferred": not pushed})
    db.add(run)
    db.flush()
    man = ChangeManifest(run_id=run.id, manifest_hash="p" * 64, selected_repo_ids=["r1"],
                         per_repo=[{"repo_id": "r1", "base_commit_sha": "b" * 40,
                                    "shared_branch_name": "atom/x"}],
                         operations=[{"op": "modify", "repo_id": "r1", "path": "src/A.java",
                                      "content_hash": "a" * 64}],
                         verification={}, review={})
    if approved:
        from app.models.base import utcnow
        man.approved_at = utcnow()
        man.approved_by = "u1"
    db.add(man)
    if pushed:
        db.add(AgenticRunRepo(run_id=run.id, repo_id="r1", branch="atom/x",
                              push_state="pushed", pushed_manifest_hash="p" * 64))
    db.flush()
    return run, man


# ── skill loading: deterministic or loud ──────────────────────────────────────

def test_load_skill_missing_fails_loud(db):
    with pytest.raises(RuntimeError, match="not uploaded"):
        G.load_skill(db, "ea")


def test_load_skill_active_is_highest_version(db):
    _skill(db, "ea", 1)
    _skill(db, "ea", 2, content="## RULE X-2: t2\nb")
    assert G.load_skill(db, "ea").version == 2
    assert G.load_skill(db, "ea", version=1).version == 1


# ── stage creation + sequencing ───────────────────────────────────────────────

def test_create_stage_run_pins_skill_version(db):
    _skill(db, "ea", 3)
    parent, _ = _parent(db)
    from app.agents import governance_skills as GS
    run, created = G.create_stage_run(db, parent, "gov_ea", created_by="u1")
    assert created and run.kind == "gov_ea"
    gov = (run.handoff_json or {}).get("governance") or {}
    assert gov["skill"] == {"type": "ea", "name": "default", "version": 3,
                            "checksum": GS.checksum("## RULE X-1: t\nb"), "smoke_status": None}
    # 0118: the full pinned slot set rides alongside the back-compat primary pin.
    assert [p_["version"] for p_ in gov["skills"]] == [3]
    assert gov["parent_run_id"] == parent.id
    assert run.workspace_run_id == parent.id


def test_create_stage_run_without_skill_fails_loud(db):
    parent, _ = _parent(db)
    with pytest.raises(RuntimeError, match="not uploaded"):
        G.create_stage_run(db, parent, "gov_ea", created_by="u1")


def test_stage_order_is_ea_then_infosec():
    assert G.FIRST_STAGE_KIND == "gov_ea"
    assert G.STAGES["gov_ea"]["next_kind"] == "gov_is"
    assert G.STAGES["gov_is"]["next_kind"] is None


def test_create_stage_run_returns_existing_active_run_untouched(db):
    _skill(db, "ea", 1)
    parent, _ = _parent(db)
    active = AgenticRun(change_request_id=parent.change_request_id, phase="review",
                        status="active", kind="gov_ea", selected_repo_ids=["r1"],
                        attempts_json={}, handoff_json={"governance": {"stage": "ea"}})
    db.add(active)
    db.flush()
    run, created = G.create_stage_run(db, parent, "gov_ea", created_by="u1")
    assert not created and run.id == active.id
    # the winner's governance json is NOT overwritten by the losing create
    assert (run.handoff_json or {}).get("governance") == {"stage": "ea"}


# ── fix delta (baseline filter) ───────────────────────────────────────────────

def _gov_run(db, parent, *, source="workspace", baseline=None):
    run = AgenticRun(change_request_id=parent.change_request_id, phase="review",
                     status="active", kind="gov_ea", selected_repo_ids=["r1"],
                     attempts_json={}, parent_run_id=parent.id, workspace_run_id=parent.id,
                     handoff_json={"governance": {
                         "stage": "ea", "parent_run_id": parent.id, "source": source,
                         "baseline": baseline or {}, "stage_base": {"r1": "b" * 40},
                         "feature_branch": "atom/x",
                         "skill": {"type": "ea", "version": 1, "checksum": "c" * 64}}})
    db.add(run)
    db.flush()
    return run


def test_fix_delta_excludes_unchanged_parent_files(db, monkeypatch):
    parent, _ = _parent(db)
    from app.agents import governance_skills as GS
    baseline = {"r1": {"src/A.java": GS.checksum("parent content"), "src/Gone.java": "__deleted__"}}
    run = _gov_run(db, parent, baseline=baseline)
    ops = [
        FileOp(op="modify", repo_id="r1", path="src/A.java",
               content="parent content", content_hash=GS.checksum("parent content")),   # untouched
        FileOp(op="modify", repo_id="r1", path="src/B.java",
               content="fixed", content_hash=GS.checksum("fixed")),                     # gov edit (new to baseline)
        FileOp(op="delete", repo_id="r1", path="src/Gone.java", content=None, content_hash=None),  # parent's delete
    ]
    import app.agents.agentic_orchestrator as O
    monkeypatch.setattr(O, "_disk_change_set", lambda _db, _run: SimpleNamespace(operations=ops))
    delta = G._fix_delta_ops(db, run)
    assert [(o.op, o.path) for o in delta] == [("modify", "src/B.java")]


def test_fix_delta_recloned_branch_is_everything_changed_since_clone(db, monkeypatch):
    parent, _ = _parent(db)
    run = _gov_run(db, parent, source="recloned_branch", baseline={"r1": {"src/A.java": "x" * 64}})
    ops = [FileOp(op="modify", repo_id="r1", path="src/C.java", content="fix", content_hash="h" * 64)]
    import app.agents.agentic_orchestrator as O
    monkeypatch.setattr(O, "_disk_change_set", lambda _db, _run: SimpleNamespace(operations=ops))
    delta = G._fix_delta_ops(db, run)
    assert [(o.op, o.path) for o in delta] == [("modify", "src/C.java")]


# ── deferred-push overlay ─────────────────────────────────────────────────────

def _completed_stage_with_manifest(db, parent, kind, ops, *, result="fixes_approved", approved=True):
    run = AgenticRun(change_request_id=parent.change_request_id, phase="completed",
                     status="completed", kind=kind, selected_repo_ids=["r1"],
                     attempts_json={}, parent_run_id=parent.id, workspace_run_id=parent.id,
                     handoff_json={"governance": {"stage": G.STAGES[kind]["stage"],
                                                  "parent_run_id": parent.id, "result": result}})
    db.add(run)
    db.flush()
    man = ChangeManifest(run_id=run.id, manifest_hash=("e" if kind == "gov_ea" else "i") * 64,
                         selected_repo_ids=["r1"],
                         per_repo=[{"repo_id": "r1", "base_commit_sha": "b" * 40,
                                    "shared_branch_name": "atom/x"}],
                         operations=ops, verification={}, review={})
    if approved:
        from app.models.base import utcnow
        man.approved_at = utcnow()
    db.add(man)
    db.flush()
    return run


def test_overlay_identity_when_no_stage_fixes(db):
    parent, man = _parent(db)
    assert G.overlay_stage_fixes(db, parent, man) is man


def test_overlay_replaces_changed_and_appends_new_paths(db):
    parent, man = _parent(db)
    _completed_stage_with_manifest(
        db, parent, "gov_ea",
        [{"op": "modify", "repo_id": "r1", "path": "src/A.java", "content_hash": "e1" * 32},
         {"op": "add", "repo_id": "r1", "path": "src/New.java", "content_hash": "e2" * 32}])
    eff = G.overlay_stage_fixes(db, parent, man)
    assert eff is not man
    by_path = {o["path"]: o for o in eff.operations}
    assert by_path["src/A.java"]["content_hash"] == "e1" * 32     # stage hash wins
    assert "src/New.java" in by_path                              # fix file appended
    assert eff.manifest_hash == man.manifest_hash                 # parent approval binding kept


def test_overlay_ignores_unapproved_or_unfinished_stages(db):
    parent, man = _parent(db)
    _completed_stage_with_manifest(
        db, parent, "gov_ea",
        [{"op": "modify", "repo_id": "r1", "path": "src/A.java", "content_hash": "z" * 64}],
        approved=False)                                           # gate never approved it
    other = _completed_stage_with_manifest(
        db, parent, "gov_is",
        [{"op": "modify", "repo_id": "r1", "path": "src/A.java", "content_hash": "q" * 64}])
    other.status = "failed"                                       # not a finished stage
    db.flush()
    assert G.overlay_stage_fixes(db, parent, man) is man


def test_overlay_infosec_wins_over_ea_on_the_same_path(db):
    parent, man = _parent(db)
    _completed_stage_with_manifest(
        db, parent, "gov_ea",
        [{"op": "modify", "repo_id": "r1", "path": "src/A.java", "content_hash": "e1" * 32}])
    _completed_stage_with_manifest(
        db, parent, "gov_is",
        [{"op": "modify", "repo_id": "r1", "path": "src/A.java", "content_hash": "i1" * 32}])
    eff = G.overlay_stage_fixes(db, parent, man)
    by_path = {o["path"]: o for o in eff.operations}
    # InfoSec runs LAST on the EA-fixed tree — its content is the final state.
    assert by_path["src/A.java"]["content_hash"] == "i1" * 32


# ── derived status resolver ───────────────────────────────────────────────────

def test_status_disabled_flag_short_circuits(db, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "governance_reviews_enabled", False, raising=False)
    out = G.governance_status(db, "chg-1")
    assert out["enabled"] is False and out["all_passed"] is True   # gate inert when off


def test_status_progression_and_all_passed(db, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "governance_reviews_enabled", True, raising=False)
    parent, _ = _parent(db)
    out = G.governance_status(db, parent.change_request_id)
    assert out["started"] is False and out["all_passed"] is False

    ea = _completed_stage_with_manifest(db, parent, "gov_ea", [], result="clean")
    out = G.governance_status(db, parent.change_request_id)
    assert out["ea"]["passed"] is True and out["infosec"]["passed"] is False
    assert out["all_passed"] is False

    _completed_stage_with_manifest(db, parent, "gov_is", [], result="overridden")
    out = G.governance_status(db, parent.change_request_id)
    assert out["all_passed"] is True
    assert out["ea"]["run_id"] == ea.id


def test_status_parent_rerun_invalidates_prior_stages(db, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "governance_reviews_enabled", True, raising=False)
    parent, _ = _parent(db)
    _completed_stage_with_manifest(db, parent, "gov_ea", [], result="clean")
    _completed_stage_with_manifest(db, parent, "gov_is", [], result="clean")
    assert G.governance_status(db, parent.change_request_id)["all_passed"] is True
    # A NEW approved codegen run supersedes the old one — stages keyed to the old
    # parent no longer count; governance must re-run with zero bookkeeping.
    _parent(db, change_id=parent.change_request_id)
    out = G.governance_status(db, parent.change_request_id)
    assert out["all_passed"] is False and out["started"] is False


# ── re-cloned review change-set: superseded by
#    test_review_change_set_recloned_uses_parent_base_to_tip_diff (F4) below. ──


# ── findings ledger (review points raised → open|fixed lifecycle) ─────────────

def test_findings_ledger_marks_fixed_points_across_rounds():
    it1 = {"category": "security", "severity": "blocker", "file": "A.java", "line": 10,
           "why": "hardcoded secret", "suggested_fix": "move to config"}
    it2 = {"category": "convention", "severity": "error", "file": "B.java", "line": 5,
           "why": "controller imports repository"}
    gov = {}
    G._update_findings_ledger(gov, [it1, it2], rounds=1)
    assert {e["status"] for e in gov["raised_findings"]} == {"open"}
    assert len(gov["raised_findings"]) == 2

    # Round 2: it1 got an explicit PASS (in fixed_keys) → fixed; it2 re-raised STILL OPEN
    # → open. Flipping is verdict-driven now, never by mere absence (F2).
    G._update_findings_ledger(
        gov, [{**it2, "why": "STILL OPEN: controller imports repository"}], rounds=2,
        fixed_keys={G._finding_key(it1)})
    by_file = {e["file"]: e["status"] for e in gov["raised_findings"]}
    assert by_file == {"A.java": "fixed", "B.java": "open"}
    # Ledger keeps the raised point visible even though it is no longer re-reported.
    assert any(e["why"] == "hardcoded secret" for e in gov["raised_findings"])


def test_findings_ledger_excludes_reviewer_gaps_and_round1_never_fixes():
    gov = {}
    G._update_findings_ledger(gov, [
        {"category": "directive", "severity": "blocker", "why": "[D1] NOT VERIFIED", "reviewer_gap": True},
        {"category": "security", "severity": "blocker", "file": "A.java", "why": "xxe"},
    ], rounds=1)
    assert len(gov["raised_findings"]) == 1        # gap sentinel is not a code finding
    # A clean round 1 after a crash-resume must not mark anything fixed prematurely.
    gov2 = {"raised_findings": [{"key": "k1", "round": 1, "status": "open",
                                "category": "security", "severity": "blocker",
                                "file": "A.java", "line": 1, "why": "xxe", "suggested_fix": None}]}
    G._update_findings_ledger(gov2, [], rounds=1)
    assert gov2["raised_findings"][0]["status"] == "open"


def test_ledger_backfill_from_persisted_review_rows(db):
    """A stage run whose reviews predate the live ledger reconstructs it from
    review_findings: latest-round blockers are open, earlier-only ones fixed."""
    from app.models.agentic import ReviewFinding
    parent, _ = _parent(db)
    run = _gov_run(db, parent)
    rows = [
        # round 1 — two real findings + a reviewer-gap sentinel (must be excluded)
        ReviewFinding(run_id=run.id, round=1, category="security", severity="blocker",
                      file="A.java", line=10, why="hardcoded secret", blocking=True),
        ReviewFinding(run_id=run.id, round=1, category="convention", severity="error",
                      file="B.java", line=5, why="controller imports repository", blocking=True),
        ReviewFinding(run_id=run.id, round=1, category="directive", severity="blocker",
                      why="[D2] NOT VERIFIED — directive unaddressed", blocking=True),
        # round 2 — only the convention point survives (re-raised STILL OPEN)
        ReviewFinding(run_id=run.id, round=2, category="convention", severity="error",
                      file="B.java", line=5, why="STILL OPEN: controller imports repository",
                      blocking=True),
    ]
    db.add_all(rows)
    db.flush()
    ledger = G._ledger_from_review_rows(db, run)
    by_file = {e["file"]: e["status"] for e in ledger}
    assert by_file == {"A.java": "fixed", "B.java": "open"}
    assert len(ledger) == 2                       # gap sentinel excluded
    # And _stage_view falls back to the reconstruction when the live ledger is empty.
    view = G._stage_view(db, parent, "gov_ea")
    assert {e["file"] for e in view["raised_findings"]} == {"A.java", "B.java"}


def test_contained_path_refuses_escapes(tmp_path, monkeypatch):
    """Cited paths are model output: both the snapshot read and the restore write
    must stay inside the repo clone (an absolute path makes `/` drop the root)."""
    rd = tmp_path / "ws" / "repo1"
    (rd / "src").mkdir(parents=True)
    (rd / "src" / "App.java").write_text("ok", encoding="utf-8")
    monkeypatch.setattr(G.workspace_local, "repo_dir", lambda ws, rid: rd)

    inside = G._contained_path("ws", "repo1", "src/App.java")
    assert inside is not None and inside.read_text(encoding="utf-8") == "ok"
    assert G._contained_path("ws", "repo1", "./src/../src/App.java") == inside

    for escape in ("../../etc/passwd", "/etc/passwd", "/proc/self/environ",
                   "src/../../../../etc/passwd"):
        assert G._contained_path("ws", "repo1", escape) is None, escape


# ── cited-path containment (reviewer JSON is model output, not trusted input) ─

def test_contained_path_allows_inside_and_blocks_escapes(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    (clone / "src").mkdir(parents=True)
    monkeypatch.setattr(G.workspace_local, "repo_dir", lambda _ws, _rid: clone)
    inside = G._contained_path("ws", "r1", "src/A.java")
    assert inside is not None and str(inside).startswith(str(clone.resolve()))
    # Traversal and absolute paths must resolve to None — they reach BOTH a read
    # (snapshot capture) and a write (unapproved-edit restore).
    assert G._contained_path("ws", "r1", "../outside.txt") is None
    assert G._contained_path("ws", "r1", "src/../../escape.java") is None
    assert G._contained_path("ws", "r1", "/etc/passwd") is None


# ── Batch A: false-clean cluster (F1/F2/F3/F6) ────────────────────────────────

def test_directive_verdicts_requires_anchored_pass_fail():
    from app.agents import agentic_review as AR
    F = AR.Finding
    findings = [
        F(severity="info", category="directive", why="[D1] PASS — ok at A.java:10", blocking=False),
        F(severity="blocker", category="directive", why="[D2] FAIL — bad at B.java:5", blocking=True),
        F(severity="info", category="directive", why="[D3] considered, no issue", blocking=False),  # vacuous mention
        F(severity="info", category="correctness", why="unrelated note", blocking=False),
    ]
    v = G._directive_verdicts(findings)
    assert v == {1: "PASS", 2: "FAIL"}          # D3's bare mention is NOT a verdict


def test_enforce_directive_coverage_injects_gap_for_unverdicted():
    from app.agents import agentic_review as AR
    F = AR.Finding
    # 3 directives; only D1 PASS + D2 FAIL verdicted; D3 vacuously mentioned.
    findings = [
        F(severity="info", category="directive", why="[D1] PASS — A.java:1", blocking=False),
        F(severity="blocker", category="directive", why="[D2] FAIL — layering", file="B.java", blocking=True),
        F(severity="info", category="directive", why="[D3] looks fine", blocking=False),
    ]
    G._enforce_directive_coverage(AR, findings, 3, lambda i: f"rule R-{i}")
    gaps = [f for f in findings if AR._is_reviewer_gap(f)]
    assert len(gaps) == 1 and "[D3]" in gaps[0].why and gaps[0].blocking


def test_enforce_directive_coverage_empty_verdict_gaps_everything():
    from app.agents import agentic_review as AR
    findings = []                                # lost/empty round-2 verdict
    G._enforce_directive_coverage(AR, findings, 4, lambda i: f"prior blocker P{i}")
    gaps = [f for f in findings if AR._is_reviewer_gap(f)]
    assert len(gaps) == 4 and all(g.blocking for g in gaps)   # LOST ≠ CLEAN


def test_enforce_directive_coverage_no_duplicate_when_sentinel_exists():
    from app.agents import agentic_review as AR
    F = AR.Finding
    findings = [F(severity="blocker", category="directive",
                  why="[D1] NOT VERIFIED — the reviewer did not return a verdict for this binding directive: x",
                  blocking=True)]
    G._enforce_directive_coverage(AR, findings, 1, lambda i: "rule R-1")
    assert len(findings) == 1                    # no duplicate gap


def test_ledger_flips_fixed_only_on_explicit_pass_not_absence():
    it1 = {"category": "security", "severity": "blocker", "file": "A.java", "why": "secret"}
    it2 = {"category": "convention", "severity": "error", "file": "B.java", "why": "layering"}
    gov = {}
    G._update_findings_ledger(gov, [it1, it2], rounds=1)
    assert {e["status"] for e in gov["raised_findings"]} == {"open"}
    # Round 2 returns NOTHING (lost verdict) with NO fixed_keys → nothing flips to fixed.
    G._update_findings_ledger(gov, [], rounds=2, fixed_keys=set())
    assert {e["status"] for e in gov["raised_findings"]} == {"open"}
    # Only an explicit PASS (fixed_keys) flips A.java.
    G._update_findings_ledger(gov, [], rounds=2, fixed_keys={G._finding_key(it1)})
    by_file = {e["file"]: e["status"] for e in gov["raised_findings"]}
    assert by_file == {"A.java": "fixed", "B.java": "open"}


def test_fix_delta_emits_delete_for_reverted_parent_add(db, monkeypatch, tmp_path):
    from app.agents import governance_skills as GS
    parent, _ = _parent(db)
    # Parent added Bad.java (base did not have it); baseline recorded it. The fixer
    # deleted it → it drops out of changed_files entirely. Delta must still emit a delete.
    baseline = {"r1": {"src/Bad.java": GS.checksum("bad content")}}
    run = _gov_run(db, parent, baseline=baseline)
    (tmp_path / "src").mkdir(parents=True)       # Bad.java absent on disk (deleted)
    monkeypatch.setattr(G.workspace_local, "repo_dir", lambda _ws, _rid: tmp_path)
    import app.agents.agentic_orchestrator as O
    monkeypatch.setattr(O, "_disk_change_set", lambda _db, _run: SimpleNamespace(operations=[]))
    delta = G._fix_delta_ops(db, run)
    assert [(o.op, o.path) for o in delta] == [("delete", "src/Bad.java")]


def test_fix_delta_emits_modify_for_reverted_parent_modification(db, monkeypatch, tmp_path):
    from app.agents import governance_skills as GS
    parent, _ = _parent(db)
    baseline = {"r1": {"src/A.java": GS.checksum("parent modified content")}}
    run = _gov_run(db, parent, baseline=baseline)
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "A.java").write_text("original base content", encoding="utf-8")  # reverted
    monkeypatch.setattr(G.workspace_local, "repo_dir", lambda _ws, _rid: tmp_path)
    import app.agents.agentic_orchestrator as O
    monkeypatch.setattr(O, "_disk_change_set", lambda _db, _run: SimpleNamespace(operations=[]))
    delta = G._fix_delta_ops(db, run)
    assert [(o.op, o.path) for o in delta] == [("modify", "src/A.java")]
    assert delta[0].content == "original base content"


# ── Batch B: workspace scope (F4) + dead-stage restore (F5) ───────────────────

def test_review_change_set_recloned_uses_parent_base_to_tip_diff(db, monkeypatch, tmp_path):
    # F4: a file EA added is committed on the branch, absent from the parent manifest.
    # The recloned review scope must still include it (parent base → HEAD diff).
    parent, man = _parent(db)
    run = _gov_run(db, parent, source="recloned_branch")
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "SecurityConfig.java").write_text("EA added this", encoding="utf-8")
    (tmp_path / "src" / "A.java").write_text("parent content", encoding="utf-8")
    monkeypatch.setattr(G.workspace_local, "repo_dir", lambda _ws, _rid: tmp_path)

    class _Res:  # fake git diff base..HEAD → parent A.java + EA's SecurityConfig.java
        ok = True
        stdout = "M\tsrc/A.java\nA\tsrc/SecurityConfig.java\n"
    import app.agents.agentic_orchestrator as O
    monkeypatch.setattr(O.adapter, "run_command", lambda *a, **k: _Res())
    monkeypatch.setattr(O, "_disk_change_set", lambda _db, _run: SimpleNamespace(operations=[]))
    cs = G._review_change_set(db, run, man)
    paths = {(o.op, o.path) for o in cs.operations}
    assert ("add", "src/SecurityConfig.java") in paths     # EA's file is in InfoSec's scope
    assert ("modify", "src/A.java") in paths


def test_restore_recreates_deleted_file_and_skips_approved_paths(db, monkeypatch, tmp_path):
    from app.models.agentic import ChangeManifest
    from app.models.base import utcnow
    parent, _ = _parent(db)
    (tmp_path / "src").mkdir(parents=True)
    monkeypatch.setattr(G.workspace_local, "repo_dir", lambda _ws, _rid: tmp_path)
    import app.agents.agentic_orchestrator as O
    monkeypatch.setattr(O, "_ws_id", lambda _run: "ws")

    # Dead attempt 1: deleted Del.java (snapshot has pre-delete content) AND edited A.java.
    dead = AgenticRun(change_request_id=parent.change_request_id, phase="failed", status="failed",
                      kind="gov_ea", selected_repo_ids=["r1"], attempts_json={},
                      parent_run_id=parent.id, workspace_run_id=parent.id,
                      handoff_json={"governance": {"stage": "ea", "parent_run_id": parent.id,
                          "cited_snapshots": {
                              "r1:src/Del.java": {"repo_id": "r1", "content": "keep me"},
                              "r1:src/A.java":   {"repo_id": "r1", "content": "base A"}}}})
    db.add(dead)
    # Retry 2: APPROVED, owns A.java → attempt 1's snapshot must NOT clobber it.
    retry = AgenticRun(change_request_id=parent.change_request_id, phase="completed", status="completed",
                       kind="gov_ea", selected_repo_ids=["r1"], attempts_json={},
                       parent_run_id=parent.id, workspace_run_id=parent.id,
                       handoff_json={"governance": {"stage": "ea", "parent_run_id": parent.id}})
    db.add(retry); db.flush()
    sm = ChangeManifest(run_id=retry.id, manifest_hash="r" * 64, selected_repo_ids=["r1"],
                        per_repo=[{"repo_id": "r1", "base_commit_sha": "b" * 40, "shared_branch_name": "x"}],
                        operations=[{"op": "modify", "repo_id": "r1", "path": "src/A.java",
                                     "content_hash": "h" * 64}],
                        verification={}, review={}, approved_at=utcnow())
    db.add(sm); db.flush()
    # Disk: Del.java is gone (dead stage deleted it); A.java holds retry-2's approved bytes.
    (tmp_path / "src" / "A.java").write_text("approved fix by retry 2", encoding="utf-8")

    restored = G.restore_unapproved_stage_edits(db, parent)
    assert (tmp_path / "src" / "Del.java").read_text() == "keep me"     # recreated (scenario A)
    assert (tmp_path / "src" / "A.java").read_text() == "approved fix by retry 2"  # NOT clobbered (B)
    assert "r1:src/Del.java" in restored and "r1:src/A.java" not in restored


# ── Batch C: durability (F7 orphan reclaim, F8 push reconcile) ────────────────

def test_reconcile_pushed_repos_records_commit_already_on_remote(db, monkeypatch):
    from app.models.agentic import AgenticRunRepo
    from app.models.code_repo import CodeRepo
    parent, _ = _parent(db)
    run = _gov_run(db, parent)
    db.add(CodeRepo(id="r1", label="proj", gitlab_repo="grp/proj", gitlab_url="https://gl", gitlab_branch="main"))
    db.flush()
    man = SimpleNamespace(per_repo=[{"repo_id": "r1", "base_commit_sha": "base000"}],
                          manifest_hash="m" * 64, operations=[{"repo_id": "r1", "path": "x"}])

    # git state: HEAD is our governance commit (parent==base), and ls-remote shows the
    # remote tip == HEAD → the crash happened AFTER the push, before the DB ack.
    def _run_command(rd, argv):
        j = " ".join(argv)
        if "rev-parse HEAD^" in j:  return SimpleNamespace(ok=True, stdout="base000", exit_code=0)
        if "rev-parse HEAD" in j:   return SimpleNamespace(ok=True, stdout="commitC1", exit_code=0)
        if "log -1" in j:           return SimpleNamespace(ok=True, stdout="governance(ea): fix 1 file(s)", exit_code=0)
        if "ls-remote" in j:        return SimpleNamespace(ok=True, stdout="commitC1\trefs/heads/x", exit_code=0)
        return SimpleNamespace(ok=True, stdout="", exit_code=0)
    import app.agents.agentic_orchestrator as O
    monkeypatch.setattr(O.adapter, "run_command", _run_command)
    monkeypatch.setattr(G.workspace_local, "repo_dir", lambda _ws, _rid: __import__("pathlib").Path("/tmp"))
    monkeypatch.setattr(G.workspace_local, "set_remote", lambda *a, **k: True)
    monkeypatch.setattr(G.workspace_local, "build_clone_url", lambda *a, **k: "url")

    reconciled = G._reconcile_pushed_repos(db, run.id, man, "ws", "x")
    assert reconciled == {"r1"}
    rr = db.query(AgenticRunRepo).filter_by(run_id=run.id, repo_id="r1").one()
    assert rr.push_state == "pushed" and rr.pushed_manifest_hash == "m" * 64


def test_reconcile_skips_when_remote_lacks_our_commit(db, monkeypatch):
    from app.models.code_repo import CodeRepo
    parent, _ = _parent(db)
    run = _gov_run(db, parent)
    db.add(CodeRepo(id="r1", label="proj", gitlab_repo="grp/proj", gitlab_url="https://gl", gitlab_branch="main"))
    db.flush()
    man = SimpleNamespace(per_repo=[{"repo_id": "r1", "base_commit_sha": "base000"}],
                          manifest_hash="m" * 64, operations=[])

    def _run_command(rd, argv):
        j = " ".join(argv)
        if "rev-parse HEAD^" in j: return SimpleNamespace(ok=True, stdout="base000", exit_code=0)
        if "rev-parse HEAD" in j:  return SimpleNamespace(ok=True, stdout="commitC1", exit_code=0)
        if "log -1" in j:          return SimpleNamespace(ok=True, stdout="governance(ea): fix", exit_code=0)
        if "ls-remote" in j:       return SimpleNamespace(ok=True, stdout="base000\trefs/heads/x", exit_code=0)  # behind
        return SimpleNamespace(ok=True, stdout="", exit_code=0)
    import app.agents.agentic_orchestrator as O
    monkeypatch.setattr(O.adapter, "run_command", _run_command)
    monkeypatch.setattr(G.workspace_local, "repo_dir", lambda _ws, _rid: __import__("pathlib").Path("/tmp"))
    monkeypatch.setattr(G.workspace_local, "set_remote", lambda *a, **k: True)
    monkeypatch.setattr(G.workspace_local, "build_clone_url", lambda *a, **k: "url")
    assert G._reconcile_pushed_repos(db, run.id, man, "ws", "x") == set()   # not on remote → repush later


# ── Batch E: F11 backfill latest-round, checksum integrity ────────────────────

def test_backfill_marks_fixed_when_latest_round_had_only_nonblocking(db):
    from app.models.agentic import ReviewFinding
    parent, _ = _parent(db)
    run = _gov_run(db, parent)
    db.add_all([
        ReviewFinding(run_id=run.id, round=1, category="security", severity="blocker",
                      file="A.java", why="secret", blocking=True),
        # round 2 fixed it → only a nonblocking PASS row persists that round
        ReviewFinding(run_id=run.id, round=2, category="directive", severity="info",
                      file=None, why="[D1] PASS — resolved at A.java:1", blocking=False),
    ])
    db.flush()
    ledger = G._ledger_from_review_rows(db, run)
    assert len(ledger) == 1 and ledger[0]["status"] == "fixed"   # not frozen 'open' (F11)


def test_load_skill_rejects_checksum_mismatch(db):
    from app.agents import governance_skills as GS
    good = "## RULE X-1: t\nb"
    row = GovernanceSkill(skill_type="ea", version=1, content=good, checksum=GS.checksum(good),
                          rules_json=[{"id": "X-1", "title": "t"}])
    db.add(row); db.flush()
    assert G.load_skill(db, "ea").version == 1                   # matching checksum → ok
    row.content = "## RULE X-1: TAMPERED\nz"                     # out-of-band mutation
    db.flush()
    with pytest.raises(RuntimeError, match="integrity check"):
        G.load_skill(db, "ea")


# ── Fable review of the review-fixes: three defects found in them ─────────────

def test_orphan_query_excludes_gate_parked_runs(db):
    """The F7 orphan reclaim must NOT re-dispatch runs parked at a human gate —
    they are lease-free + idle BY DESIGN (observed live: a parked stage was
    re-driven every sweep, forever) — but MUST reclaim idle, lease-free runs of
    every kind.

    The kind filter was originally `IN ('gov_ea','gov_is')`. That left non-
    governance runs (analysis/full/xsd/code) unrecoverable: lease-free, so
    `recover_runs` (which needs a NON-NULL expired lease) skips them too, so
    nothing could ever resume them. Observed live on 2026-08-25 — undispatched
    `analysis` runs wedged at `pending` forever and held their change's
    one-active-run slot. Non-governance kinds are now IN scope.
    """
    from datetime import timedelta
    from app.models.base import utcnow
    from app.services.celery_tasks import find_orphan_governance_runs
    parent, _ = _parent(db)
    old = utcnow() - timedelta(hours=2)
    rows = {
        "parked":  AgenticRun(change_request_id="chg-1", phase="awaiting_human_approval",
                              status="active", kind="gov_ea", selected_repo_ids=["r1"],
                              attempts_json={}, parent_run_id=parent.id),
        "orphan":  AgenticRun(change_request_id="chg-2", phase="pending",
                              status="active", kind="gov_is", selected_repo_ids=["r1"],
                              attempts_json={}, parent_run_id=parent.id),
        "recent":  AgenticRun(change_request_id="chg-3", phase="pending",
                              status="active", kind="gov_ea", selected_repo_ids=["r1"],
                              attempts_json={}, parent_run_id=parent.id),
        "nongov":  AgenticRun(change_request_id="chg-4", phase="pending",
                              status="active", kind="code", selected_repo_ids=["r1"],
                              attempts_json={}),
        "analysis": AgenticRun(change_request_id="chg-5", phase="pending",
                               status="active", kind="analysis", selected_repo_ids=["r1"],
                               attempts_json={}),
    }
    for r in rows.values():
        db.add(r)
    db.flush()
    for k in ("parked", "orphan", "nongov", "analysis"):
        rows[k].updated_at = old            # after flush so onupdate doesn't overwrite
    db.flush()
    idle_before = utcnow() - timedelta(seconds=300)
    got = {r.id for r in find_orphan_governance_runs(db, idle_before)}
    # Idle + lease-free + driveable, of ANY kind. `parked` (human gate) and
    # `recent` (not yet idle) stay excluded — those exclusions are the point.
    assert got == {rows["orphan"].id, rows["nongov"].id, rows["analysis"].id}


def test_branch_change_ops_falls_back_to_manifest_on_diff_failure(db, monkeypatch, tmp_path):
    """Shallow re-clones (agentic_clone_depth=50 by default) can lack the parent base
    commit — a failed diff must degrade LOUDLY to the parent manifest scope, never to
    a silent EMPTY scope (the F4 failure-class the function exists to prevent)."""
    parent, man = _parent(db)
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "A.java").write_text("on disk", encoding="utf-8")
    monkeypatch.setattr(G.workspace_local, "repo_dir", lambda _ws, _rid: tmp_path)
    import app.agents.agentic_orchestrator as O
    monkeypatch.setattr(O.adapter, "run_command",
                        lambda *a, **k: SimpleNamespace(ok=False, stdout="", exit_code=128))
    ops = G._branch_change_ops("ws", ["r1"], {"r1": "b" * 40}, fallback_man=man)
    assert [(o.op, o.path) for o in ops] == [("modify", "src/A.java")]   # parent manifest scope
    assert ops[0].content == "on disk"


def test_restore_removes_file_created_by_dead_stage(db, monkeypatch, tmp_path):
    """A dead stage that CREATED a cited file leaves orphan bytes no content-snapshot
    can undo — the absent-marker snapshot lets restore remove the creation."""
    parent, _ = _parent(db)
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "New.java").write_text("created by dead fixer", encoding="utf-8")
    monkeypatch.setattr(G.workspace_local, "repo_dir", lambda _ws, _rid: tmp_path)
    import app.agents.agentic_orchestrator as O
    monkeypatch.setattr(O, "_ws_id", lambda _run: "ws")
    dead = AgenticRun(change_request_id=parent.change_request_id, phase="failed", status="failed",
                      kind="gov_ea", selected_repo_ids=["r1"], attempts_json={},
                      parent_run_id=parent.id, workspace_run_id=parent.id,
                      handoff_json={"governance": {"stage": "ea", "parent_run_id": parent.id,
                          "cited_snapshots": {
                              "r1:src/New.java": {"repo_id": "r1", "content": None, "absent": True}}}})
    db.add(dead)
    db.flush()
    restored = G.restore_unapproved_stage_edits(db, parent)
    assert not (tmp_path / "src" / "New.java").exists()
    assert restored == ["r1:src/New.java"]


# ── Validator floor + bundle extras (skill-execution design §6) ───────────────

def _bundle_skill_row(db, monkeypatch, *, smoke="green"):
    import io, tarfile
    from app.agents import governance_skills as GS
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in [("SKILL.md", b"---\nname: is\n---\nprocedure"),
                           ("scripts/scan.py", b"print(1)")]:
            ti = tarfile.TarInfo(name); ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    row = GovernanceSkill(
        skill_type="infosec", version=9, content="---\nname: is\n---\nprocedure",
        checksum=GS.checksum("---\nname: is\n---\nprocedure"),
        bundle_bytes=buf.getvalue(), bundle_filename="b.tar.gz",
        exec_manifest_json={"scripts": [{"path": "scripts/scan.py", "role": "validator",
                                         "invocation": "python3 scripts/scan.py {target}",
                                         "timeout_seconds": 30, "output_format": "json_stdout",
                                         "findings_parse": "stdout.json.total_findings",
                                         "exit_semantics": "", "normalize": [], "network": False}]},
        smoke_status=smoke)
    db.add(row); db.flush()
    return row


def test_validator_floor_runs_even_when_smoke_not_green(db, monkeypatch, tmp_path):
    """Smoke is advisory: the floor still runs its validators when smoke is not
    green (the caller raises the warning banner). It must NOT raise."""
    from types import SimpleNamespace as NS
    parent, _ = _parent(db)
    run = _gov_run(db, parent)
    row = _bundle_skill_row(db, monkeypatch, smoke="pending")
    monkeypatch.setattr(G.workspace_local, "run_dir", lambda _rid: tmp_path / "rd")
    monkeypatch.setattr(G.workspace_local, "repo_dir", lambda _ws, _rid: tmp_path / "repo")
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(G.workspace_local, "changed_files", lambda _ws, _rid: [])
    import app.agents.governance_sandbox as GSB
    monkeypatch.setattr(GSB, "materialize_bundle",
                        lambda r, d: (d.mkdir(parents=True, exist_ok=True) or d))
    monkeypatch.setattr(GSB, "run_script", lambda c, **k: NS(
        ran=True, error=None, exit_code=0, role="validator",
        gate_findings=[{"file": "a.py", "why": "secret", "severity": "blocker"}],
        findings_count=1))
    items, keys = G._run_validator_floor(db, run, row)   # must not raise
    assert len(items) == 1 and keys


def test_validator_floor_items_and_did_not_run_sentinel(db, monkeypatch, tmp_path):
    from types import SimpleNamespace as NS
    parent, _ = _parent(db)
    run = _gov_run(db, parent)
    row = _bundle_skill_row(db, monkeypatch, smoke="green")
    monkeypatch.setattr(G.workspace_local, "run_dir", lambda _rid: tmp_path / "rd")
    monkeypatch.setattr(G.workspace_local, "repo_dir", lambda _ws, _rid: tmp_path / "repo")
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    import app.agents.governance_sandbox as GSB

    def _fake_run(contract, *, bundle_dir, target_dir, scratch_dir=None):
        return NS(ran=True, error=None, exit_code=0, role="validator",
                  gate_findings=[{"file": "a.py", "why": "hardcoded secret", "severity": "blocker"}],
                  findings_count=1)
    monkeypatch.setattr(GSB, "run_script", _fake_run)
    monkeypatch.setattr(GSB, "materialize_bundle", lambda r, d: (d.mkdir(parents=True, exist_ok=True) or d))
    items, keys = G._run_validator_floor(db, run, row)
    assert len(items) == 1 and items[0]["why"].startswith("[validator:scan.py]")
    assert items[0]["severity"] == "blocker" and keys

    def _fake_fail(contract, *, bundle_dir, target_dir, scratch_dir=None):
        return NS(ran=False, error="timed out after 30s", exit_code=None, role="validator",
                  gate_findings=[], findings_count=None)
    monkeypatch.setattr(GSB, "run_script", _fake_fail)
    items2, _ = G._run_validator_floor(db, run, row)
    assert items2 and "DID NOT RUN" in items2[0]["why"] and items2[0]["severity"] == "blocker"


def test_validator_floor_scans_only_changed_files(db, monkeypatch, tmp_path):
    """The floor is a CHANGE gate: validators scan a sparse copy of the changed
    files, never the whole repo — pre-existing debt in untouched files must not
    block the change. Unknown changed-set → fail-closed to the full repo."""
    from types import SimpleNamespace as NS
    parent, _ = _parent(db)
    run = _gov_run(db, parent)
    row = _bundle_skill_row(db, monkeypatch, smoke="green")
    monkeypatch.setattr(G.workspace_local, "run_dir", lambda _rid: tmp_path / "rd")
    monkeypatch.setattr(G.workspace_local, "repo_dir", lambda _ws, _rid: tmp_path / "repo")
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "changed.py").write_text("token = 'x'\n")
    (repo / "debt.py").write_text("password = 'pre-existing'\n")
    monkeypatch.setattr(G.workspace_local, "changed_files",
                        lambda _ws, _rid: [("modify", "src/changed.py"),
                                           ("delete", "src/gone.py")])
    import os

    import app.agents.governance_sandbox as GSB
    seen = {}

    def _fake_run(contract, *, bundle_dir, target_dir, scratch_dir=None):
        seen["files"] = sorted(os.path.relpath(os.path.join(dp, f), target_dir)
                               for dp, _, fs in os.walk(target_dir) for f in fs)
        seen["target"] = str(target_dir)
        return NS(ran=True, error=None, exit_code=0, role="validator",
                  gate_findings=[], findings_count=0)
    monkeypatch.setattr(GSB, "run_script", _fake_run)
    monkeypatch.setattr(GSB, "materialize_bundle",
                        lambda r, d: (d.mkdir(parents=True, exist_ok=True) or d))
    G._run_validator_floor(db, run, row)
    assert seen["files"] == ["src/changed.py"] and seen["target"] != str(repo)

    monkeypatch.setattr(G.workspace_local, "changed_files",
                        lambda _ws, _rid: (_ for _ in ()).throw(RuntimeError("no git")))
    G._run_validator_floor(db, run, row)
    assert seen["target"] == str(repo) and "debt.py" in seen["files"]


def test_bundle_review_extras_only_for_bundles(db, monkeypatch):
    row = _bundle_skill_row(db, monkeypatch)
    note, tools = G._bundle_review_extras(row)
    assert "run_skill_script" in note and tools and tools[0]["name"] == "run_skill_script"
    from types import SimpleNamespace as NS
    md_only = NS(bundle_bytes=None, exec_manifest_json=None)
    assert G._bundle_review_extras(md_only) == ("", [])
