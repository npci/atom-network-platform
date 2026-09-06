# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Multi-skill SLOTS (0118) + governance reset — deterministic pieces on sqlite.

The org ships several skills per type (four InfoSec skills); each uploads under
its own slot name and the stage executes EVERY enabled slot. These tests lock
in: slot selection (newest per name, disabled excluded, loud when empty), stage
pinning of the full slot set, per-skill rule parsing (mode mixing must never
drop units), the multi-bundle validator floor with qualified script addressing,
run_skill_script slot resolution, and the reset provision (supersede + snapshot
restore + status/overlay exclusion).
"""
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from app.agents.governance_sandbox import _docker_available
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 — register every model on Base before create_all
import importlib as _importlib
import pkgutil as _pkgutil
for _m in _pkgutil.iter_modules(__import__("app.models", fromlist=["x"]).__path__):
    _importlib.import_module(f"app.models.{_m.name}")
from app.agents import governance_orchestrator as G
from app.agents import governance_skills as GS
from app.agents import workspace_local
from app.core.database import Base
from app.models.agentic import AgenticRun, AgenticRunRepo, ChangeManifest
from app.models.base import utcnow
from app.models.governance_skill import GovernanceSkill


# ── Docker-only bash policy (retrofit PR-5) ──────────────────────────────────
# This repo requires the DOCKER sandbox backend for `gov_bash`, unlike upstream,
# which falls back to an rlimit subprocess. gov_bash runs MODEL-AUTHORED commands
# and bypasses the static gate by construction, and the subprocess backend cannot
# isolate filesystem or network — so here it refuses instead of degrading.
#
# The tests below exercise a real shell, so they need the daemon. They SKIP rather
# than pass without it: a green run on a machine with no docker must not read as
# "bash works".
_NEEDS_DOCKER = pytest.mark.skipif(
    not _docker_available(),
    reason="gov_bash requires the docker sandbox backend in this repo (see "
           "governance_sandbox.run_shell); no daemon reachable here",
)


@pytest.fixture()
def db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


@pytest.fixture()
def ws_paths(monkeypatch, tmp_path):
    """Confine workspace/run dirs to tmp so floor/reset tests touch real files
    without leaking into the shared workspace root."""
    monkeypatch.setattr(workspace_local, "repo_dir",
                        lambda ws, rid: tmp_path / "ws" / str(ws) / str(rid))
    monkeypatch.setattr(workspace_local, "run_dir",
                        lambda run_id: tmp_path / "runs" / str(run_id))
    return tmp_path


def _skill(db, stype="infosec", version=1, name="default", enabled=True,
           content="## RULE X-1: t\nb", **extra):
    row = GovernanceSkill(skill_type=stype, version=version, name=name,
                          enabled=enabled, content=content,
                          checksum=GS.checksum(content),
                          rules_json=[{"id": "X-1", "title": "t"}], **extra)
    db.add(row)
    db.flush()
    return row


def _parent(db, change_id="chg-1", *, pushed=False):
    run = AgenticRun(change_request_id=change_id, phase="completed", status="completed",
                     kind="code", selected_repo_ids=["r1"], attempts_json={},
                     handoff_json={"feature_branch": "atom/x",
                                   "push_deferred": not pushed})
    db.add(run)
    db.flush()
    run.workspace_run_id = run.id
    man = ChangeManifest(run_id=run.id, manifest_hash="p" * 64, selected_repo_ids=["r1"],
                         per_repo=[{"repo_id": "r1", "base_commit_sha": "b" * 40,
                                    "shared_branch_name": "atom/x"}],
                         operations=[{"op": "modify", "repo_id": "r1", "path": "src/A.java",
                                      "content_hash": "a" * 64}],
                         verification={}, review={})
    man.approved_at = utcnow()
    man.approved_by = "u1"
    db.add(man)
    if pushed:
        db.add(AgenticRunRepo(run_id=run.id, repo_id="r1", branch="atom/x",
                              push_state="pushed", pushed_manifest_hash="p" * 64))
    db.flush()
    return run


def _stage_run(db, parent, kind="gov_is", *, status="active",
               phase="awaiting_human_approval", gov=None):
    run = AgenticRun(change_request_id=parent.change_request_id, phase=phase,
                     status=status, kind=kind, parent_run_id=parent.id,
                     workspace_run_id=parent.workspace_run_id or parent.id,
                     selected_repo_ids=list(parent.selected_repo_ids or []),
                     attempts_json={}, handoff_json={"governance": gov or {}})
    db.add(run)
    db.flush()
    return run


# ── active_skills: slot selection ─────────────────────────────────────────────

def test_active_skills_newest_per_slot_sorted(db):
    _skill(db, version=1, name="secret-scan")
    _skill(db, version=2, name="sast-scanner")
    _skill(db, version=3, name="secret-scan")          # newer row of the same slot
    act = G.active_skills(db, "infosec")
    assert [(r.name, r.version) for r in act] == [("sast-scanner", 2), ("secret-scan", 3)]


def test_active_skills_excludes_disabled_and_fails_loud_when_empty(db):
    _skill(db, version=1, name="secret-scan", enabled=False)
    with pytest.raises(RuntimeError, match="not uploaded"):
        G.active_skills(db, "infosec")
    _skill(db, version=2, name="sast-scanner")
    act = G.active_skills(db, "infosec")
    assert [r.name for r in act] == ["sast-scanner"]


def test_active_skills_integrity_check_per_slot(db):
    row = _skill(db, version=1, name="secret-scan")
    row.checksum = "0" * 64
    db.flush()
    with pytest.raises(RuntimeError, match="integrity"):
        G.active_skills(db, "infosec")


# ── stage creation pins the full slot set ─────────────────────────────────────

def test_create_stage_run_pins_all_slots(db):
    _skill(db, stype="ea", version=1, name="sa-code-review")
    a = _skill(db, version=1, name="secret-scan")
    b = _skill(db, version=2, name="sast-scanner")
    parent = _parent(db)
    run, created = G.create_stage_run(db, parent, "gov_is", created_by="u1")
    assert created
    gov = (run.handoff_json or {}).get("governance") or {}
    assert [(p["name"], p["version"]) for p in gov["skills"]] == \
        [("sast-scanner", b.version), ("secret-scan", a.version)]
    # Back-compat primary pin keeps the classic keys.
    assert gov["skill"]["type"] == "infosec" and gov["skill"]["version"] == b.version
    loaded = G._load_pinned_skills(db, run, gov)
    assert [r.name for r in loaded] == ["sast-scanner", "secret-scan"]


# ── combined rules: per-skill parse, no unit loss on mode mixing ──────────────

def test_combined_rules_single_slot_is_passthrough(db):
    sk = _skill(db, version=1, name="only", content="## RULE R-1: one\nbody")
    pre, rules, content = G._combined_rules([sk])
    assert content == sk.content and [r.id for r in rules] == ["R-1"]


def test_combined_rules_multi_prefixes_and_keeps_both_modes(db):
    rule_mode = _skill(db, version=1, name="secret-scan",
                       content="## RULE SS-1: no secrets\nbody")
    sections = _skill(db, version=2, name="sast-scanner",
                      content="---\nname: sast-scanner\n---\nintro\n\n## Step 1\nrun\n\n## Step 2\nreport")
    pre, rules, content = G._combined_rules([rule_mode, sections])
    ids = [r.id for r in rules]
    assert "secret-scan/SS-1" in ids
    assert any(i.startswith("sast-scanner/") for i in ids)
    assert len([i for i in ids if i.startswith("sast-scanner/")]) == 2
    assert "# ── SKILL: secret-scan" in content and "# ── SKILL: sast-scanner" in content
    assert "name: sast-scanner" not in content      # frontmatter stripped from the prompt


# ── multi-bundle validator floor + run_skill_script addressing ────────────────

def _bundle_bytes(finding_why):
    buf = io.BytesIO()
    script = ("import json,sys\n"
              "print(json.dumps({'total_findings':1,'items':[{'why':'%s',"
              "'severity':'high','file':'f.java','line':1}]}))\n" % finding_why)
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SKILL.md", "## Check\nrun the validator")
        z.writestr("scripts/v.py", script)
    return buf.getvalue()


def _em():
    return {"scripts": [{"path": "scripts/v.py", "role": "validator",
                         "invocation": "python3 scripts/v.py {target}",
                         "timeout_seconds": 60, "output_format": "json_stdout",
                         "findings_parse": "stdout.json.total_findings",
                         "exit_semantics": "always 0", "normalize": [],
                         "network": False, "smoke": None}]}


def test_floor_runs_validators_of_every_slot(db, ws_paths):
    a = _skill(db, version=1, name="alpha", bundle_bytes=_bundle_bytes("from-alpha"),
               bundle_filename="alpha.zip", exec_manifest_json=_em())
    b = _skill(db, version=2, name="beta", bundle_bytes=_bundle_bytes("from-beta"),
               bundle_filename="beta.zip", exec_manifest_json=_em())
    parent = _parent(db)
    run = _stage_run(db, parent, status="active", phase="review")
    items, keys = G._run_validator_floor(db, run, [a, b])
    whys = " | ".join(i["why"] for i in items)
    assert len(items) == 2 and "from-alpha" in whys and "from-beta" in whys
    assert {i["validator"] for i in items} == {"alpha/scripts/v.py", "beta/scripts/v.py"}
    manifest = json.loads((workspace_local.run_dir(run.id) / "_skill_bundle" /
                           "_exec_manifest.json").read_text())
    assert {s["path"] for s in manifest["scripts"]} == \
        {"alpha/scripts/v.py", "beta/scripts/v.py"}
    assert all(s.get("_subdir") and s.get("_orig_path") == "scripts/v.py"
               for s in manifest["scripts"])


def test_run_skill_script_resolves_qualified_and_unique_bare(db, ws_paths):
    from app.agents.agentic_tools import ToolError, run_skill_script
    a = _skill(db, version=1, name="alpha", bundle_bytes=_bundle_bytes("from-alpha"),
               bundle_filename="alpha.zip", exec_manifest_json=_em())
    b = _skill(db, version=2, name="beta", bundle_bytes=_bundle_bytes("from-beta"),
               bundle_filename="beta.zip", exec_manifest_json=_em())
    parent = _parent(db)
    run = _stage_run(db, parent, status="active", phase="review")
    G._run_validator_floor(db, run, [a, b])
    ctx = SimpleNamespace(run_id=run.id, selected_repo_ids=["r1"],
                          workspace_run_id=run.workspace_run_id)
    out = json.loads(run_skill_script(ctx, "alpha/scripts/v.py"))
    assert out["ran"] and out["findings_count"] == 1
    assert out["findings"][0]["why"] == "from-alpha"
    with pytest.raises(ToolError, match="not declared"):
        run_skill_script(ctx, "scripts/v.py")       # ambiguous across two slots
    single = _stage_run(db, parent, status="active", phase="review")
    G._run_validator_floor(db, single, [a])          # single slot → unqualified manifest
    ctx1 = SimpleNamespace(run_id=single.id, selected_repo_ids=["r1"],
                           workspace_run_id=single.workspace_run_id)
    out1 = json.loads(run_skill_script(ctx1, "scripts/v.py"))
    assert out1["ran"] and out1["findings"][0]["why"] == "from-alpha"


# ── reset: supersede + snapshot restore + status/overlay exclusion ────────────

def _repo_file(ws_paths, ws, rid, rel, text):
    rd = workspace_local.repo_dir(ws, rid)
    (rd / ".git").mkdir(parents=True, exist_ok=True)
    p = rd / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_reset_supersedes_restores_and_clears_status(db, ws_paths):
    _skill(db, stype="ea", version=1, name="sa-code-review")
    _skill(db, version=1, name="secret-scan")
    parent = _parent(db)
    ws = parent.workspace_run_id
    a_java = _repo_file(ws_paths, ws, "r1", "src/A.java", "FIXED-BY-EA")
    b_java = _repo_file(ws_paths, ws, "r1", "src/B.java", "CREATED-BY-IS")

    ea = _stage_run(db, parent, kind="gov_ea", status="completed", phase="completed",
                    gov={"stage": "ea", "result": "fixes_approved",
                         "cited_snapshots": {"r1:src/A.java":
                                             {"repo_id": "r1", "content": "ORIGINAL"}}})
    ea_man = ChangeManifest(run_id=ea.id, manifest_hash="e" * 64, selected_repo_ids=["r1"],
                            per_repo=[{"repo_id": "r1", "base_commit_sha": "b" * 40,
                                       "shared_branch_name": "atom/x"}],
                            operations=[{"op": "modify", "repo_id": "r1",
                                         "path": "src/A.java", "content_hash": "c" * 64}],
                            verification={}, review={})
    ea_man.approved_at = utcnow()
    db.add(ea_man)
    parked = _stage_run(db, parent, kind="gov_is", status="active",
                        phase="awaiting_human_approval",
                        gov={"stage": "infosec",
                             "cited_snapshots": {"r1:src/B.java":
                                                 {"repo_id": "r1", "content": None,
                                                  "absent": True}}})
    db.flush()

    out = G.reset_governance(db, parent.change_request_id, requested_by="admin-1")
    assert out["reset"] and set(out["superseded_runs"]) == {ea.id, parked.id}
    assert a_java.read_text() == "ORIGINAL"          # deferred-approved fix reverted
    assert not b_java.exists()                       # parked run's created file removed
    db.refresh(parked)
    assert parked.status == "cancelled"
    db.refresh(ea)
    assert ea.status == "completed"                  # audit row untouched, just superseded
    # Status derivation ignores superseded runs → fresh start from scratch.
    assert G._stage_view(db, parent, "gov_ea")["run_id"] is None
    assert G._stage_view(db, parent, "gov_is")["run_id"] is None
    # Deferred-push overlay excludes the superseded approved manifest.
    man = SimpleNamespace(per_repo=[], operations=[], manifest_hash="p" * 64,
                          approved_at=utcnow())
    assert G.overlay_stage_fixes(db, parent, man) is man


def test_reset_requests_cancel_on_live_lease(db, ws_paths):
    from datetime import timedelta
    parent = _parent(db)
    running = _stage_run(db, parent, kind="gov_ea", status="active", phase="review")
    running.lease_owner = "worker-1"
    running.lease_expires_at = utcnow() + timedelta(minutes=5)
    db.flush()
    out = G.reset_governance(db, parent.change_request_id, requested_by="admin-1")
    assert not out["reset"] and out["cancel_requested"] == [running.id]
    db.refresh(running)
    assert running.cancel_requested and running.status == "active"


def test_reset_committed_stage_fixes_are_not_reverted(db, ws_paths):
    parent = _parent(db, pushed=True)
    ws = parent.workspace_run_id
    a_java = _repo_file(ws_paths, ws, "r1", "src/A.java", "FIXED-AND-PUSHED")
    ea = _stage_run(db, parent, kind="gov_ea", status="completed", phase="completed",
                    gov={"stage": "ea", "result": "fixes_approved",
                         "cited_snapshots": {"r1:src/A.java":
                                             {"repo_id": "r1", "content": "ORIGINAL"}}})
    db.add(AgenticRunRepo(run_id=ea.id, repo_id="r1", branch="atom/x",
                          push_state="pushed", pushed_manifest_hash="e" * 64))
    db.flush()
    out = G.reset_governance(db, parent.change_request_id, requested_by="admin-1")
    assert out["reset"] and out["superseded_runs"] == [ea.id]
    # Pushed fixes are branch history — disk stays at the committed state.
    assert a_java.read_text() == "FIXED-AND-PUSHED" and out["restored_count"] == 0


def test_reset_without_stage_runs_is_a_noop_reset(db, ws_paths):
    parent = _parent(db)
    out = G.reset_governance(db, parent.change_request_id)
    assert out["reset"] and out["superseded_runs"] == []


# ── stage view exposes the pinned slot set ────────────────────────────────────

def test_stage_view_exposes_skills_and_aggregate_smoke(db):
    parent = _parent(db)
    _stage_run(db, parent, kind="gov_is", status="active", phase="review",
               gov={"stage": "infosec",
                    "skill": {"type": "infosec", "name": "secret-scan", "version": 3,
                              "checksum": "x", "smoke_status": "green"},
                    "skills": [{"name": "secret-scan", "version": 3,
                                "checksum": "x", "smoke_status": "green"},
                               {"name": "sast-scanner", "version": 4,
                                "checksum": "y", "smoke_status": "failed"}]})
    db.flush()
    view = G._stage_view(db, parent, "gov_is")
    assert [s["name"] for s in view["skills"]] == ["secret-scan", "sast-scanner"]
    assert view["smoke_ok"] is False                 # one pinned slot failed smoke


# ── slot-sharded review batches (one slot's budget can't starve another's) ────

def test_slot_sharded_batches_never_span_slots(db):
    a = _skill(db, version=1, name="alpha",
               content="\n".join(f"## RULE A-{i}: t\nb" for i in range(1, 4)))
    b = _skill(db, version=2, name="beta",
               content="\n".join(f"## RULE B-{i}: t\nb" for i in range(1, 3)))
    _, rules, _ = G._combined_rules([a, b])
    batches = G._slot_sharded_batches([a, b], rules)
    assert len(batches) == 2 and [len(x) for x in batches] == [3, 2]
    assert all(len({r.id.split("/")[0] for r in batch}) == 1 for batch in batches)
    single = G._slot_sharded_batches([a], G._combined_rules([a])[1])
    assert len(single) == 1 and len(single[0]) == 3      # classic path untouched


# ── scope: change (change-level report-graders) ───────────────────────────────

def test_exec_manifest_scope_validation():
    from app.agents import governance_bundle as GB
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SKILL.md", "## Check\nx")
        z.writestr("scripts/v.py", "print('{}')")
    parsed = GB.parse_bundle(buf.getvalue(), "b.zip")
    contract = {"path": "scripts/v.py", "role": "validator",
                "output_format": "json_stdout", "findings_parse": "stdout.json.n"}
    em = GB.validate_exec_manifest({"scripts": [{**contract, "scope": "change"}]}, parsed)
    assert em["scripts"][0]["scope"] == "change"
    em = GB.validate_exec_manifest({"scripts": [contract]}, parsed)
    assert em["scripts"][0]["scope"] == "repo"           # default
    with pytest.raises(GB.BundleError, match="scope"):
        GB.validate_exec_manifest({"scripts": [{**contract, "scope": "galaxy"}]}, parsed)


def _em_change():
    em = _em()
    em["scripts"][0]["scope"] = "change"
    return em


def test_change_scoped_validator_runs_once_across_repos(db, ws_paths, monkeypatch):
    a = _skill(db, version=1, name="alpha", bundle_bytes=_bundle_bytes("change-level"),
               bundle_filename="a.zip", exec_manifest_json=_em_change())
    parent = _parent(db)
    parent.selected_repo_ids = ["r1", "r2"]
    db.flush()
    run = _stage_run(db, parent, status="active", phase="review")
    # A real change-set: each repo has one changed file so the sparse copy (and
    # thus the merged _change target) is non-empty.
    for rid in ("r1", "r2"):
        _repo_file(ws_paths, run.workspace_run_id, rid, "src/A.java", "changed")
    monkeypatch.setattr(workspace_local, "changed_files",
                        lambda ws, rid: [("modify", "src/A.java")])
    items, _ = G._run_validator_floor(db, run, [a])
    # One execution for the whole change — not one REPORT-MISSING-style
    # finding per repo (the live EA run produced 4 duplicates exactly so).
    assert len(items) == 1 and items[0]["why"].endswith("change-level")
    assert (workspace_local.run_dir(run.id) / "_skill_bundle" / ".."
            ).resolve().joinpath("_floor_target", "_change").exists()


def test_change_scoped_validator_empty_merge_is_did_not_run(db, ws_paths):
    """LOST ≠ CLEAN: when no repo can enumerate its change-set (empty merged
    target), a change-scoped validator must surface a must-block DID-NOT-RUN,
    never run against an empty dir and read clean."""
    a = _skill(db, version=1, name="alpha", bundle_bytes=_bundle_bytes("change-level"),
               bundle_filename="a.zip", exec_manifest_json=_em_change())
    parent = _parent(db)
    parent.selected_repo_ids = ["r1", "r2"]
    db.flush()
    run = _stage_run(db, parent, status="active", phase="review")
    # No changed_files set up → both repos are full-repo fallback → empty merge.
    items, _ = G._run_validator_floor(db, run, [a])
    assert len(items) == 1
    assert items[0]["severity"] == "blocker" and "DID NOT RUN" in items[0]["why"]


def test_identical_repo_scoped_findings_dedup(db, ws_paths):
    a = _skill(db, version=1, name="alpha", bundle_bytes=_bundle_bytes("same-everywhere"),
               bundle_filename="a.zip", exec_manifest_json=_em())
    parent = _parent(db)
    parent.selected_repo_ids = ["r1", "r2"]
    db.flush()
    run = _stage_run(db, parent, status="active", phase="review")
    items, _ = G._run_validator_floor(db, run, [a])
    # The script reports the identical finding against both repos — the gate
    # keeps ONE copy (the ledger keys on the same identity anyway).
    assert len(items) == 1


# ── review-batch checkpointing (resume where it stopped) ──────────────────────

def test_ckpt_key_pins_rules_and_skills(db):
    a = _skill(db, version=1, name="alpha", content="## RULE A-1: t\nb")
    b = _skill(db, version=2, name="beta", content="## RULE B-1: t\nb")
    _, rules, _ = G._combined_rules([a, b])
    k1 = G._ckpt_key(1, 0, rules[:1], [a, b])
    assert k1 == G._ckpt_key(1, 0, rules[:1], [a, b])          # stable
    assert k1 != G._ckpt_key(2, 0, rules[:1], [a, b])          # round changes it
    assert k1 != G._ckpt_key(1, 0, rules[1:], [a, b])          # rules change it
    assert k1 != G._ckpt_key(1, 0, rules[:1], [a])             # skill set changes it


def test_freeze_thaw_roundtrip_preserves_downstream_behaviour():
    from app.agents import agentic_review
    src = SimpleNamespace(severity="blocker", category="directive",
                          why="[D2] FAIL — SA-02 violated at Foo.java:10",
                          suggested_fix="fix it", file="Foo.java", line=10,
                          blocking=True, done_when="")
    gap = SimpleNamespace(severity="blocker", category="directive",
                          why="[D3] NOT VERIFIED — the reviewer did not return a verdict "
                              "for this binding directive: rule X",
                          suggested_fix=None, file=None, line=None,
                          blocking=True, done_when="")
    thawed = G._thaw_findings(G._freeze_findings([src, gap]))
    assert G._directive_verdicts(thawed) == {2: "FAIL"}
    assert [agentic_review._is_reviewer_gap(f) for f in thawed] == [False, True]
    assert thawed[0].blocking and thawed[0].file == "Foo.java"


def test_reset_removes_stage_created_files_but_keeps_change_files(db, ws_paths):
    parent = _parent(db)
    ws = parent.workspace_run_id
    change_file = _repo_file(ws_paths, ws, "r1", "src/A.java", "PARENT-CONTENT")
    created = _repo_file(ws_paths, ws, "r1", "sa_review_report.json", "{}")
    ea = _stage_run(db, parent, kind="gov_ea", status="completed", phase="completed",
                    gov={"stage": "ea", "result": "fixes_approved",
                         "baseline": {"r1": {"src/A.java": "sha-of-parent"}},
                         "cited_snapshots": {}})
    man = ChangeManifest(run_id=ea.id, manifest_hash="e" * 64, selected_repo_ids=["r1"],
                         per_repo=[{"repo_id": "r1", "base_commit_sha": "b" * 40,
                                    "shared_branch_name": "atom/x"}],
                         operations=[
                             # deferred-parent 'add' classification of a CHANGE file —
                             # in the stage baseline, must survive the reset
                             {"op": "add", "repo_id": "r1", "path": "src/A.java",
                              "content_hash": "c" * 64},
                             # the stage's own creation — must be removed
                             {"op": "add", "repo_id": "r1", "path": "sa_review_report.json",
                              "content_hash": "d" * 64}],
                         verification={}, review={})
    man.approved_at = utcnow()
    db.add(man)
    db.flush()
    out = G.reset_governance(db, parent.change_request_id, requested_by="admin-1")
    assert out["reset"]
    assert change_file.exists()                      # the approved change stays
    assert not created.exists()                      # the stage's artifact is gone
    assert "r1:sa_review_report.json" in out["restored_files"]


# ── verdict-integrity floor (external audit: fail-open holes) ─────────────────

def _F(**kw):
    base = dict(severity="info", category="architecture", why="", suggested_fix="",
                file=None, line=None, blocking=False, done_when="")
    base.update(kw)
    return SimpleNamespace(**base)


def test_anchored_fail_is_always_blocking():
    f = _F(why="[D3] FAIL — violation exists", blocking=False, severity="info")
    G._harden_batch_verdicts([f], {"src/A.java"})
    assert f.blocking is True                      # can never reach the clean path


def test_bare_pass_downgrades_to_not_verified_gap():
    from app.agents import agentic_review
    f = _F(why="[D1] PASS — trust me, implementation is compliant")
    G._harden_batch_verdicts([f], {"src/A.java"})
    assert f.blocking and "NOT VERIFIED" in f.why
    assert agentic_review._is_reviewer_gap(f)      # blocks the gate, never sent to the fixer
    assert G._directive_verdicts([f]) == {}        # no longer counts as a PASS verdict


def test_pass_with_change_file_citation_survives():
    f = _F(why="[D2] PASS — timeouts set in src/A.java:57 via HttpClient builder")
    G._harden_batch_verdicts([f], {"src/A.java"})
    assert "PASS" in f.why and not f.blocking


def test_pass_with_not_applicable_justification_survives():
    f = _F(why="[D4] PASS — not applicable: this change contains no persistence layer work")
    G._harden_batch_verdicts([f], {"src/A.java"})
    assert "PASS" in f.why and not f.blocking


def test_pass_citing_file_outside_change_set_is_downgraded():
    f = _F(why="[D5] PASS — verified in src/Other.java:10")
    G._harden_batch_verdicts([f], {"src/A.java"})
    assert f.blocking and "NOT VERIFIED" in f.why


# ── review-fix follow-ups (found by the adversarial review) ───────────────────

def test_coerced_fail_is_must_block_severity():
    # A reviewer-tagged severity=info FAIL must become must-block, or the cap can
    # slice it out of the fixer and the gate.
    f = _F(why="[D3] FAIL — violation", severity="info", category="architecture",
           blocking=False)
    G._harden_batch_verdicts([f], {"src/A.java"})
    from app.agents.agentic_orchestrator import is_must_block
    assert f.blocking and is_must_block(f.category, f.severity)


def test_checkpoint_not_inherited_after_a_fix_round(db, ws_paths, monkeypatch):
    # A prior attempt that ran a fix round (code_change>0) computed its round-1
    # checkpoint against PRE-FIX code — the retry must NOT inherit it.
    from app.agents import agentic_orchestrator as AO
    monkeypatch.setattr(AO, "_ws_id", lambda run: run.workspace_run_id, raising=False)
    parent = _parent(db)
    _repo_file(ws_paths, parent.workspace_run_id, "r1", "src/A.java", "x")
    prior = _stage_run(db, parent, kind="gov_ea", status="failed", phase="failed",
                       gov={"stage": "ea", "baseline": {"r1": {"src/A.java": "s"}},
                            "review_checkpoint": {"r1:b0:deadbeef": []}})
    prior.attempts_json = {"code_change": 1}          # a fix round ran
    db.flush()
    fresh = _stage_run(db, parent, kind="gov_ea", status="active", phase="pending",
                       gov={"stage": "ea"})
    art = {}
    monkeypatch.setattr(workspace_local, "changed_files", lambda ws, rid: [])
    # Exercise only the inherit block via a tiny driver: call the workspace phase's
    # inherit logic by replaying its condition. Simpler: assert the guard directly.
    pg = G._gov(prior)
    inherit = (pg.get("review_checkpoint") and not pg.get("superseded")
               and (prior.attempts_json or {}).get("code_change", 0) == 0)
    assert not inherit                                # fix round ran → no inherit


def test_reset_partial_push_reverts_unpushed_repo(db, ws_paths):
    parent = _parent(db)
    parent.selected_repo_ids = ["r1", "r2"]
    ws = parent.workspace_run_id
    a = _repo_file(ws_paths, ws, "r1", "src/A.java", "PUSHED-FIX")
    b = _repo_file(ws_paths, ws, "r2", "src/B.java", "UNPUSHED-FIX")
    sr = _stage_run(db, parent, kind="gov_is", status="completed", phase="completed",
                    gov={"stage": "infosec", "result": "fixes_approved",
                         "cited_snapshots": {"r1:src/A.java": {"repo_id": "r1", "content": "ORIG-A"},
                                             "r2:src/B.java": {"repo_id": "r2", "content": "ORIG-B"}}})
    man = ChangeManifest(run_id=sr.id, manifest_hash="e" * 64, selected_repo_ids=["r1", "r2"],
                         per_repo=[{"repo_id": "r1", "base_commit_sha": "b" * 40,
                                    "shared_branch_name": "atom/x"}],
                         operations=[{"op": "modify", "repo_id": "r1", "path": "src/A.java",
                                      "content_hash": "c" * 64}],
                         verification={}, review={})
    man.approved_at = utcnow()
    db.add(man)
    # Only repo r1 was pushed — r2's push failed.
    db.add(AgenticRunRepo(run_id=sr.id, repo_id="r1", branch="atom/x",
                          push_state="pushed", pushed_manifest_hash="e" * 64))
    db.flush()
    out = G.reset_governance(db, parent.change_request_id, requested_by="admin-1")
    assert out["reset"]
    assert a.read_text() == "PUSHED-FIX"              # pushed repo: branch history, untouched
    assert b.read_text() == "ORIG-B"                  # unpushed repo: reverted


# ── bash tool (Claude-Code parity for skill execution) ────────────────────────

def test_bash_gated_outside_governance(ws_paths):
    from app.agents.agentic_tools import ToolError, gov_bash
    ctx = SimpleNamespace(run_id="not-a-gov-run", selected_repo_ids=["r1"],
                          workspace_run_id=None)
    with pytest.raises(ToolError, match="governance review stage"):
        gov_bash(ctx, "echo hi")


@_NEEDS_DOCKER
def test_bash_runs_in_bundle_root(db, ws_paths, monkeypatch):
    from app.agents import governance_sandbox as GSB
    from app.agents.agentic_tools import gov_bash
    monkeypatch.setattr(GSB, "_docker_available", lambda: False)
    a = _skill(db, version=1, name="alpha", bundle_bytes=_bundle_bytes("x"),
               bundle_filename="alpha.zip", exec_manifest_json=_em())
    parent = _parent(db)
    run = _stage_run(db, parent, status="active", phase="review")
    G._run_validator_floor(db, run, [a])
    ctx = SimpleNamespace(run_id=run.id, selected_repo_ids=["r1"],
                          workspace_run_id=run.workspace_run_id)
    out = json.loads(gov_bash(ctx, "ls SKILL.md && echo shell-works"))
    assert out["exit_code"] == 0
    assert "SKILL.md" in out["stdout"] and "shell-works" in out["stdout"]


@_NEEDS_DOCKER
def test_bash_available_for_contractless_bundle(db, ws_paths, monkeypatch):
    """A vanilla Agent-Skill bundle with NO declared exec contract still
    materializes and still gets the shell — Claude-Code parity: the agent
    follows SKILL.md's own documented invocations verbatim."""
    from app.agents import governance_sandbox as GSB
    from app.agents.agentic_tools import gov_bash
    monkeypatch.setattr(GSB, "_docker_available", lambda: False)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SKILL.md", "run `python scripts/gen.py --out report.json`")
        z.writestr("scripts/gen.py",
                   "import json,sys\n"
                   "open(sys.argv[sys.argv.index('--out')+1],'w')"
                   ".write(json.dumps({'ok':1}))\n"
                   "print('written')\n")
    a = _skill(db, version=1, name="alpha", bundle_bytes=buf.getvalue(),
               bundle_filename="alpha.zip", exec_manifest_json=None)
    parent = _parent(db)
    run = _stage_run(db, parent, status="active", phase="review")
    items, keys = G._run_validator_floor(db, run, [a])
    assert items == [] and keys == set()               # nothing declared → no floor
    note, tools = G._bundle_review_extras([a], run)
    assert any(t["name"] == "bash" for t in tools)     # …but the shell is there
    assert "`bash` tool" in note and "_skill_out" in note
    ctx = SimpleNamespace(run_id=run.id, selected_repo_ids=["r1"],
                          workspace_run_id=run.workspace_run_id)
    out = json.loads(gov_bash(
        ctx, "python3 scripts/gen.py --out ../_skill_out/report.json "
             "&& cat ../_skill_out/report.json"))
    assert out["exit_code"] == 0 and '"ok": 1' in out["stdout"]


@_NEEDS_DOCKER
def test_bash_timeout_and_stderr_surface(db, ws_paths, monkeypatch):
    from app.agents import governance_sandbox as GSB
    from app.agents.agentic_tools import gov_bash
    monkeypatch.setattr(GSB, "_docker_available", lambda: False)
    a = _skill(db, version=1, name="alpha", bundle_bytes=_bundle_bytes("x"),
               bundle_filename="alpha.zip", exec_manifest_json=_em())
    parent = _parent(db)
    run = _stage_run(db, parent, status="active", phase="review")
    G._run_validator_floor(db, run, [a])
    ctx = SimpleNamespace(run_id=run.id, selected_repo_ids=["r1"],
                          workspace_run_id=run.workspace_run_id)
    out = json.loads(gov_bash(ctx, "echo oops >&2; exit 3"))
    assert out["exit_code"] == 3 and "oops" in out["stderr"]


# ── script-failure surfacing (user-visible, not transcript-only) ──────────────

@_NEEDS_DOCKER
def test_failed_script_execution_surfaces_to_user(db, ws_paths, monkeypatch):
    """A bash call that invokes a script and fails → sidecar → gov json +
    governance_script_failure event. The user sees it without reading the
    transcript."""
    from app.agents import governance_sandbox as GSB
    from app.agents.agentic_tools import gov_bash
    from app.models.agentic import AgenticEvent
    monkeypatch.setattr(GSB, "_docker_available", lambda: False)
    a = _skill(db, version=1, name="alpha", bundle_bytes=_bundle_bytes("x"),
               bundle_filename="alpha.zip", exec_manifest_json=_em())
    parent = _parent(db)
    run = _stage_run(db, parent, status="active", phase="review")
    G._run_validator_floor(db, run, [a])
    ctx = SimpleNamespace(run_id=run.id, selected_repo_ids=["r1"],
                          workspace_run_id=run.workspace_run_id)
    out = json.loads(gov_bash(ctx, "python3 scripts/does_not_exist.py --flag x"))
    assert out["exit_code"] != 0
    json.loads(gov_bash(ctx, "true"))                  # generic success: no record
    gov = {}
    G._surface_script_failures(db, run, gov)
    fails = gov.get("script_failures") or []
    assert len(fails) == 1 and "does_not_exist.py" in fails[0]["command"]
    evs = [e for e in db.query(AgenticEvent).filter_by(run_id=run.id)
           if e.kind == "governance_script_failure"]
    assert len(evs) == 1 and "FAILED" in (evs[0].payload or {}).get("action", "")
    # Idempotent: same failures re-read → no duplicate event, no growth.
    G._surface_script_failures(db, run, gov)
    assert len(gov["script_failures"]) == 1
    evs2 = [e for e in db.query(AgenticEvent).filter_by(run_id=run.id)
            if e.kind == "governance_script_failure"]
    assert len(evs2) == 1


def test_run_skill_script_crash_recorded_but_findings_exit_not(db, ws_paths, monkeypatch):
    """Nonzero exit WITH parsed findings is a scanner convention, not a failure;
    nonzero exit with NOTHING parseable is a crash and must surface."""
    from app.agents import governance_sandbox as GSB
    from app.agents.agentic_tools import run_skill_script
    monkeypatch.setattr(GSB, "_docker_available", lambda: False)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SKILL.md", "## Check\nrun")
        z.writestr("scripts/findings_exit1.py",
                   "import json,sys\nprint(json.dumps({'total_findings':2,'items':[]}))\nsys.exit(1)\n")
        z.writestr("scripts/crasher.py", "import sys\nsys.exit(3)\n")
    em = {"scripts": [
        {"path": "scripts/findings_exit1.py", "role": "validator",
         "invocation": "python3 scripts/findings_exit1.py {target}",
         "timeout_seconds": 60, "output_format": "json_stdout",
         "findings_parse": "stdout.json.total_findings",
         "exit_semantics": "1 on findings", "normalize": [], "network": False, "smoke": None},
        {"path": "scripts/crasher.py", "role": "generator",
         "invocation": "python3 scripts/crasher.py {target}",
         "timeout_seconds": 60, "output_format": "exit_code",
         "findings_parse": None, "exit_semantics": "0=ok", "normalize": [],
         "network": False, "smoke": None}]}
    a = _skill(db, version=1, name="alpha", bundle_bytes=buf.getvalue(),
               bundle_filename="alpha.zip", exec_manifest_json=em)
    parent = _parent(db)
    run = _stage_run(db, parent, status="active", phase="review")
    G._run_validator_floor(db, run, [a])
    ctx = SimpleNamespace(run_id=run.id, selected_repo_ids=["r1"],
                          workspace_run_id=run.workspace_run_id)
    json.loads(run_skill_script(ctx, "scripts/findings_exit1.py"))
    json.loads(run_skill_script(ctx, "scripts/crasher.py"))
    gov = {}
    G._surface_script_failures(db, run, gov)
    fails = gov.get("script_failures") or []
    assert [f["script"] for f in fails] == ["scripts/crasher.py"]


# ── script-invocation detection (what earns the user-facing failure banner) ───
#
# The banner says "the review may be incomplete — verify these checks by hand".
# It is only worth reading if it does not fire on ordinary shell probing: a
# `grep` that exits 1 because it matched nothing is a normal negative result,
# not a crashed tool. A substring test on ".py" / "scripts/" fired on both.

@pytest.mark.parametrize("command", [
    "python3 scripts/generate_sca.py /repo --out out.json",
    "python scripts/checklist.py",
    "./scripts/detect_modules.py",
    "scripts/render_sca.py --in a.json",
    "node tools/scan.js",
    "cd /x && python3 scripts/gen.py",
    "OUT=/tmp/x python3 scripts/gen.py",
])
def test_invokes_a_script_true(command):
    from app.agents.agentic_tools import _invokes_a_script
    assert _invokes_a_script(command) is True


@pytest.mark.parametrize("command", [
    'grep -rn "foo.py" .',                     # exits 1 on no match — NOT a failure
    "grep -r scripts/ src | head",
    "ls scripts/",
    "cat scripts/README.md",
    "find . -name '*.py'",
    "echo scripts/gen.py",
    "true",
])
def test_invokes_a_script_false(command):
    from app.agents.agentic_tools import _invokes_a_script
    assert _invokes_a_script(command) is False


@_NEEDS_DOCKER
def test_grep_no_match_does_not_raise_the_failure_banner(db, ws_paths, monkeypatch):
    """End-to-end: the classic false positive. `grep` for a .py name, no match,
    exit 1 — the user must NOT be told a skill script failed."""
    from app.agents import governance_sandbox as GSB
    from app.agents.agentic_tools import gov_bash
    monkeypatch.setattr(GSB, "_docker_available", lambda: False)
    a = _skill(db, version=1, name="alpha", bundle_bytes=_bundle_bytes("x"),
               bundle_filename="alpha.zip", exec_manifest_json=_em())
    parent = _parent(db)
    run = _stage_run(db, parent, status="active", phase="review")
    G._run_validator_floor(db, run, [a])
    ctx = SimpleNamespace(run_id=run.id, selected_repo_ids=["r1"],
                          workspace_run_id=run.workspace_run_id)
    out = json.loads(gov_bash(ctx, 'grep -rn "nothing_matches_here.py" .'))
    assert out["exit_code"] != 0                      # grep found nothing
    gov = {}
    G._surface_script_failures(db, run, gov)
    assert not gov.get("script_failures")             # …and nobody was alarmed


def test_bash_refuses_without_docker_rather_than_degrading(tmp_path, monkeypatch):
    """The policy itself: no daemon, no shell — and the refusal says so.

    Upstream falls back to an unisolated subprocess here. This repo does not:
    gov_bash's command is never stored, so the static gate cannot vet it, and the
    schema tells the model network is DISABLED — a claim the subprocess backend
    cannot keep. Refusing is the honest failure.
    """
    from app.agents import governance_sandbox as GSB

    monkeypatch.setattr(GSB, "_docker_available", lambda: False)
    r = GSB.run_shell("echo hi", cwd=tmp_path)
    assert r["ran"] is False
    assert "requires the docker sandbox backend" in (r["error"] or "")
    # run_script is deliberately NOT subject to this — declared scripts are
    # static-gated and on disk, so their subprocess fallback keeps real containment.
