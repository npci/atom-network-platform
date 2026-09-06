# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The sim_pack certification round (SIM-6) — greenfield, lean, in-process.

One round: derive the graded scope from the registry delta
(`cert_case_builder`, the same function every other consumer uses), build and
publish this round's pack layered on the active baseline (`sim_packs`),
execute every request VARIANT against the simulator runtime — the same
`runtime.handle` a partner's stack hits over HTTP — and grade the captured
exchange with `cert_assertions`. Every result row records which case AND
variant ran, which mode each side executed in, and which pack graded it
(Gate 3's recording claim; migration 0133).

Judgements:

* **No published baseline → the round refuses and persists nothing.**
  Layering on nothing would grade against a contract that says nothing;
  publishing the root is a one-time human act (`POST /sim/packs` + publish).
* An empty delta WITH an operator-supplied catalogue runs as
  `harness_baseline` scope against the baseline pack itself; an empty delta
  with no catalogue is an empty round — refused, nothing persisted.
* **Variants are MATERIALISED, not just identified.** Each variant's
  `input_data` is rendered into the pack's `request_template` at the
  registry field paths it names, so two variants of a case send genuinely
  different bytes — without this the §3.1 variant axis is decorative on the
  wire and every variant certifies the same request. A key that names no
  path in this document is RECORDED as unrendered on the result row, never
  silently dropped: silently ignoring the input that was supposed to
  differentiate a variant would let two "different" variants send identical
  bytes while the report claims they differed. The codec refuses to invent
  missing structure (see `core/wire/codec`), so an unrendered key is a real
  mismatch worth seeing.
* A simulator REFUSAL (validation_failed, no template, unknown_api) grades
  ERROR, not FAIL — it is this side's pack/template defect; failing the
  partner for it is the most damaging wrong answer available.
* **Two-sided by class (Gate 3).** Each side executes ONLY its own class:
  authority-initiated variants run here against the pack simulator;
  partner-initiated ones are announced to the bank (`cert_setup_notification`
  carrying the mode-selected alias and `?pack=`) and recorded as
  `not_reported` placeholders — one per VARIANT, not per API name, so
  completion and the deadline count what was actually expected. The run then
  stays RUNNING and `cert_join` finalizes it on the bank's reports or the
  suite deadline; that machinery is harness-agnostic and keys off the row
  markers alone, so nothing there needed changing. With no partner agent or
  the tunnel off, the whole scope is this side's and the run completes
  synchronously exactly as before.
* **Modes are resolved per run, never inherited** (I-6b/§3.6.4, the rules in
  `cert_modes`), and BOTH postures execute. `npci=simulator` calls the
  in-process pack simulator. `npci=application` DRIVES this platform's
  deployed application through the symmetric trigger contract (§3.6.1) —
  a real deployment has no control API — so the case becomes one the join is
  waiting on, its outcome arriving as the application's own outbound call
  through the tunnel. Without a configured `cert_trigger_url` there is
  nothing to drive, and the round REFUSES rather than running the simulator
  and stamping the result `application`: that is the §3.6.3 over-claim, a
  certificate asserting more than was verified.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping, Sequence
from uuid import uuid4

from app.core.config import settings
from app.core.wire.registry import codec_for
from app.models.phase_c import (
    CertDirection,
    CertRun,
    CertRunStatus,
    CertTestResult,
    CertTestStatus,
)
from app.services import cert_modes
from app.services.cert_assertions import FAIL, assertion_failures, evaluate_specs
from app.services.cert_case_builder import delta_messages, derive_round_scope
from app.services.sim_packs import builder
from app.services.simulator import resolver, runtime, store

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

__all__ = ["run_round"]

_DIRECTION = {"partner": CertDirection.PARTNER_TO_AUTHORITY}


def _demo_catalogue(db: "Session", change_id: str) -> list[dict]:
    """One labelled demo case per delta API — keeps the harness runnable
    before a real catalogue source is wired; `_origin` says so on every row."""
    return [
        {"case_id": f"TC_{msg.api_name}", "api": msg.api_name,
         "initiator": "npci", "expected_status": "PASS",
         "authority_batch": {"expected_rc": "00"}, "_origin": "demo"}
        for _, msg in sorted(delta_messages(db, change_id).items())
    ]


def _round_pack(db: "Session", *, change_id: str, run_number: int,
                baseline, variants: Sequence[Any]):
    """Build + publish this round's pack; reuse an identical earlier build
    (content address says it IS the same pack); a changed rebuild gets a new
    revision suffix — refs are immutable."""
    ref = f"{change_id}@r{run_number}"
    pack = builder.build_pack(db, change_id=change_id, pack_ref=ref,
                              base_pack_ref=baseline.pack_ref,
                              variants=variants)
    if pack is None:
        return None                     # empty delta — run against the baseline
    existing = store.get(db, ref)
    suffix = 1
    while existing is not None and existing.pack_id != pack.pack_id:
        suffix += 1
        ref = f"{change_id}@r{run_number}.{suffix}"
        pack = pack.model_copy(update={"pack_ref": ref})
        existing = store.get(db, ref)
    if existing is None:
        store.save_draft(db, pack, created_by="sim_pack_harness")
    return store.publish(db, ref)


def _materialise(template: str, input_data: Mapping[str, str],
                 codec) -> tuple[str, list[str]]:
    """Render a variant's inputs into the template at their field paths.

    Returns (body, unrendered_keys). An unparseable template is returned
    untouched with every key unrendered — that is a pack defect, and the
    ERROR-grading path above already treats our own defects as ours.
    """
    from app.core.wire.codec import CodecError

    try:
        doc = codec.parse(template)
    except CodecError:
        return template, sorted(input_data)
    unrendered = [path for path, value in sorted(input_data.items())
                  if codec.set_value(doc, path, str(value)) == 0]
    return codec.serialize(doc), unrendered


def _fire_authority_trigger(*, case_id: str, variant, cflow_id: str,
                            run_number: int, modes) -> bool:
    """Drive THIS platform's deployed application for one case (§3.6.1).

    `reply_via` is the alias the application answers on — never a URL, so no
    counterparty address is embedded in the system under test. Returns
    whether the trigger was accepted; the OUTCOME is not this call's to know.
    """
    from app.services.integration_testing.trigger import fire_trigger

    return fire_trigger(
        settings.cert_trigger_url, settings.cert_trigger_secret or None,
        test_case_id=case_id,
        cert_context={"cflow_id": cflow_id, "cert_attempt": run_number,
                      "initiator": "npci", **modes.as_dict()},
        case_data=dict(variant.input_data or {}),
        reply_via=f"a2a://{settings.integration_testing_simulator_alias}",
    )


async def _announce(db: "Session", *, change_id: str, partner_id: str,
                    cflow_id: str, run_number: int, scope, catalogue,
                    modes, graded_by) -> set[str]:
    """Tell the partner the scope, and return the variant ids IT owns.

    Publishes `simulator.endpoint` as an ALIAS selected by this run's NPCI
    mode, with `?pack=` binding the contract this round is graded against —
    so the bank-initiated direction certifies against the same pack as the
    authority-initiated one (SIM §3.3, ITA §3.3/I-6b).

    Returns an EMPTY set — the whole scope stays this side's — when the
    tunnel is off, no partner agent exists, or the send fails. A failed
    announcement must not strand the round awaiting reports that can never
    arrive; running the authority's own class and saying so is the honest
    degradation.
    """
    partner_class = {
        v.id for variants in scope.variants_by_case.values() for v in variants
        if (v.initiator or "authority") == "partner"
    }
    if not settings.integration_testing_enabled or not partner_class:
        return set()

    from app.models.phase_c import PartnerAgent

    partner = db.get(PartnerAgent, partner_id)
    if partner is None:
        logger.warning("sim_pack round %s r%d: %d partner-initiated variant(s) "
                       "but partner %s is not onboarded — executing this "
                       "side's class only", cflow_id, run_number,
                       len(partner_class), partner_id)
        return set()

    owned_cases = sorted({
        case_id for case_id, variants in scope.variants_by_case.items()
        if any(v.id in partner_class for v in variants)})
    case_list = [
        {"case_id": c.get("case_id"), "api": c.get("api"),
         "initiator": c.get("initiator") or "npci",
         "expected_status": c.get("expected_status")}
        for c in catalogue if c.get("case_id") in scope.case_ids
    ]

    from app.a2a_common import protocol as _proto
    from app.services.a2a_client import send_task_to_partner
    from app.services.cert_agent.setup import simulator_block

    sim_block = simulator_block(
        alias=cert_modes.alias_for(
            "npci", modes.npci,
            simulator_alias=settings.integration_testing_simulator_alias,
            application_alias=settings.integration_testing_application_alias),
        cflow_id=cflow_id, pack_ref=graded_by.pack_ref)
    try:
        await send_task_to_partner(
            partner=partner,
            task_type=_proto.A2ATaskType.CERT_SETUP_NOTIFICATION,
            payload={
                "summary": f"Certification round {run_number}: "
                           f"{len(scope.case_ids)} case(s), "
                           f"{len(owned_cases)} initiated by you.",
                "cases": sorted(scope.case_ids),
                "case_list": case_list,
                "simulator": sim_block,
                "cert_context": {"cflow_id": cflow_id,
                                 "cert_attempt": run_number,
                                 **modes.as_dict()},
            },
            db=db, change_request_id=change_id, cflow_id=cflow_id,
            cert_attempt=run_number)
    except Exception:
        logger.exception(
            "sim_pack round %s r%d: cert_setup_notification failed — running "
            "this side's class only rather than awaiting reports that cannot "
            "arrive", cflow_id, run_number)
        return set()

    # ITA I-6: the START SIGNAL for the partner-initiated half. The setup
    # notification announces the scope; nothing on the partner EXECUTES until
    # this arrives (their handler fires the certification trigger per case).
    # The `simulator` block rides along because its endpoint carries `?pack=`:
    # a start signal naming only a bare alias would have the partner's stack
    # exercise the ACTIVE BASELINE — every label says round N, the grading
    # says baseline, which is the §3.3 "certified against baseline" trap.
    try:
        await send_task_to_partner(
            partner=partner,
            task_type=_proto.A2ATaskType.CERT_EXECUTION_START,
            payload={
                "summary": (f"Begin partner-initiated execution: "
                            f"{len(owned_cases)} case(s); report each via "
                            f"cert_case_result (reporter=bank)."),
                "case_ids": owned_cases,
                "deadline_ms": int(float(settings.cert_suite_deadline_s) * 1000),
                "simulator_alias": sim_block.get("alias"),
                "simulator": sim_block,
                "cert_context": {"cflow_id": cflow_id,
                                 "cert_attempt": run_number,
                                 "initiator": "bank",
                                 **modes.as_dict()},
            },
            db=db, change_request_id=change_id, cflow_id=cflow_id,
            cert_attempt=run_number)
    except Exception:
        logger.exception(
            "sim_pack round %s r%d: cert_execution_start failed — the partner "
            "knows the scope but was never told to begin; their cases will "
            "expire at the suite deadline as not_reported (the honest record)",
            cflow_id, run_number)
    return partner_class


async def run_round(
    db: "Session",
    *,
    change_id: str,
    partner_id: str,
    role: str = "",
    test_data: Mapping[str, Any] | None = None,
    test_data_per_case: Mapping[str, Any] | None = None,
    dispatch_meta: Mapping[str, Any] | None = None,
) -> dict:
    test_data = dict(test_data or {})
    meta = dict(dispatch_meta or {})

    # I-6b: explicit per-run opt-in. `prior` is passed to state the rule at
    # the call site — application mode is never inherited from round N-1.
    try:
        modes = cert_modes.resolve(test_data.get("modes"), prior=None)
    except ValueError as exc:
        return {"skipped": True, "error": "bad_mode", "detail": str(exc)}
    if modes.npci == cert_modes.APPLICATION and not settings.cert_trigger_url:
        # §3.6.1: an application-mode side is DRIVEN by the trigger contract —
        # there is no control API on a real deployment. Without a trigger URL
        # there is nothing to drive, and running the in-process simulator
        # while stamping the result `application` is the §3.6.3 over-claim.
        return {
            "skipped": True, "error": "no_authority_trigger_url",
            "detail": ("npci_mode=application needs `cert_trigger_url` — the "
                       "deployed application is a subject under test, not a "
                       "test driver. Refusing rather than running the "
                       "simulator and labelling the result 'application'."),
        }

    baseline = store.active_baseline(db)
    if baseline is None:
        logger.warning("sim_pack round refused for change=%s: no published "
                       "baseline pack to layer on", change_id)
        return {"skipped": True, "error": "no_baseline",
                "detail": "publish the root pack first (POST /sim/packs, "
                          "then publish) — layering on nothing would grade "
                          "against a contract that says nothing"}

    prior = (db.query(CertRun)
             .filter(CertRun.change_request_id == change_id,
                     CertRun.partner_id == partner_id)
             .order_by(CertRun.run_number.desc()).first())
    run_number = (prior.run_number + 1) if prior else 1
    cflow_id = (prior.cflow_id if prior and prior.cflow_id
                else f"CF-{uuid4().hex[:12]}")

    catalogue = list(test_data.get("case_catalogue") or [])
    if not catalogue:
        # The change's own published cert workbook (Phase A's cert_test_cases
        # kit document) — the catalogue partners were told they would be
        # certified against. The demo fallback stays LAST: a real change with
        # a real workbook must never silently certify against demo cases.
        from app.services.cert_catalogue import case_catalogue_for_change

        catalogue = case_catalogue_for_change(db, change_id, role=role)
    if not catalogue:
        catalogue = _demo_catalogue(db, change_id)
    if not catalogue:
        return {"skipped": True, "error": "empty_scope",
                "detail": f"change {change_id} touched no registry rows and "
                          "no case catalogue was supplied — an empty round "
                          "certifies nothing"}

    scope = derive_round_scope(db, change_id=change_id, cflow_id=cflow_id,
                               run_number=run_number,
                               available_cases=catalogue)
    if scope.empty:
        return {"skipped": True, "error": "empty_scope",
                "detail": scope.result.summary()}

    pack_row = _round_pack(db, change_id=change_id, run_number=run_number,
                           baseline=baseline, variants=scope.result.variants)
    graded_by = pack_row or baseline

    run = CertRun(
        change_request_id=change_id, partner_id=partner_id, cflow_id=cflow_id,
        run_number=run_number, status=CertRunStatus.RUNNING,
        dispatched_by=meta.get("dispatched_by"),
        previous_run_id=meta.get("previous_run_id"),
        fix_notification_message_id=meta.get("fix_notification_message_id"),
        coverage={
            "summary": scope.result.summary(),
            "fallback": scope.result.fallback,
            "uncovered_apis": scope.result.uncovered_apis,
            "unconstrained_fields": scope.result.unconstrained_fields,
            "gaps": scope.result.gaps,
            "variants": len(scope.result.variants),
            "specs": len(scope.result.specs),
        },
        pack_ref=graded_by.pack_ref, pack_id=graded_by.pack_id,
        npci_mode=modes.npci, partner_mode=modes.partner,
    )
    # §3.6.3: what this run's pass actually proves, in the round's own record.
    run.coverage["evidence"] = modes.evidence()
    db.add(run)
    db.flush()

    api_of = {c.get("case_id"): c.get("api") for c in catalogue}
    counters = {"PASS": 0, "FAIL": 0, "SKIP": 0, "ERROR": 0}

    # Gate 3: announce the scope and hand the partner ITS class. Returns the
    # variant ids this side must NOT execute.
    # Cases this side TRIGGERED rather than executed: their outcome arrives
    # through the tunnel, so the join owns them exactly as it owns the
    # partner's class.
    triggered_owned: set[str] = set()
    partner_owned = await _announce(
        db, change_id=change_id, partner_id=partner_id, cflow_id=cflow_id,
        run_number=run_number, scope=scope, catalogue=catalogue,
        modes=modes, graded_by=graded_by)

    for case_id, variants in sorted(scope.variants_by_case.items()):
        for variant in variants:
            if variant.id in partner_owned:
                # The bank's report replaces this row (see
                # process_cert_case_result_report). Until it does the case is
                # NOT REPORTED — an ERROR-class placeholder, never a pass.
                counters["ERROR"] += 1
                db.add(CertTestResult(
                    cert_run_id=run.id, test_case_id=case_id,
                    direction=CertDirection.PARTNER_TO_AUTHORITY,
                    status=CertTestStatus.ERROR,
                    expected_response=dict(variant.expected or {}),
                    actual_response={
                        "not_reported": True,
                        "variant_id": variant.variant_id,
                        "reason": "partner-initiated case awaiting the bank's "
                                  "report",
                    },
                    pack_ref=graded_by.pack_ref, pack_id=graded_by.pack_id,
                    npci_mode=modes.npci, partner_mode=modes.partner,
                ))
                continue
            codec = codec_for(variant.wire_format or "xml")
            resolved = resolver.resolve(db, graded_by.pack_ref)
            entry = resolved.apis.get((api_of.get(case_id) or "").lower())
            body = (entry or {}).get("request_template")

            if modes.npci == cert_modes.APPLICATION:
                # §3.6.1: a deployed application is DRIVEN, not called. The
                # trigger says only "start"; the outcome arrives separately
                # as the application's real outbound call through the
                # tunnel, so this case becomes one the join is waiting on —
                # exactly like a partner-initiated one.
                accepted = await asyncio.to_thread(
                    _fire_authority_trigger, case_id=case_id, variant=variant,
                    cflow_id=cflow_id, run_number=run_number, modes=modes)
                counters["ERROR"] += 1
                db.add(CertTestResult(
                    cert_run_id=run.id, test_case_id=case_id,
                    direction=CertDirection.AUTHORITY_TO_PARTNER,
                    status=CertTestStatus.ERROR,
                    expected_response=dict(variant.expected or {}),
                    actual_response={
                        "not_reported": True,
                        "variant_id": variant.variant_id,
                        "triggered": accepted,
                        "reason": ("application-mode case triggered, awaiting "
                                   "its outbound call" if accepted else
                                   "application-mode case could NOT be "
                                   "triggered — it will never report"),
                    },
                    pack_ref=graded_by.pack_ref, pack_id=graded_by.pack_id,
                    npci_mode=modes.npci, partner_mode=modes.partner,
                ))
                triggered_owned.add(variant.id)
                continue

            status = CertTestStatus.PASS
            actual: dict[str, Any] = {"variant_id": variant.variant_id,
                                      "pack": graded_by.pack_ref}
            if body and variant.input_data:
                body, unrendered = _materialise(body, variant.input_data, codec)
                if unrendered:
                    actual["unrendered_inputs"] = unrendered
            if not body:
                status = CertTestStatus.ERROR
                actual["error"] = ("pack API carries no request template — "
                                   "nothing to execute; not the partner's "
                                   "failure")
            else:
                try:
                    reply = await runtime.handle(
                        db, body=body, pack=graded_by.pack_ref,
                        tc_id=case_id, variant_id=variant.variant_id)
                    actual.update({"rc": reply.rc, "scenario": reply.scenario})
                    outcomes = evaluate_specs(
                        scope.specs_by_variant.get(variant.id, []),
                        request_body=body, response_body=reply.content,
                        actual_code=reply.rc, codec=codec)
                    failures = assertion_failures(outcomes)
                    if failures:
                        status = CertTestStatus.FAIL
                        actual["assertion_failures"] = failures
                except runtime.SimRefusal as exc:
                    status = CertTestStatus.ERROR
                    actual["error"] = exc.payload
            counters[status.value.upper()] += 1
            db.add(CertTestResult(
                cert_run_id=run.id, test_case_id=case_id,
                direction=_DIRECTION.get(variant.initiator,
                                         CertDirection.AUTHORITY_TO_PARTNER),
                status=status, expected_response=dict(variant.expected or {}),
                actual_response=actual,
                pack_ref=graded_by.pack_ref, pack_id=graded_by.pack_id,
                npci_mode=modes.npci, partner_mode=modes.partner,
            ))

    run.total = sum(counters.values())
    run.passed = counters["PASS"]
    run.failed = counters["FAIL"]
    run.skipped = counters["SKIP"] + counters["ERROR"]
    # ITA-7/Gate 3: a run still owed partner reports stays RUNNING with no
    # completed_at — `cert_join` flips it on the last report or the suite
    # deadline. Completing it here would certify a suite whose partner half
    # never ran.
    _awaiting = bool(partner_owned or triggered_owned)
    run.status = CertRunStatus.RUNNING if _awaiting else CertRunStatus.COMPLETED
    run.completed_at = None if _awaiting else datetime.now(timezone.utc)
    db.commit()

    certified = run.failed == 0 and counters["ERROR"] == 0 and run.total > 0 \
        and run.passed == run.total
    summary = {
        # A run awaiting the partner is neither certified NOR failed — the
        # verdict is the join's to reach. `passed=None` is what the seam's
        # CertResult already means by "dispatched, not adjudicated"; calling
        # it False here would flip an assignment on a verdict nobody reached.
        "status": "awaiting_partner" if _awaiting else
                  ("certified" if certified else "failed"),
        "passed": None if _awaiting else certified,
        "awaiting_partner_cases": sorted({
            r.test_case_id for r in run.results
            if isinstance(r.actual_response, dict)
            and r.actual_response.get("not_reported")}) if _awaiting else [],
        "run_id": run.id, "cflow_id": cflow_id, "run_number": run_number,
        "pack_ref": graded_by.pack_ref, "pack_id": graded_by.pack_id,
        "total": run.total, "pass": run.passed, "fail": run.failed,
        "error": counters["ERROR"], "skip": counters["SKIP"],
        "coverage": run.coverage["summary"],
        **modes.as_dict(), "evidence": modes.evidence(),
    }
    logger.info("sim_pack round %s r%d: %s", cflow_id, run_number, summary)
    return summary
