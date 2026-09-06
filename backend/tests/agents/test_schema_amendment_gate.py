# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Schema-amendment gate (fix 2) — staging, provenance, apply, and the loop breaker.

Regression cover for the run that deadlocked for seventeen hours. Phase A wrote
``<xs:enumeration value="BG"/>`` for a new purpose code; "BG" was already bound to a live
transit code, so the reviewer correctly demanded BG → GP. The code phase physically could
not make that edit (``[REFUSED] … LOCKED``) and there was no route by which anyone could:
the finding was right and its remedy was impossible, so the loop could never converge.

The fix is not "let the code agent edit approved schema" — that re-opens a human-approved
artifact mid-implementation. It is: capture the exact edit, park, and let a human rule.
"""
import subprocess

import pytest

from app.core.config import settings
from app.agents import agentic_tools as T

RID = "repo-1"
RUN = "run-1"

_XSD = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:simpleType name="txnPurpose">
    <xs:restriction base="xs:string">
      <xs:enumeration value="00"/>
      <xs:enumeration value="BG"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>
"""


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    rd = tmp_path / RUN / RID
    (rd / "src").mkdir(parents=True)
    (rd / "src" / "NET-Common.xsd").write_text(_XSD, encoding="utf-8")
    (rd / "src" / "A.java").write_text("class A {}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=rd, check=True)
    subprocess.run(["git", "add", "-A"], cwd=rd, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
                   cwd=rd, check=True)
    return rd


def _head_sha(rd) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=rd,
                          capture_output=True, text=True, check=True).stdout.strip()


def _code_ctx():
    return T.RunContext(run_id=RUN, selected_repo_ids=[RID], code_phase=True)


# ── staging ──────────────────────────────────────────────────────────────────


def test_code_phase_schema_edit_is_staged_not_applied(ws):
    ctx = _code_ctx()
    T.read_file(ctx, RID, "src/NET-Common.xsd")
    out = T.edit_file(ctx, RID, "src/NET-Common.xsd",
                      '<xs:enumeration value="BG"/>', '<xs:enumeration value="GP"/>')
    assert "[STAGED" in out
    # The disk is untouched: the approved schema is not edited on a model's say-so.
    assert 'value="BG"' in (ws / "src" / "NET-Common.xsd").read_text(encoding="utf-8")
    assert 'value="GP"' not in (ws / "src" / "NET-Common.xsd").read_text(encoding="utf-8")
    # …but the exact edit is captured, so approval can replay it byte-for-byte.
    staged = list(ctx.schema_amendments.values())
    assert len(staged) == 1
    assert staged[0]["old_string"] == '<xs:enumeration value="BG"/>'
    assert staged[0]["new_string"] == '<xs:enumeration value="GP"/>'
    assert staged[0]["kind"] == "edit"
    # It is NOT recorded as a completed file op — nothing changed on disk.
    assert (RID, "src/NET-Common.xsd") not in ctx.file_ops


def test_staging_message_tells_the_agent_to_keep_working(ws):
    ctx = _code_ctx()
    T.read_file(ctx, RID, "src/NET-Common.xsd")
    out = T.edit_file(ctx, RID, "src/NET-Common.xsd", 'value="BG"', 'value="GP"')
    low = out.lower()
    assert "not yet applied" in low
    assert "do not retry" in low            # a retry loop is the failure being fixed
    assert "summary" in low                  # must justify the change with evidence


def test_restaging_the_same_edit_is_idempotent(ws):
    """The agent retried the identical write eleven times in the real run. Staging twice
    must not produce two proposals for a human to review."""
    ctx = _code_ctx()
    T.read_file(ctx, RID, "src/NET-Common.xsd")
    T.edit_file(ctx, RID, "src/NET-Common.xsd", 'value="BG"', 'value="GP"')
    out2 = T.edit_file(ctx, RID, "src/NET-Common.xsd", 'value="BG"', 'value="GP"')
    assert "[ALREADY STAGED]" in out2
    assert len(ctx.schema_amendments) == 1


def test_a_different_schema_edit_stages_separately(ws):
    ctx = _code_ctx()
    T.read_file(ctx, RID, "src/NET-Common.xsd")
    T.edit_file(ctx, RID, "src/NET-Common.xsd", 'value="BG"', 'value="GP"')
    T.edit_file(ctx, RID, "src/NET-Common.xsd", 'value="00"', 'value="01"')
    assert len(ctx.schema_amendments) == 2


def test_staging_is_capped(ws):
    """Past a dozen proposals nobody can review them one by one — the schema baseline is
    wrong for this change and the agent should stop, not keep queueing edits."""
    ctx = _code_ctx()
    T.read_file(ctx, RID, "src/NET-Common.xsd")
    for i in range(T._MAX_STAGED_AMENDMENTS):
        T.edit_file(ctx, RID, "src/NET-Common.xsd", f"old-{i}", f"new-{i}")
    out = T.edit_file(ctx, RID, "src/NET-Common.xsd", "one-too-many", "x")
    assert "[NOT STAGED]" in out
    assert len(ctx.schema_amendments) == T._MAX_STAGED_AMENDMENTS


def test_creating_a_schema_file_is_staged(ws):
    ctx = _code_ctx()
    out = T.create_file(ctx, RID, "src/New.xsd", "<xs:schema/>")
    assert "[STAGED" in out
    assert not (ws / "src" / "New.xsd").exists()
    assert list(ctx.schema_amendments.values())[0]["kind"] == "create"


def test_java_edits_are_unaffected_in_the_code_phase(ws):
    """The freeze applies to schema only — the code phase's actual job must still work."""
    ctx = _code_ctx()
    T.read_file(ctx, RID, "src/A.java")
    out = T.edit_file(ctx, RID, "src/A.java", "class A {}", "class A { int y; }")
    assert "[STAGED" not in out and "edited" in out
    assert "int y" in (ws / "src" / "A.java").read_text(encoding="utf-8")
    assert not ctx.schema_amendments


def test_xsd_phase_still_edits_schema_directly(ws):
    """Phase A authors schema — nothing here may stage or block it."""
    ctx = T.RunContext(run_id=RUN, selected_repo_ids=[RID], schema_only=True)
    T.read_file(ctx, RID, "src/NET-Common.xsd")
    out = T.edit_file(ctx, RID, "src/NET-Common.xsd", 'value="BG"', 'value="GP"')
    assert "[STAGED" not in out
    assert 'value="GP"' in (ws / "src" / "NET-Common.xsd").read_text(encoding="utf-8")


# ── provenance (describe) ────────────────────────────────────────────────────


def test_describe_marks_text_added_by_phase_a_in_this_change(ws):
    """The most important distinction at the gate: amending text Phase A wrote an hour ago
    is correcting an in-flight mistake, not altering a contract other systems speak."""
    from app.services import schema_amendment as SA
    base = _head_sha(ws)
    # Phase A adds a NEW enum after the pinned base — exactly the BG case.
    p = ws / "src" / "NET-Common.xsd"
    p.write_text(p.read_text(encoding="utf-8").replace(
        '<xs:enumeration value="BG"/>', '<xs:enumeration value="BG"/>\n      <xs:enumeration value="ZZ"/>'),
        encoding="utf-8")
    out = SA.describe(RUN, RUN, {RID: base}, [
        {"repo_id": RID, "path": "src/NET-Common.xsd", "kind": "edit",
         "old_string": '<xs:enumeration value="ZZ"/>', "new_string": '<xs:enumeration value="GP"/>'}])
    assert out[0]["origin"] == "phase_a"
    assert out[0]["applicable"] is True
    assert out[0]["line"] is not None
    assert "in-flight" in out[0]["origin_note"]


def test_describe_marks_pre_existing_baseline_text(ws):
    from app.services import schema_amendment as SA
    out = SA.describe(RUN, RUN, {RID: _head_sha(ws)}, [
        {"repo_id": RID, "path": "src/NET-Common.xsd", "kind": "edit",
         "old_string": '<xs:enumeration value="00"/>', "new_string": '<xs:enumeration value="01"/>'}])
    assert out[0]["origin"] == "baseline"
    assert "wire-contract" in out[0]["origin_note"]


def test_describe_never_guesses_provenance_without_a_base(ws):
    from app.services import schema_amendment as SA
    out = SA.describe(RUN, RUN, {}, [                      # no base SHA for the repo
        {"repo_id": RID, "path": "src/NET-Common.xsd", "kind": "edit",
         "old_string": 'value="BG"', "new_string": 'value="GP"'}])
    assert out[0]["origin"] == "unknown"


def test_describe_flags_a_proposal_that_can_no_longer_apply(ws):
    from app.services import schema_amendment as SA
    out = SA.describe(RUN, RUN, {RID: _head_sha(ws)}, [
        {"repo_id": RID, "path": "src/NET-Common.xsd", "kind": "edit",
         "old_string": "text that is not in the file", "new_string": "x"}])
    assert out[0]["applicable"] is False


# ── apply ────────────────────────────────────────────────────────────────────


def test_apply_lands_the_edit_verbatim(ws):
    """Approval replays the staged text exactly — no model re-does the edit, so what the
    human saw is what lands."""
    from app.services import schema_amendment as SA
    res = SA.apply(RUN, RUN, [
        {"repo_id": RID, "path": "src/NET-Common.xsd", "kind": "edit",
         "old_string": '<xs:enumeration value="BG"/>', "new_string": '<xs:enumeration value="GP"/>'}])
    assert len(res["applied"]) == 1 and not res["failed"]
    txt = (ws / "src" / "NET-Common.xsd").read_text(encoding="utf-8")
    assert 'value="GP"' in txt and 'value="BG"' not in txt


def test_apply_reports_rather_than_forcing_a_stale_proposal(ws):
    from app.services import schema_amendment as SA
    res = SA.apply(RUN, RUN, [
        {"repo_id": RID, "path": "src/NET-Common.xsd", "kind": "edit",
         "old_string": "no longer present", "new_string": "x"}])
    assert not res["applied"] and len(res["failed"]) == 1
    assert "no longer present" in res["failed"][0]["reason"]


def test_apply_refuses_an_ambiguous_match(ws):
    """Two identical anchors means the approval could land in the wrong place — report it."""
    from app.services import schema_amendment as SA
    p = ws / "src" / "NET-Common.xsd"
    p.write_text(p.read_text(encoding="utf-8") + '\n<!-- <xs:enumeration value="BG"/> -->\n',
                 encoding="utf-8")
    res = SA.apply(RUN, RUN, [
        {"repo_id": RID, "path": "src/NET-Common.xsd", "kind": "edit",
         "old_string": '<xs:enumeration value="BG"/>', "new_string": '<xs:enumeration value="GP"/>'}])
    assert not res["applied"] and "ambiguous" in res["failed"][0]["reason"]


def test_apply_creates_a_new_schema_file(ws):
    from app.services import schema_amendment as SA
    res = SA.apply(RUN, RUN, [{"repo_id": RID, "path": "src/New.xsd", "kind": "create",
                               "content": "<xs:schema/>"}])
    assert len(res["applied"]) == 1
    assert (ws / "src" / "New.xsd").read_text(encoding="utf-8") == "<xs:schema/>"


def test_one_bad_amendment_does_not_abort_the_others(ws):
    from app.services import schema_amendment as SA
    res = SA.apply(RUN, RUN, [
        {"repo_id": RID, "path": "src/missing.xsd", "kind": "edit", "old_string": "a", "new_string": "b"},
        {"repo_id": RID, "path": "src/NET-Common.xsd", "kind": "edit",
         "old_string": 'value="BG"', "new_string": 'value="GP"'}])
    assert len(res["applied"]) == 1 and len(res["failed"]) == 1


# ── rejection ────────────────────────────────────────────────────────────────


def test_rejection_directive_is_actionable_not_just_no():
    """A bare "no" makes the agent re-propose the same edit next round — which is the loop.
    The directive must close the question AND say what to do instead."""
    from app.services import schema_amendment as SA
    d = SA.rejection_directive([{"file": "NET-Common.xsd", "path": "src/NET-Common.xsd"}],
                               reason="BG is fine; scope the check by message type")
    assert "REJECTED" in d
    assert "NET-Common.xsd" in d
    assert "scope the check by message type" in d          # the human's reason is carried
    assert "again" in d.lower()                            # explicitly closes re-proposing
    assert "message type" in d or "adapter" in d           # names a concrete alternative


def test_amendment_key_is_stable_and_discriminating():
    from app.agents.agentic_orchestrator import _amendment_key
    a = {"repo_id": "r", "path": "p.xsd", "kind": "edit", "old_string": "BG"}
    assert _amendment_key(a) == _amendment_key(dict(a))            # same proposal, same key
    assert _amendment_key(a) != _amendment_key({**a, "old_string": "00"})   # different edit


# ── review finding 1: a partial apply must not resume as if the schema were fixed ────
#
# `apply()` refuses stale/ambiguous anchors by design. The danger is what the API does
# next: if it records the failed proposal as "decided" and tells the agent "the schema
# now contains your proposed text", the agent writes Java against a schema that was never
# amended — a BINDING falsehood it cannot detect, and the gate can never re-open because
# the proposal's key is already burned. That is strictly worse than the deadlock.


def test_apply_reports_failure_instead_of_forcing_a_stale_anchor(ws):
    from app.services import schema_amendment as SA
    res = SA.apply(RUN, RUN, [{"repo_id": RID, "path": "src/NET-Common.xsd", "kind": "edit",
                               "old_string": '<xs:enumeration value="ZZ"/>',   # not present
                               "new_string": '<xs:enumeration value="GP"/>'}])
    assert not res["applied"] and len(res["failed"]) == 1
    assert "no longer present" in res["failed"][0]["reason"]
    assert 'value="GP"' not in (ws / "src" / "NET-Common.xsd").read_text(encoding="utf-8")


def _decide(monkeypatch, ws, amendments, *, approve=True):
    """Drive the real endpoint function against a stub run parked at the gate."""
    from types import SimpleNamespace
    import app.api.agentic as A

    events, advanced = [], []
    run = SimpleNamespace(id=RUN, change_request_id="cr-1", phase="awaiting_schema_amendment",
                          workspace_run_id=RUN, kind="code", created_by="u1",
                          handoff_json={"schema_amendment_request": {"amendments": amendments}})
    monkeypatch.setattr(A, "_run_or_404", lambda db, rid: run)
    monkeypatch.setattr(A, "_authz_write", lambda r, u: None)
    monkeypatch.setattr(A, "_run_view", lambda r: {"id": r.id})
    monkeypatch.setattr(A, "_record_amendment_decision",
                        lambda db, r, u, a, **kw: events.append(("ledger", kw)))
    monkeypatch.setattr("app.agents.agentic_events.emit_event",
                        lambda db, rid, kind, payload=None: events.append((kind, payload)))
    monkeypatch.setattr(A.S, "advance", lambda db, r, p: advanced.append(p))
    monkeypatch.setattr("app.services.celery_tasks.agentic_drive_task",
                        SimpleNamespace(delay=lambda *a, **k: None))
    db = SimpleNamespace(commit=lambda: None, add=lambda *a: None)
    body = A.DecideSchemaAmendmentRequest(approve=approve, reason=None if approve else "no")
    return run, events, advanced, A, db, body


def test_partial_apply_reparks_and_does_not_resume(ws, monkeypatch):
    """One good edit, one stale: the good one lands, but the run must NOT advance to
    CODE_CHANGE, and the stale proposal must stay open for a fresh ruling."""
    from fastapi import HTTPException
    good = {"repo_id": RID, "path": "src/NET-Common.xsd", "file": "NET-Common.xsd",
            "kind": "edit", "old_string": '<xs:enumeration value="BG"/>',
            "new_string": '<xs:enumeration value="GP"/>'}
    stale = {"repo_id": RID, "path": "src/NET-Common.xsd", "file": "NET-Common.xsd",
             "kind": "edit", "old_string": '<xs:enumeration value="ZZ"/>',
             "new_string": '<xs:enumeration value="YY"/>'}
    run, events, advanced, A, db, body = _decide(monkeypatch, ws, [good, stale])

    with pytest.raises(HTTPException) as ei:
        A.decide_schema_amendment(RUN, body, db, _User())
    assert ei.value.status_code == 409

    assert advanced == [], "a half-applied amendment must not resume code generation"
    kinds = [k for k, _ in events]
    assert "schema_amendment_partial" in kinds
    assert "schema_amendment_approved" not in kinds, "no 'applied' claim when it wasn't"

    h = run.handoff_json
    # The stale proposal is re-staged so the human sees it again…
    assert h["schema_amendment_request"]["amendments"], "gate must re-open on the remainder"
    # …and only the edit that genuinely landed is recorded as decided.
    from app.agents.agentic_orchestrator import _amendment_key
    assert _amendment_key(good) in h["schema_amendments_decided"]
    assert _amendment_key(stale) not in h["schema_amendments_decided"], \
        "burning a failed key would stop the gate from ever re-opening for it"


def test_full_apply_resumes_and_marks_everything_decided(ws, monkeypatch):
    good = {"repo_id": RID, "path": "src/NET-Common.xsd", "file": "NET-Common.xsd",
            "kind": "edit", "old_string": '<xs:enumeration value="BG"/>',
            "new_string": '<xs:enumeration value="GP"/>'}
    run, events, advanced, A, db, body = _decide(monkeypatch, ws, [good])
    A.decide_schema_amendment(RUN, body, db, _User())

    assert advanced, "a fully applied amendment resumes code generation"
    kinds = [k for k, _ in events]
    assert "schema_amendment_approved" in kinds and "schema_amendment_partial" not in kinds
    assert 'value="GP"' in (ws / "src" / "NET-Common.xsd").read_text(encoding="utf-8")
    assert "schema_amendment_request" not in run.handoff_json
    from app.agents.agentic_orchestrator import _amendment_key
    assert _amendment_key(good) in run.handoff_json["schema_amendments_decided"]


def test_rejection_still_resumes_and_settles_the_proposal(ws, monkeypatch):
    """A rejection IS a real outcome — the schema is unchanged by design — so it resumes
    and the proposal is settled, otherwise the agent would re-propose it forever."""
    a = {"repo_id": RID, "path": "src/NET-Common.xsd", "file": "NET-Common.xsd",
         "kind": "edit", "old_string": '<xs:enumeration value="BG"/>',
         "new_string": '<xs:enumeration value="GP"/>'}
    run, events, advanced, A, db, body = _decide(monkeypatch, ws, [a], approve=False)
    A.decide_schema_amendment(RUN, body, db, _User())

    assert advanced, "a rejection resumes with a binding implement-around directive"
    assert "schema_amendment_rejected" in [k for k, _ in events]
    assert 'value="BG"' in (ws / "src" / "NET-Common.xsd").read_text(encoding="utf-8")
    from app.agents.agentic_orchestrator import _amendment_key
    assert _amendment_key(a) in run.handoff_json["schema_amendments_decided"]


class _User:
    id = "u1"
    role = "admin"
    is_admin = True
