# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The shipped example skills must RUN, not merely parse.

`test_example_skills.py` pins the two example rulebooks to the parser contract.
That is necessary but not sufficient: a file can parse perfectly and still never
produce a verdict, because the parts that decide a stage's outcome live in the
orchestrator — directive assembly, the anchored `[Dn] PASS/FAIL` floor, the
coverage tally, the clean/park decision, and the EA → InfoSec chain.

So this module drives `_phase_gov_review` itself against the real
`examples/governance_skills/*.md`, with only the reviewer LLM and the on-disk
change set stubbed. Everything between "a rule exists in the markdown" and "the
stage completed and the Build gate opened" is the production code path.

The stub reviewer is deliberately faithful rather than convenient: it answers
one anchored verdict per directive it is actually handed, so a regression that
stops issuing a directive for a rule shows up as a coverage gap here instead of
silently shrinking what the stage checks.
"""
from __future__ import annotations

import importlib as _importlib
import inspect
import importlib.util
import pathlib
import pkgutil as _pkgutil
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 — register every model on Base before create_all
# Import EVERY model module (several FK targets aren't in app.models.__init__) so
# create_all sees the full FK graph — same import dance the governance tests use.
for _m in _pkgutil.iter_modules(__import__("app.models", fromlist=["x"]).__path__):
    _importlib.import_module(f"app.models.{_m.name}")

from app.agents import agentic_review  # noqa: E402
from app.agents import governance_orchestrator as G  # noqa: E402
from app.agents import governance_skills as GS  # noqa: E402
from app.agents.agentic_tools import FileOp  # noqa: E402
from app.core.database import Base  # noqa: E402
from app.models.agentic import AgenticRun, ChangeManifest  # noqa: E402
from app.models.governance_skill import GovernanceSkill  # noqa: E402

# Captured BEFORE any monkeypatching. A stub that takes **kw silently accepts
# kwargs the real reviewer would reject — which is exactly how four live gov_ea
# runs died with `run_review() got an unexpected keyword argument 'extra_tools'`
# while the governance tests stayed green. Every stubbed call is bound against
# this, so an orchestrator/reviewer signature drift fails here instead of in prod.
_REAL_RUN_REVIEW_SIG = inspect.signature(agentic_review.run_review)

BACKEND = pathlib.Path(__file__).resolve().parents[2]
EXAMPLES = BACKEND / "examples" / "governance_skills"
FILES = {"ea": EXAMPLES / "ea_review_skill.md", "infosec": EXAMPLES / "infosec_review_skill.md"}

# The one path the stubbed change set touches; PASS verdicts must cite it or
# _harden_batch_verdicts rewrites them into NOT-VERIFIED blockers.
CHANGED = "src/A.java"
CHANGED_BODY = "body"


def _load_seeder():
    """scripts/ is not importable (pytest.ini pins pythonpath to backend/), and the
    point is to exercise the SHIPPED seeder rather than a copy of its logic."""
    path = BACKEND / "scripts" / "seed_governance_skills.py"
    spec = importlib.util.spec_from_file_location("seed_governance_skills", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


@pytest.fixture()
def seeded(db):
    """Both example skills loaded through the shipped seeder's own code path."""
    seeder = _load_seeder()
    seeder.SessionLocal = lambda: db
    assert seeder.seed() == 0
    return db


def _parent(db, change_id="chg-e2e"):
    run = AgenticRun(change_request_id=change_id, phase="completed", status="completed",
                     kind="code", selected_repo_ids=["r1"], attempts_json={},
                     handoff_json={"feature_branch": "atom/x", "push_deferred": True})
    db.add(run)
    db.flush()
    from app.models.base import utcnow
    man = ChangeManifest(run_id=run.id, manifest_hash="p" * 64, selected_repo_ids=["r1"],
                         per_repo=[{"repo_id": "r1", "base_commit_sha": "b" * 40,
                                    "shared_branch_name": "atom/x"}],
                         operations=[{"op": "modify", "repo_id": "r1", "path": CHANGED,
                                      "content_hash": "a" * 64}],
                         verification={}, review={}, approved_at=utcnow(), approved_by="u1")
    db.add(man)
    db.flush()
    return run, man


class FakeReviewer:
    """Stands in for the reviewer LLM. Answers every directive it is handed with an
    anchored verdict, recording the directives so a test can assert the stage really
    bound one to each rule in the example file."""

    def __init__(self, *, fail_on=None, silent_on=None, sloppy_fail_on=None, bare_pass_on=None):
        # Each is {stage_kind: {1-based directive index, …}}:
        #   fail_on        → a well-formed blocking FAIL
        #   silent_on      → no verdict at all (exercises the completeness floor)
        #   sloppy_fail_on → FAIL mis-tagged severity=info/blocking=False
        #   bare_pass_on   → PASS asserted with no checkable evidence
        self.fail_on = fail_on or {}
        self.silent_on = silent_on or {}
        self.sloppy_fail_on = sloppy_fail_on or {}
        self.bare_pass_on = bare_pass_on or {}
        self.calls: list[dict] = []

    async def __call__(self, db, *, run_id, ctx, change_set, directives=None,
                       agent_name="", preface="", intent="", round=1, **kw):
        # Would the REAL run_review have accepted this exact call?
        _REAL_RUN_REVIEW_SIG.bind(db, run_id=run_id, ctx=ctx, change_set=change_set,
                                  directives=directives, agent_name=agent_name,
                                  preface=preface, intent=intent, round=round, **kw)
        directives = list(directives or [])
        kind = agent_name.removesuffix("_review")
        self.calls.append({"kind": kind, "directives": directives,
                           "round": round, "preface": preface})
        fails = self.fail_on.get(kind, set())
        silent = self.silent_on.get(kind, set())
        sloppy = self.sloppy_fail_on.get(kind, set())
        bare = self.bare_pass_on.get(kind, set())
        findings = []
        for i in range(1, len(directives) + 1):
            if i in silent:
                continue
            if i in fails:
                findings.append(agentic_review.Finding(
                    severity="blocker", category="security",
                    why=f"[D{i}] FAIL — {CHANGED}:12 violates this rule",
                    suggested_fix="Apply the rule.", file=CHANGED, line=12, blocking=True))
            elif i in sloppy:
                # A real reviewer failure mode: the verdict says FAIL but the
                # metadata says "don't worry about it".
                findings.append(agentic_review.Finding(
                    severity="info", category="convention",
                    why=f"[D{i}] FAIL — {CHANGED}:12 violates this rule",
                    suggested_fix="Apply the rule.", file=CHANGED, line=12, blocking=False))
            elif i in bare:
                findings.append(agentic_review.Finding(
                    severity="info", category="directive",
                    why=f"[D{i}] PASS — looks fine to me", blocking=False))
            else:
                findings.append(agentic_review.Finding(
                    severity="info", category="directive",
                    why=f"[D{i}] PASS — {CHANGED}:12 complies",
                    file=CHANGED, line=12, blocking=False))
        return agentic_review.ReviewFindings(findings=findings,
                                             blocking=any(f.blocking for f in findings),
                                             reviewer_model="stub", rounds=1)


@pytest.fixture(autouse=True)
def dispatched(monkeypatch):
    """Record chain dispatches instead of reaching for the broker. Without this a
    clean EA stage blocks ~19s on an unreachable Redis before falling back to the
    stale-lease re-arm — correct behaviour, but not something to pay for per test."""
    sent: list[str] = []
    import app.services.celery_tasks as CT
    monkeypatch.setattr(CT.agentic_drive_task, "delay", lambda rid: sent.append(rid))
    return sent


@pytest.fixture()
def harness(monkeypatch, tmp_path):
    """Stub exactly two seams: the reviewer LLM and the on-disk change set."""
    import app.agents.agentic_orchestrator as O

    ops = [FileOp(op="modify", repo_id="r1", path=CHANGED,
                  content=CHANGED_BODY, content_hash=G._sha(CHANGED_BODY))]
    monkeypatch.setattr(O, "_disk_change_set", lambda _db, _run: SimpleNamespace(operations=ops))
    monkeypatch.setattr(O, "_ws_id", lambda _run: "ws")
    monkeypatch.setattr(G.workspace_local, "run_dir", lambda _rid: tmp_path)
    monkeypatch.setattr(G.settings, "governance_reviews_enabled", True, raising=False)

    def _mk(**kw):
        rv = FakeReviewer(**kw)
        monkeypatch.setattr(agentic_review, "run_review", rv)
        return rv
    return _mk


def _to_review(db, run):
    """Walk the real phase machine to REVIEW — the workspace/context phases clone
    and index a repo, which is not what this module is testing."""
    gov = G._gov(run)
    gov["stage_base"] = {"r1": "b" * 40}
    gov["feature_branch"] = "atom/x"
    gov["source"] = "workspace"
    # What _phase_gov_workspace records: the parent's edits, so the stage's own
    # fix delta stays empty until the fixer actually writes something.
    gov["baseline"] = {"r1": {CHANGED: G._sha(CHANGED_BODY)}}
    G._save_gov(db, run, gov)
    for phase in (G.P.WORKSPACE_READY, G.P.CONTEXT_READY, G.P.REVIEW):
        G.S.advance(db, run, phase)
    db.flush()
    return run


async def _review(db, run):
    art = {"ctx": SimpleNamespace(run_id=run.id, selected_repo_ids=["r1"]), "intent": "x"}
    await G._phase_gov_review(db, run, art, None)
    db.refresh(run)
    return art


async def _run_stage(db, parent, kind):
    """Create the stage the way the API does, then run its REVIEW phase."""
    run, created = G.create_stage_run(db, parent, kind, created_by="u1")
    assert created
    _to_review(db, run)
    return run, await _review(db, run)


# ── the seeder: the documented way these skills get loaded ────────────────────

def test_seeder_loads_both_examples(seeded):
    rows = seeded.query(GovernanceSkill).order_by(GovernanceSkill.skill_type).all()
    assert [(r.skill_type, r.version, r.name) for r in rows] == [
        ("ea", 1, "example-ea-architecture-review"),
        ("infosec", 1, "example-infosec-code-review"),
    ]
    for r in rows:
        assert r.checksum == GS.checksum(r.content)
        assert len(r.rules_json) == 12
        assert r.provenance_json["example"] is True


def test_seeder_is_idempotent(seeded):
    """A second run must not append a duplicate version — the README promises this."""
    seeder = _load_seeder()
    seeder.SessionLocal = lambda: seeded
    assert seeder.seed() == 0
    assert seeded.query(GovernanceSkill).count() == 2


def test_seeded_examples_are_loadable_by_the_stage(seeded):
    for stype in ("ea", "infosec"):
        skills = G.active_skills(seeded, stype)
        assert len(skills) == 1
        _pre, rules, _content = G._combined_rules(skills)
        assert len(rules) == 12


# ── the review actually runs, rule by rule ────────────────────────────────────

@pytest.mark.parametrize("kind,stype,prefix", [("gov_ea", "ea", "EA-"),
                                               ("gov_is", "infosec", "IS-")])
async def test_every_example_rule_becomes_a_binding_directive(seeded, harness, kind, stype, prefix):
    rv = harness()
    parent, _ = _parent(seeded)
    run, _art = await _run_stage(seeded, parent, kind)

    handed = [d for c in rv.calls for d in c["directives"]]
    assert len(handed) == 12, "one directive per rule in the example file"
    ids = [d.split(":")[0].removeprefix("Rule ").strip() for d in handed]
    assert ids == [f"{prefix}{n:02d}" for n in range(1, 13)]
    # The rulebook reaches the model VERBATIM, wrapped in the stage's own preface —
    # a truncated or paraphrased skill block would mean rules judged on a summary.
    preface = rv.calls[0]["preface"]
    body = GS.parse_frontmatter(FILES[stype].read_text(encoding="utf-8"))[1]
    assert body.strip() in preface
    for rule_id in ids:
        assert rule_id in preface
    assert G._gov(run)["rule_coverage"] == {"total": 12, "passed": 12, "failed": 0, "gaps": 0}


@pytest.mark.parametrize("kind", ["gov_ea", "gov_is"])
async def test_all_rules_pass_completes_the_stage_clean(seeded, harness, kind):
    harness()
    parent, _ = _parent(seeded)
    run, art = await _run_stage(seeded, parent, kind)

    assert art["review"]["items"] == [], "a clean review parks nothing for a human"
    assert art["review"]["has_blocker"] is False
    assert run.status == "completed"
    assert G._gov(run)["result"] == "clean"


async def test_a_failed_rule_blocks_the_stage(seeded, harness):
    """The FAIL path: it must reach the fixer as a blocking, actionable finding."""
    harness(fail_on={"gov_is": {3}})          # IS-03 — injection sinks
    parent, _ = _parent(seeded)
    run, art = await _run_stage(seeded, parent, "gov_is")

    assert G._gov(run)["rule_coverage"] == {"total": 12, "passed": 11, "failed": 1, "gaps": 0}
    assert art["review"]["has_blocker"] is True
    items = art["review"]["items"]
    assert len(items) == 1 and items[0]["file"] == CHANGED
    assert not items[0].get("reviewer_gap"), "a rule FAIL is the author's to fix"
    assert G._gov(run)["result"] is None, "a blocked stage must not self-complete"
    assert run.status != "completed"


async def test_a_mistagged_fail_still_blocks_the_stage(seeded, harness):
    """Verdict-integrity floor #1: the stage's clean decision reads the blocking-item
    list, so a FAIL the reviewer tagged severity=info/blocking=False would otherwise
    open the clean path with a known violation in the change."""
    harness(sloppy_fail_on={"gov_ea": {2}})   # EA-02 — hardcoded environment values
    parent, _ = _parent(seeded)
    run, art = await _run_stage(seeded, parent, "gov_ea")

    assert G._gov(run)["rule_coverage"] == {"total": 12, "passed": 11, "failed": 1, "gaps": 0}
    assert art["review"]["has_blocker"] is True
    assert len(art["review"]["items"]) == 1
    assert G._gov(run)["result"] is None, "an anchored FAIL can never complete clean"


async def test_a_pass_without_evidence_is_not_verified(seeded, harness):
    """Verdict-integrity floor #2: 'PASS — trust me' is not a verdict. It must become
    a blocking reviewer gap, not a silent compliance tick."""
    harness(bare_pass_on={"gov_is": {5}})     # IS-05 — authn/authz on new endpoints
    parent, _ = _parent(seeded)
    run, art = await _run_stage(seeded, parent, "gov_is")

    gaps = [i for i in art["review"]["items"] if i.get("reviewer_gap")]
    assert len(gaps) == 1 and "NOT VERIFIED" in gaps[0]["why"]
    assert G._gov(run)["rule_coverage"] == {"total": 12, "passed": 11, "failed": 0, "gaps": 1}
    assert G._gov(run)["result"] is None


async def test_an_unanswered_rule_is_a_gap_not_a_pass(seeded, harness):
    """The completeness floor: reviewer silence on a rule must never read as clean."""
    harness(silent_on={"gov_ea": {7}})        # EA-07 — retry safety
    parent, _ = _parent(seeded)
    run, art = await _run_stage(seeded, parent, "gov_ea")

    cov = G._gov(run)["rule_coverage"]
    assert cov == {"total": 12, "passed": 11, "failed": 0, "gaps": 1}
    gaps = [i for i in art["review"]["items"] if i.get("reviewer_gap")]
    assert len(gaps) == 1 and "NOT VERIFIED" in gaps[0]["why"]
    assert G._gov(run)["result"] is None, "an un-verdicted rule cannot complete clean"


# ── the two stages in sequence, and the Build gate ────────────────────────────

async def test_ea_clean_chains_into_infosec_and_opens_the_build_gate(seeded, harness, dispatched):
    harness()
    parent, _ = _parent(seeded)
    ea, _ = await _run_stage(seeded, parent, "gov_ea")
    assert G._gov(ea)["result"] == "clean"

    # _finish_stage chained EA → InfoSec, pinned to the infosec example.
    is_run = seeded.query(AgenticRun).filter(AgenticRun.kind == "gov_is").one()
    assert dispatched == [is_run.id]
    assert G._gov(is_run)["skill"]["name"] == "example-infosec-code-review"

    status = G.governance_status(seeded, parent.change_request_id)
    assert status["ea"]["passed"] is True
    assert status["all_passed"] is False, "Build stays gated until InfoSec passes too"

    # Drive the chained InfoSec stage's review to completion.
    _to_review(seeded, is_run)
    await _review(seeded, is_run)
    assert G._gov(is_run)["result"] == "clean"

    status = G.governance_status(seeded, parent.change_request_id)
    assert status["infosec"]["passed"] is True
    assert status["all_passed"] is True, "both stages clean → Build gate opens"
