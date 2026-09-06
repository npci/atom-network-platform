# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Reuse-first approach gate + XSD refine loop (THE BOOK v3.4).

Before any API/XSD is created, the agent maps the existing flows and STOPS to present
the human reuse-vs-new OPTIONS; the human's choice is injected on resume. At the XSD gate
the human can request changes — comply-first: additive requests are applied as asked
(objections go on record via flag_concern), and only a genuinely BREAKING request opens
one propose_revision conversation whose outcome is final.
"""
import asyncio
from types import SimpleNamespace

from app.core.config import settings
from app.agents import agentic_orchestrator as O
from app.agents import agentic_state as S
from app.agents.agentic_tools import RunContext, propose_approach, flag_concern, _DISPATCH
from app.agents.agentic_subagents import _xsd_user_prompt, PROPOSE_TOOLS
from app.agents.context_assembler import ContextPack
from app.models.agentic import AgenticPhase as P
from app.agents.agentic_state import VALID_TRANSITIONS


# ── unit: the tools record state + the propose pass cannot edit ──────────────────

def test_propose_approach_records_when_evidence_is_grounded():
    ctx = RunContext(run_id="r", selected_repo_ids=["x"])
    # Agent actually read these two files this run → evidence citing them is verified.
    ctx.read_files = {("x", "ReqTransfer.java"), ("x", "TxnConsumer.java")}
    out = propose_approach(
        ctx, "summary", [{"id": "reuse-txn", "approach": "reuse"}], recommended="reuse-txn",
        evidence=[{"claim": "carries debit leg", "file": "ReqTransfer.java"},
                  {"claim": "consumer", "file": "TxnConsumer.java"}])
    assert ctx.awaiting_decision is True
    assert ctx.proposal["recommended"] == "reuse-txn" and len(ctx.proposal["options"]) == 1
    assert len(ctx.proposal["evidence"]) == 2
    assert "decision" in out.lower()


def test_propose_approach_rejected_when_evidence_not_read():
    ctx = RunContext(run_id="r", selected_repo_ids=["x"])
    ctx.read_files = {("x", "ReqTransfer.java")}     # only one file actually read
    out = propose_approach(
        ctx, "summary", [{"id": "reuse-txn", "approach": "reuse"}], recommended="reuse-txn",
        evidence=[{"claim": "carries debit leg", "file": "ReqTransfer.java"},
                  {"claim": "consumer", "file": "NeverOpened.java"}])   # this one wasn't read
    # Only 1 verified citation (<2) → NOT recorded; agent is bounced back to read more.
    assert ctx.awaiting_decision is False
    assert ctx.proposal is None
    assert "not recorded" in out.lower() and "NeverOpened.java" in out


def test_propose_approach_normalises_plan_divergence_flags():
    ctx = RunContext(run_id="r", selected_repo_ids=["x"])
    ctx.read_files = {("x", "ReqTransfer.java"), ("x", "TxnConsumer.java")}
    opts = [{"id": "extend", "approach": "extend", "diverges_from_plan": "yes",
             "divergence_note": "plan recommended a new schema; this extends payTrans instead"},
            {"id": "new", "approach": "new", "diverges_from_plan": "no", "divergence_note": ""}]
    propose_approach(ctx, "summary", opts, recommended="extend",
                     evidence=[{"claim": "carries debit leg", "file": "ReqTransfer.java"},
                               {"claim": "consumer", "file": "TxnConsumer.java"}])
    by_id = {o["id"]: o for o in ctx.proposal["options"]}
    # "yes"/"no" strings collapse to REAL bools — a literal "no" must not read as truthy in the UI.
    assert by_id["extend"]["diverges_from_plan"] is True
    assert by_id["new"]["diverges_from_plan"] is False
    assert "payTrans" in by_id["extend"]["divergence_note"]


def test_propose_approach_coerces_string_options():
    # Off-framework asks (e.g. 'convert the whole codebase to Rust') made the model pass
    # options as plain strings — these must become gate-renderable {id, title} dicts.
    ctx = RunContext(run_id="r", selected_repo_ids=["x"])
    ctx.read_files = {("x", "ReqTransfer.java"), ("x", "TxnConsumer.java")}
    propose_approach(ctx, "platform migration", ["Full rewrite", "Strangler-fig", "  "],
                     recommended="option-1",
                     evidence=[{"claim": "flow", "file": "ReqTransfer.java"},
                               {"claim": "consumer", "file": "TxnConsumer.java"}])
    opts = ctx.proposal["options"]
    assert [o["id"] for o in opts] == ["option-1", "option-2"]      # blank option dropped
    assert opts[0]["title"] == "Full rewrite" and opts[0]["how_it_fits"] == "Full rewrite"
    assert all(isinstance(o, dict) for o in opts)


def test_propose_approach_ids_contiguous_when_blank_leads():
    # A blank option BEFORE real ones must not shift ids — synthesized ids stay
    # contiguous from option-1 so a caller's recommended="option-1" still resolves.
    ctx = RunContext(run_id="r", selected_repo_ids=["x"])
    ctx.read_files = {("x", "ReqTransfer.java"), ("x", "TxnConsumer.java")}
    propose_approach(ctx, "platform migration", ["  ", "Full rewrite", "Strangler-fig"],
                     recommended="option-1",
                     evidence=[{"claim": "flow", "file": "ReqTransfer.java"},
                               {"claim": "consumer", "file": "TxnConsumer.java"}])
    opts = ctx.proposal["options"]
    assert [o["id"] for o in opts] == ["option-1", "option-2"]      # not option-2/option-3
    assert opts[0]["title"] == "Full rewrite"
    # recommended now lines up with a real option
    assert ctx.proposal["recommended"] in {o["id"] for o in opts}


def test_tool_call_detail_survives_string_options():
    # 2026-07-03 prod failure: _detail() did o.get() on a string option and the
    # AttributeError killed the whole run. Telemetry formatting must never raise.
    from app.agents.agentic_runtime import _detail
    ti = {"summary": "migration", "options": ["Full rewrite", "Strangler-fig"],
          "recommended": "option-1"}
    out = _detail("propose_approach", ti, "result")
    assert "Full rewrite" in out and "Strangler-fig" in out
    # options as a bare string (not a list) degrades to the summary, still no raise
    assert _detail("propose_approach", {"summary": "s", "options": "oops"}, "r") == "s"


def test_flag_concern_records_and_does_not_apply():
    ctx = RunContext(run_id="r", selected_repo_ids=["x"])
    flag_concern(ctx, "removing amount breaks ReqTransfer consumers", severity="blocker",
                 declined_change="remove <Amount> element")
    assert ctx.concerns and ctx.concerns[0]["severity"] == "blocker"
    assert ctx.concerns[0]["declined_change"] == "remove <Amount> element"


def test_propose_tools_are_read_only():
    names = {t["name"] for t in PROPOSE_TOOLS}
    assert "propose_approach" in names
    # The propose pass must NOT be able to create/edit/delete anything.
    assert not ({"edit_file", "create_file", "delete_file"} & names)
    assert _DISPATCH["propose_approach"] is propose_approach and _DISPATCH["flag_concern"] is flag_concern


# ── unit: apply-mode prompt injects the human's decision + the refine guardrail ──

def test_xsd_prompt_injects_decision():
    decision = {"option": {"approach": "reuse", "title": "Route money via existing txn API",
                           "target_api": "the existing transaction API", "how_it_fits": "add a meta field"}}
    p = _xsd_user_prompt(ContextPack(selected_repo_ids=["r"]), "do epfo", decision=decision)
    assert "human chose this approach" in p.lower()
    assert "existing transaction API" in p


def test_xsd_prompt_injects_change_request_with_guardrail():
    p = _xsd_user_prompt(ContextPack(selected_repo_ids=["r"]), "do epfo",
                         change_request="remove the Amount element")
    assert "requested changes" in p.lower()
    # the guardrail rode along: disruptive requests open a conversation, not a silent decline
    assert "DISRUPTIVE" in p and "propose_revision" in p


# ── state machine: the new gate + the code-skip + the refine edge exist ──────────

def test_transitions_for_gate_and_refine_and_code_skip():
    assert P.AWAITING_APPROACH_DECISION in VALID_TRANSITIONS[P.XSD_DISCOVERY]
    assert VALID_TRANSITIONS[P.AWAITING_APPROACH_DECISION] == {P.XSD_DISCOVERY, P.FAILED, P.CANCELLED}
    assert P.CODE_CHANGE in VALID_TRANSITIONS[P.CONTEXT_READY]            # Phase B skips XSD
    assert P.XSD_DISCOVERY in VALID_TRANSITIONS[P.AWAITING_XSD_APPROVAL]   # refine loop


# ── orchestrator: propose pass stops at the gate; decision → apply ───────────────

class _Run:
    def __init__(self, kind="xsd", handoff=None):
        self.id = "run-1"; self.kind = kind; self.phase = P.XSD_DISCOVERY.value
        self.selected_repo_ids = ["repo1"]; self.handoff_json = handoff or {}
        self.workspace_run_id = None; self.attempts_json = {}; self.change_request_id = "cr"


def _patch_common(monkeypatch, events):
    monkeypatch.setattr(O, "emit_event", lambda db, rid, kind, payload=None: events.append((kind, payload)))
    # advance just records the target phase on the run (no DB)
    def _adv(db, run, to):
        run.phase = to.value
    monkeypatch.setattr(O.S, "advance", _adv)


def test_step_proposes_and_gates_when_no_decision(monkeypatch):
    events = []
    _patch_common(monkeypatch, events)
    monkeypatch.setattr(settings, "agentic_approach_gate", True)
    async def fake_propose(db, run, art, model):
        return {"summary": "txn goes through the existing pay flow",
                "options": [{"id": "reuse", "approach": "reuse", "target_api": "txn API"}],
                "recommended": "reuse"}
    monkeypatch.setattr(O, "_phase_propose", fake_propose)
    # _phase_xsd must NOT be called in the propose pass.
    monkeypatch.setattr(O, "_phase_xsd", lambda *a, **k: (_ for _ in ()).throw(AssertionError("edited before decision!")))

    run = _Run(kind="xsd", handoff={})
    art = {"ctx": object(), "intent": "epfo"}
    asyncio.run(O._step(None, run, art, None))

    assert run.phase == P.AWAITING_APPROACH_DECISION.value
    assert any(k == "approach_proposal" for k, _ in events)


def test_step_applies_after_decision(monkeypatch):
    events = []
    _patch_common(monkeypatch, events)
    monkeypatch.setattr(settings, "agentic_approach_gate", True)
    called = {"xsd": 0, "freeze": 0}
    async def fake_xsd(db, run, art, model):
        called["xsd"] += 1
    monkeypatch.setattr(O, "_phase_xsd", fake_xsd)
    monkeypatch.setattr(O, "_phase_freeze_xsd", lambda db, run, art, **k: called.__setitem__("freeze", called["freeze"] + 1))
    monkeypatch.setattr(O, "_phase_propose", lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-proposed!")))

    run = _Run(kind="xsd", handoff={"approach_decision": {"selected_option_id": "reuse"}})
    asyncio.run(O._step(None, run, {"ctx": object(), "intent": "epfo"}, None))

    assert called["xsd"] == 1 and called["freeze"] == 1            # applied + froze
    assert run.phase == P.AWAITING_XSD_APPROVAL.value


def test_step_code_kind_skips_xsd_and_gate(monkeypatch):
    events = []
    _patch_common(monkeypatch, events)
    run = _Run(kind="code"); run.phase = P.CONTEXT_READY.value
    asyncio.run(O._step(None, run, {"ctx": object(), "intent": "x"}, None))
    assert run.phase == P.CODE_CHANGE.value          # straight to code, no XSD/gate


def test_gate_off_goes_straight_through(monkeypatch):
    events = []
    _patch_common(monkeypatch, events)
    monkeypatch.setattr(settings, "agentic_approach_gate", False)
    called = {"xsd": 0}
    async def fake_xsd(db, run, art, model):
        called["xsd"] += 1
    monkeypatch.setattr(O, "_phase_xsd", fake_xsd)
    monkeypatch.setattr(O, "_phase_freeze_xsd", lambda *a, **k: None)
    monkeypatch.setattr(O, "_phase_propose", lambda *a, **k: (_ for _ in ()).throw(AssertionError("gated though off!")))

    run = _Run(kind="xsd", handoff={})
    asyncio.run(O._step(None, run, {"ctx": object(), "intent": "x"}, None))
    assert called["xsd"] == 1 and run.phase == P.AWAITING_XSD_APPROVAL.value


# ── disruptive-revision conversation (refine loop) ────────────────────────────────

def test_propose_revision_records_and_stops_the_loop():
    from app.agents.agentic_tools import propose_revision
    ctx = RunContext(run_id="r", selected_repo_ids=["x"])
    out = propose_revision(ctx, "deleting Amount breaks ReqTransfer consumers",
                           [{"id": "deprecate", "title": "Deprecate instead of delete"}],
                           recommended="deprecate")
    assert ctx.awaiting_decision is True
    assert ctx.proposal["kind"] == "revision" and ctx.proposal["recommended"] == "deprecate"
    assert "decision" in out.lower()


def test_xsd_prompt_risk_accepted_overrides_guardrail():
    p = _xsd_user_prompt(ContextPack(selected_repo_ids=["r"]), "do epfo",
                         change_request="remove the Amount element", accepted_risk=True)
    assert "RISK ACCEPTED" in p and "implement the request EXACTLY".lower() in p.lower()
    assert "propose_revision again" in p          # told not to re-gate
    assert "call propose_revision with a clear explanation" not in p   # guardrail replaced


def test_xsd_prompt_guardrail_uses_propose_revision():
    p = _xsd_user_prompt(ContextPack(selected_repo_ids=["r"]), "do epfo",
                         change_request="remove the Amount element")
    assert "propose_revision" in p and "DISRUPTIVE" in p
    assert "RISK ACCEPTED" not in p


def test_xsd_prompt_guardrail_comply_first_on_additive_requests():
    # prod 58ab724c: an ADDITIVE "create the new API schema" request was refused three
    # times as "contradicting the ratified reuse decision". The guardrail must say the
    # opposite: additive work is never disruptive, and an explicit request supersedes
    # the earlier approach decision.
    p = _xsd_user_prompt(ContextPack(selected_repo_ids=["r"]), "do epfo",
                         change_request="create a new ReqComplaint message schema")
    assert "ADDITIVE" in p and "apply it as asked" in p
    assert "supersession" in p or "supersedes" in p
    assert "NEVER conditional" in p          # alternatives must be self-sufficient


def test_xsd_prompt_chosen_alternative_is_final():
    # The human picked one of the agent's OWN safer alternatives — the next round must
    # implement it, not re-litigate: guardrail replaced by the settled-decision block.
    p = _xsd_user_prompt(ContextPack(selected_repo_ids=["r"]), "do epfo",
                         change_request="Deprecate instead of delete. Keep Amount as-is.",
                         via_revision=True)
    assert "DECISION MADE" in p and "implement it NOW" in p
    assert "propose_revision" not in p       # no invitation to reopen the conversation
    assert "RISK ACCEPTED" not in p


def test_flag_concern_without_decline_still_applies():
    # Record-and-apply mode: an objection to a non-breaking request is put on record,
    # but the tool result tells the agent to apply the human's ask.
    ctx = RunContext(run_id="r", selected_repo_ids=["x"])
    out = flag_concern(ctx, "I would have reused ReqChkTxn instead of a new schema")
    assert "APPLY" in out and "NOT applied" not in out
    assert ctx.concerns[0]["declined_change"] is None


def test_flag_concern_with_decline_keeps_declining():
    ctx = RunContext(run_id="r", selected_repo_ids=["x"])
    out = flag_concern(ctx, "removing Amount breaks ReqTransfer consumers",
                       declined_change="remove the Amount element")
    assert "NOT applied" in out
    assert ctx.concerns[0]["declined_change"] == "remove the Amount element"


def test_new_api_flow_gaps_and_plan_file_keys():
    # file_change_list is the drift key that disarmed the required-schema guard in the wild
    # (change 215ead25) — it must be read like the other four.
    assert "file_change_list" in O._PLAN_FILE_KEYS
    ta = {"file_change_list": [
        {"path": "network-domain-xsd/src/main/resources/ReqVerifyPayee.xsd", "intent": "ADD"},
        {"path": "network-domain-xsd/src/main/resources/RespVerifyPayee.xsd", "intent": "ADD"},
        {"path": "network-domain-xsd/src/main/resources/network-common.xsd", "intent": "EXTEND"},
        {"path": "tp/src/main/java/A.java", "intent": "ADD"}]}
    flow = {"actors": ["payer PSP", "the authority switch"],
            "steps": [{"id": "s1", "message": "ReqVerifyPayee",
                       "from": "payer PSP", "to": "the authority switch"}]}
    # RespVerifyPayee is never routed → flagged; ReqVerifyPayee is → clear; network-common and
    # Java files are not message schemas → ignored.
    assert O._new_api_flow_gaps(ta, flow) == ["RespVerifyPayee"]
    assert O._new_api_flow_gaps(ta, None) == ["ReqVerifyPayee", "RespVerifyPayee"]
    assert O._new_api_flow_gaps(None, flow) == []
    # Token matching, not substring: an unrouted ReqTransfer is NOT masked by a routed
    # ReqTransferVerify, and a prose mention outside the route fields does not count.
    ta2 = {"file_change_list": [{"path": "x/ReqTransfer.xsd"}, {"path": "x/ReqTransferVerify.xsd"}]}
    flow2 = {"steps": [{"message": "ReqTransferVerify", "from": "payer PSP", "to": "switch"}],
             "overview": "this change also mentions ReqTransfer in passing"}
    assert O._new_api_flow_gaps(ta2, flow2) == ["ReqTransfer"]


def test_flow_gaps_ignore_extend_of_an_existing_message():
    # An EXTEND of an EXISTING message is not something the plan "introduces". The plan agent
    # is told to spell out a four-party route only for a NEW API, so demanding one for an
    # already-routed ReqTransfer is a false alarm that costs the PM a needless reopen.
    ta = {"file_change_list": [{"path": "x/ReqTransfer.xsd", "intent": "EXTEND — add credential subType"},
                               {"path": "x/RespTransfer.xsd", "intent": "modify the ack"},
                               {"path": "x/ReqBrandNew.xsd", "intent": "ADD"}]}
    assert O._new_api_flow_gaps(ta, {}) == ["ReqBrandNew"]
    # An entry with no intent at all still counts as an add (unchanged prior behaviour).
    assert O._new_api_flow_gaps({"file_change_list": [{"path": "x/ReqBare.xsd"}]}, {}) == ["ReqBare"]


def test_render_plan_includes_user_rectifications():
    out = O._render_analysis_plan(
        {"user_rectifications": [{"requested": "new dedicated verification API",
                                  "applied": "added ReqVerifyPayee/RespVerifyPayee",
                                  "feasibility": "verified",
                                  "repercussions": "a second flow to maintain"}]},
        {"overview": "x"}, {})
    assert "USER RECTIFICATIONS" in out and "a second flow to maintain" in out
    assert "implement EXACTLY" in out


def test_pending_plan_supersession_detection():
    # A refine round that CREATED schema files under a ratified reuse decision supersedes
    # the plan — detected deterministically so the human is told the plan delta and the
    # plan rolls at /approve-xsd, not silently.
    scope = SimpleNamespace(created=["core:network-domain-xsd/src/main/resources/ReqVerifyPayee.xsd",
                                     "core:transaction-processor/src/main/java/New.java"])
    handoff = {"approach_decision": {"approach": "reuse", "option": {"title": "Reuse ReqValAdd"}}}
    pend = O._pending_plan_supersession(handoff, "create a new dedicated verification API", scope)
    assert pend["prior_approach"] == "reuse" and pend["prior_title"] == "Reuse ReqValAdd"
    assert pend["new_files"] == ["core:network-domain-xsd/src/main/resources/ReqVerifyPayee.xsd"]
    # not a supersession: no human request / no new schema / decision already 'new'
    assert O._pending_plan_supersession(handoff, None, scope) is None
    assert O._pending_plan_supersession(handoff, "x", SimpleNamespace(created=[])) is None
    assert O._pending_plan_supersession({"approach_decision": {"approach": "new"}}, "x", scope) is None
    assert O._pending_plan_supersession({}, "x", scope) is None
    # PLAN-SANCTIONED files are never a supersession: a refine round that finally creates
    # a file the plan itself ordered (reuse = no new API, NOT no schema change) is
    # completing the plan — the approach must not silently flip to 'new'.
    assert O._pending_plan_supersession(handoff, "add the missing split file", scope,
                                        plan_schema={"reqverifypayee.xsd"}) is None
    pend2 = O._pending_plan_supersession(handoff, "also add a brand-new API", SimpleNamespace(
        created=["core:a/ReqVerifyPayee.xsd", "core:a/ReqBrandNew.xsd"]),
        plan_schema={"reqverifypayee.xsd"})
    assert pend2["new_files"] == ["core:a/ReqBrandNew.xsd"]   # only the unsanctioned file counts


def test_pending_plan_supersession_attribution_sticks_to_the_creating_round():
    # changed_files is HEAD-relative and refine rounds are not committed in between, so
    # round 1's added schema keeps reappearing in scope.created. A later, UNRELATED
    # request must not be re-stamped as its creator — the consent/audit record (event,
    # divergence note, PM banner) would name the wrong request.
    handoff = {"approach_decision": {"approach": "reuse", "option": {"title": "Reuse ReqValAdd"}}}
    scope = SimpleNamespace(created=["core:a/ReqVerifyPayee.xsd"])
    r1 = O._pending_plan_supersession(handoff, "create a new dedicated verification API", scope)
    # Round 2: unrelated request, same file still on disk → round 1's attribution stands.
    r2 = O._pending_plan_supersession(handoff, "tighten the amount regex", scope, prior=r1)
    assert r2["requested"] == "create a new dedicated verification API"
    assert r2["new_files"] == ["core:a/ReqVerifyPayee.xsd"]
    # Round 3: a request that adds MORE unsanctioned schema — both files pending, both
    # requests on record, in creation order.
    scope3 = SimpleNamespace(created=["core:a/ReqVerifyPayee.xsd", "core:a/ReqBrandNew.xsd"])
    r3 = O._pending_plan_supersession(handoff, "also add a brand-new API", scope3, prior=r2)
    assert r3["new_files"] == ["core:a/ReqVerifyPayee.xsd", "core:a/ReqBrandNew.xsd"]
    assert "create a new dedicated verification API" in r3["requested"]
    assert "also add a brand-new API" in r3["requested"]


def test_pending_plan_supersession_survives_a_request_free_round():
    # A crash-resume / request-free later round must not drop a supersession the human
    # has not ruled on yet — the file is still in the manifest they will approve.
    handoff = {"approach_decision": {"approach": "reuse", "option": {"title": "Reuse ReqValAdd"}}}
    scope = SimpleNamespace(created=["core:a/ReqVerifyPayee.xsd"])
    r1 = O._pending_plan_supersession(handoff, "create a new dedicated verification API", scope)
    kept = O._pending_plan_supersession(handoff, None, scope, prior=r1)
    assert kept == r1
    # But a round where the file is GONE from disk (reverted) genuinely clears it.
    assert O._pending_plan_supersession(handoff, None, SimpleNamespace(created=[]), prior=r1) is None
    # And plan re-sanctioning the file clears it too.
    assert O._pending_plan_supersession(handoff, None, scope,
                                        plan_schema={"reqverifypayee.xsd"}, prior=r1) is None


def test_merge_disk_created_uses_git_truth_not_the_loops_ops(monkeypatch):
    # A crash-resumed refine round re-edits the file it created last time: create_file refuses
    # an existing path, the re-edit records as "modify", and the loop's `created` comes back
    # EMPTY — so the supersession would go undetected while the frozen manifest (built from
    # disk) still carries the new schema. Disk is ground truth for "new since the base commit".
    from types import SimpleNamespace as NS
    run = NS(id="r1", selected_repo_ids=["core"], workspace_run_id=None, parent_run_id=None)
    scope = NS(created=[])          # what a resumed loop reports
    monkeypatch.setattr(O.workspace_local, "changed_files", lambda ws, rid: [
        ("add", "network-domain-xsd/src/main/resources/ReqVerifyPayee.xsd"),
        ("modify", "network-domain-xsd/src/main/resources/network-common.xsd"),
        ("add", "tp/src/main/java/New.java")])       # non-schema adds are not Phase A's business
    O._merge_disk_created(run, scope)
    assert scope.created == ["core:network-domain-xsd/src/main/resources/ReqVerifyPayee.xsd"]
    # Unions with (never discards) what the loop itself reported.
    scope2 = NS(created=["core:a/Other.xsd"])
    O._merge_disk_created(run, scope2)
    assert scope2.created == ["core:a/Other.xsd",
                              "core:network-domain-xsd/src/main/resources/ReqVerifyPayee.xsd"]


def _drive_post_xsd(monkeypatch, verify_status):
    """Run _post_xsd_advance with _phase_verify stubbed to return verify_status and
    _phase_freeze_xsd stubbed to record its build_status kwarg. Returns (run, events, freeze)."""
    events = []
    _patch_common(monkeypatch, events)
    monkeypatch.setattr(O, "_phase_verify",
                        lambda db, run, art, **k: (art.__setitem__("verification",
                            {"status": verify_status, "errors": ["Foo.xsd:12 cvc-complex-type: bad"]}),
                            verify_status)[1])
    freeze = {}
    monkeypatch.setattr(O, "_phase_freeze_xsd",
                        lambda db, run, art, **k: freeze.update(k))
    run = _Run(kind="xsd")
    run.phase = P.XSD_DISCOVERY.value
    O._post_xsd_advance(None, run, {"xsd_scope": None})
    return run, events, freeze


def test_post_xsd_advance_blocks_a_non_building_schema(monkeypatch):
    # Build failed (needs_fix): flag set, LOUD event, banner reworded — but still parked
    # for the human (they iterate via request-xsd-changes). Previously the status was
    # DISCARDED and a broken schema froze reading "ready to approve".
    run, events, freeze = _drive_post_xsd(monkeypatch, "needs_fix")
    assert run.phase == P.AWAITING_XSD_APPROVAL.value
    assert run.handoff_json.get("xsd_build_failed") is True
    assert freeze.get("build_status") == "needs_fix"
    ev = dict(events)
    assert "xsd_build_failed" in ev and "does NOT build" in ev["xsd_build_failed"]["action"]


def test_post_xsd_advance_clean_build_advances_normally(monkeypatch):
    run, events, freeze = _drive_post_xsd(monkeypatch, "verified")
    assert run.phase == P.AWAITING_XSD_APPROVAL.value
    assert run.handoff_json.get("xsd_build_failed") is False          # flag cleared/false on a clean build
    assert freeze.get("build_status") == "verified"
    assert "xsd_build_failed" not in dict(events)


def test_post_xsd_advance_unverified_does_not_gate(monkeypatch):
    # "unverified" = no toolchain / can't build = a can't-check, NOT a failure — it must
    # not block approval (same addressee rule as the Phase-B verify path).
    run, events, freeze = _drive_post_xsd(monkeypatch, "unverified")
    assert run.handoff_json.get("xsd_build_failed") is False
    assert "xsd_build_failed" not in dict(events)


def test_post_xsd_advance_gates_on_revision_proposal(monkeypatch):
    events = []
    _patch_common(monkeypatch, events)
    monkeypatch.setattr(O, "_phase_freeze_xsd",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("froze during conversation!")))
    run = _Run(kind="xsd", handoff={"xsd_change_request": {"feedback": "delete Amount"}})
    run.phase = P.XSD_DISCOVERY.value
    scope = SimpleNamespace(revision_proposal={"summary": "breaks consumers",
                                               "options": [{"id": "deprecate"}], "recommended": "deprecate"})
    O._post_xsd_advance(None, run, {"xsd_scope": scope})
    assert run.phase == P.AWAITING_APPROACH_DECISION.value
    kinds = [k for k, _ in events]
    assert "revision_proposal" in kinds
    payload = dict(events)["revision_proposal"]
    assert payload["original_request"] == "delete Amount"     # context carried to the card


def test_post_xsd_advance_full_run_skips_tsd_gate_even_when_enforced(monkeypatch):
    # A "full" run is the standalone quick-start codegen console (POST /agentic/quick-start)
    # — "no BRD/TSD needed" by design. It must go straight to CODE_CHANGE regardless of the
    # ADR-0005 TSD approval gate's enforcement setting: routing it through the gate would
    # find no TSD for the change and permanently wedge it at AWAITING_TSD_APPROVAL, since
    # this run type never has a TSD a human could approve.
    events = []
    _patch_common(monkeypatch, events)
    monkeypatch.setattr(settings, "agentic_tsd_approval_gate", True)
    monkeypatch.setattr(settings, "agentic_tsd_approval_gate_enforce", True)
    monkeypatch.setattr(O, "_tsd_approval_gate",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("the TSD gate must not be consulted for a 'full' run")))
    run = _Run(kind="full", handoff={})
    run.phase = P.XSD_DISCOVERY.value
    O._post_xsd_advance(None, run, {"xsd_scope": None})
    assert run.phase == P.CODE_CHANGE.value
    assert not any(k in ("tsd_approval_needed", "tsd_approval_gate") for k, _ in events)


# ── prior document-only XSD assessment: pull-based + advisory ─────────────────────

def test_read_doc_serves_assessment_as_advisory():
    from app.agents.agentic_tools import read_doc
    ctx = RunContext(run_id="r", selected_repo_ids=["x"], doc_sections={
        "brd": {"1. Scope": "the scope"},
        "tsd": {},
        "assessment": {"Decision": "NOT REQUIRED — retries reuse ReqTransfer unchanged"},
    })
    outline = read_doc(ctx)
    assert "ASSESSMENT (document-only, ADVISORY)" in outline       # labeled in the outline
    body = read_doc(ctx, doc="assessment", heading="Decision")
    assert "NOT REQUIRED" in body
    assert "ADVISORY" in body and "do NOT inherit its conclusion" in body  # framing rides along
