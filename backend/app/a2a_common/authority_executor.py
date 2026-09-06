# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""the Authority platform `AgentExecutor` — JSON-RPC server-side dispatcher.

Receives an A2A `Task` from a partner, reads `task_type` + `payload`
from the message's data Part, persists the same `A2AMessage` audit
row the legacy `POST /a2a/tasks/send` endpoint would have created, and
dispatches to the existing handler functions in
`backend/app/api/a2a.py` and `backend/app/api/a2a_cert_handlers.py`.

Zero business logic is duplicated here — this module is glue. When
Slice 8 decommissions the legacy POST endpoint, the handler functions
will move into this file (or stay where they are with a thin shim).

Message data shape — partners send one structured Part with:
    {
      "task_type":         "change_acknowledgement",   // A2ATaskType value
      "change_id":         "<uuid>",                   // optional per task type
      "payload":           {...},                      // task-specific body
      "partner_api_key":   "a2a_..."                   // identifies caller
    }

Auth note (Slice 3): we look up the partner by `partner_api_key` in
the message data — same lookup the legacy `CurrentPartner` dependency
does, just from a different envelope. Slice 5/6 replaces this with
proper Bearer JWT; the receiving end will validate the token and
inject a `PartnerAgent` into the executor context.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from google.protobuf import json_format, struct_pb2
from sqlalchemy.orm import Session

from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types.a2a_pb2 import Part

from app.core.config import settings
from app.core.error_taxonomy import client_safe_detail

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _part_to_dict(part: Part) -> dict:
    """Decode a structured A2A `Part` back to a Python dict."""
    if part.HasField("data") and part.data.HasField("struct_value"):
        return json_format.MessageToDict(part.data.struct_value)
    return {}


def _dict_to_part(payload: dict) -> Part:
    """Wrap a Python dict as a structured A2A `Part`."""
    s = struct_pb2.Struct()
    json_format.ParseDict(payload, s)
    v = struct_pb2.Value()
    v.struct_value.CopyFrom(s)
    part = Part()
    part.data.CopyFrom(v)
    return part


def _receipt_payload(*, task_id: str, status: str, task_type: str,
                     result: str | dict) -> dict:
    """The `a2a-task-receipt` body — ITA-2 (blocker B1).

    A handler may return a STRING (every pre-existing handler; the receipt
    shape for them is unchanged, key for key) or a DICT of structured data
    merged into the receipt — which is what lets the reverse tunnel carry an
    `http_exchange_response` home instead of flattening it into prose.

    The executor keeps ownership of the three identity keys: a handler must
    not be able to reclassify the receipt's `status` or re-address its
    `task_id`/`task_type`. That costs the tunnel nothing — its payload nests
    the HTTP status under `response.status` (ITA plan §5.2), never at top
    level.
    """
    if isinstance(result, dict):
        return {
            **result,
            "task_id":   task_id,
            "status":    status,
            "task_type": task_type,
        }
    return {
        "task_id":   task_id,
        "status":    status,
        "task_type": task_type,
        "message":   result,
    }


class _AsyncBackgroundTasks:
    """Drop-in replacement for FastAPI's `BackgroundTasks` that schedules
    work via `asyncio.create_task` so the SDK executor can call into the
    existing handlers (which expect a BackgroundTasks instance) without
    pulling in the FastAPI request lifecycle.

    Mismatch: FastAPI BackgroundTasks runs AFTER the response is sent.
    Here, tasks run inside the SDK Task lifecycle — they may complete
    before the executor returns or after, depending on event loop
    scheduling. The handlers we use this for (auto_draft_background,
    cert triage) are idempotent, so the timing shift is safe.
    """

    def add_task(self, func, *args, **kwargs) -> None:
        if asyncio.iscoroutinefunction(func):
            asyncio.create_task(func(*args, **kwargs))
        else:
            asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))


# ── Executor ─────────────────────────────────────────────────────────────────

class AuthorityAgentExecutor(AgentExecutor):
    """Dispatch incoming A2A Tasks to the Authority platform's existing handlers.

    One executor instance is shared across all requests by the SDK; do
    not store per-request state on `self`. The DB session is opened
    fresh inside `execute()` and closed in the `finally` block.
    """

    async def execute(
        self, context: RequestContext, event_queue: EventQueue,
    ) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()

        # ── 1. parse incoming message ──
        data = self._extract_data(context)
        if not data:
            await self._fail(updater, "Empty or malformed message data")
            return

        # Phase 1 (protocol v1): read the envelope tolerantly — legacy
        # (pre-v1) messages with no message_id/correlation_id still parse.
        from app.a2a_common.protocol import read_envelope
        env = read_envelope(data)
        task_type_str = env.task_type
        change_id = env.change_id
        payload = env.payload

        if not task_type_str:
            await self._fail(updater, "Missing 'task_type' in message data",
                              error_code="missing_envelope_headers")
            return

        # A10 (architecture review Important #14, "No Inbound Schema
        # Validation on A2A JSON-RPC Payloads") — `read_envelope()` is
        # deliberately tolerant so legacy (pre-v1) messages parse without
        # error, but that means ANY string reaches this point as
        # `task_type`, valid or not. Reject unknown task types with the
        # existing `unknown_task_type` ErrorCode (protocol.py's `ErrorCode`
        # enum already defines it — it was defined but never raised on this
        # path) rather than letting a malformed/unexpected type reach a
        # handler that assumes one of the 28 known types. This is the
        # narrow, additive strict check the review recommends; it does not
        # yet enforce `Envelope`'s full `extra="forbid"` schema (that
        # requires coordinating the migration-window cutover across all
        # partners simultaneously — tracked separately, see
        # docs/ARCHITECTURE_REVIEW_REMEDIATION.md §A10 for the phased plan).
        from app.a2a_common.protocol import A2ATaskType, ErrorCode
        try:
            A2ATaskType(task_type_str)
        except ValueError:
            logger.warning(
                "SECURITY_EVENT event=malformed_payload severity=medium "
                "reason=unknown_task_type task_type=%r change_id=%s",
                task_type_str, change_id,
            )
            await self._fail(
                updater, f"Unknown task_type: {task_type_str!r}",
                error_code=ErrorCode.UNKNOWN_TASK_TYPE.value,
            )
            return

        # Finding #14 (architecture review Important, "No Inbound Schema
        # Validation on A2A JSON-RPC Payloads") — SHADOW-MODE only. Validates
        # the whole envelope against the STRICT `Envelope` model (extra=
        # "forbid", required message_id/from, task_type restricted to the
        # frozen A2ATaskType enum) and logs what WOULD have failed, without
        # ever blocking the request. This is deliberately measure-only: the
        # codebase's own migration-window comments and tests
        # (test_read_envelope_tolerates_legacy_message) confirm partners
        # still on protocol_version="legacy" send envelopes missing fields
        # this strict model requires — enforcing today would reject live,
        # legitimate traffic. Toggle via settings.a2a_strict_envelope_
        # validation; see docs/ARCHITECTURE_REVIEW_REMEDIATION.md §A10 for
        # the phased cutover plan that a2a_strict_envelope_validation_enforce
        # gates. Fail-open in TWO senses: (a) a schema mismatch never blocks
        # the request, and (b) an error IN this shadow check itself (e.g. a
        # pydantic import issue) is swallowed so it can never break the real
        # dispatch path below.
        if getattr(settings, "a2a_strict_envelope_validation", True):
            try:
                from pydantic import ValidationError
                from app.a2a_common.protocol import Envelope
                try:
                    Envelope.model_validate(data)
                except ValidationError as ve:
                    # str(ve) can be long; cap it so a malformed payload
                    # can't turn this into a large/sensitive-data log write
                    # (EA_Skills.md anti-pattern: "excessive logging of
                    # sensitive or large payload data").
                    _errs = str(ve)[:800]
                    logger.warning(
                        "SECURITY_EVENT event=envelope_schema_violation severity=low "
                        "task_type=%r change_id=%s decision=shadow_only errors=%s",
                        task_type_str, change_id, _errs,
                    )
                    if getattr(settings, "a2a_strict_envelope_validation_enforce", False):
                        await self._fail(
                            updater, "Envelope failed strict schema validation.",
                            error_code=ErrorCode.PAYLOAD_VALIDATION_ERROR.value,
                        )
                        return
            except Exception as _shadow_exc:  # noqa: BLE001 — shadow check must never
                # break the real dispatch path below, even if pydantic/Envelope
                # import itself fails for some reason.
                logger.debug("envelope shadow validation skipped: %s", _shadow_exc)

        # ── 2. resolve partner from auth middleware ──
        # Slice 2 of the security hardening: SdkAuthMiddleware validates the
        # Bearer JWT + A2ASession row and stashes the resolved PartnerAgent
        # on a contextvar before the SDK handler runs. We pull it out here
        # rather than re-doing the lookup. If the contextvar is empty, the
        # middleware wasn't wired — refuse the task fast (this is a
        # misconfiguration, not a partner-side error).
        from app.a2a_common.sdk_auth_middleware import (
            AUTH_CONTEXT, get_audit_meta,
        )
        auth_ctx = AUTH_CONTEXT.get()
        if auth_ctx is None or auth_ctx.partner is None:
            await self._fail(
                updater,
                "Authentication context missing — SdkAuthMiddleware not "
                "wrapping the SDK sub-app. Refusing unauthenticated task.",
            )
            return
        partner = auth_ctx.partner
        audit_meta = get_audit_meta()

        # ── 3. dispatch (sync handlers run inline; safe — short DB ops) ──
        # Imports are inside the function so this module can be imported
        # without dragging the whole FastAPI app graph (test harness).
        from app.core.database import SessionLocal
        from app.models.phase_c import (
            A2ADirection, A2AMessage, A2ATaskType, CertRun, CertRunStatus,
        )
        from app.models.base import generate_uuid, utcnow
        from app.a2a_common.authority_handlers import (
            process_milestone_update,
            process_readiness_declaration,
            process_proposal_acknowledged,
            process_change_acknowledgement,
            process_counter_proposal,
            process_counter_decision,
            process_blocker,
            process_emergency_issue,
            ensure_cp_for_query,
            process_cert_test_response,
            process_cert_case_result_report,
            process_cert_acknowledgement,
            process_defect_notice,
            process_defect_resolution,
            process_cert_status_update,
            process_cert_waiver_request,
            process_cert_run_abort,
            process_cert_fix_notification,
        )
        from app.services.negotiation_service import auto_draft_background

        # Validate task_type against the union of the protocol v1 enum and the
        # legacy enum (cert types migrate to protocol in later phases). We keep
        # `task_type` as the raw wire STRING — both enums are str-enums, so the
        # `==` comparisons in dispatch match by value against either. The audit
        # column stores it verbatim.
        import app.a2a_common.protocol as proto
        _accepted = {t.value for t in proto.A2ATaskType} | {t.value for t in A2ATaskType}
        if task_type_str not in _accepted:
            # Rejected before the audit row exists, so the log line is the only
            # trace an operator gets. Retired names land here too (e.g. the
            # `readiness_declaration` -> `cert_readiness_declaration` rename),
            # where it means the sender is stale rather than wrong.
            logger.warning(
                "Rejected unknown task_type '%s' from partner=%s — "
                "unregistered or retired; sender may need updating.",
                task_type_str, getattr(partner, "id", "?"),
            )
            await self._fail(
                updater, f"Invalid task_type '{task_type_str}'.",
                proto.ErrorCode.UNKNOWN_TASK_TYPE.value,
            )
            return
        task_type = task_type_str

        db: Optional[Session] = None
        message: Optional[A2AMessage] = None
        try:
            db = SessionLocal()

            # PTNR-F20: resolve the change BEFORE anything writes, and report a
            # miss as a miss.
            #
            # `a2a_messages.change_request_id` carries a FK to
            # `change_requests.id`, so an unknown change_id made the audit-row
            # INSERT below raise an IntegrityError, which the broad handler at
            # the bottom of this method rendered — correctly, for its own
            # purposes — as ErrorCategory.RESOURCE_ACCESS: "a downstream
            # resource was unavailable". That asserts something FALSE about this
            # platform's infrastructure and omits the only fact the caller needs.
            #
            # It cost a real false outage report between the two organisations:
            # the partner read that string, reproduced it three times, and filed
            # a wire-outage report against this side for a fault that did not
            # exist. The discriminator was row existence, not id format — a
            # well-formed UUID that simply is not present failed identically.
            #
            # NOT a loosening of `client_safe_detail`. That guard is right and
            # stays: `exc` down there is routinely a SQLAlchemy error carrying
            # table names, columns and SQL, and it crosses a trust boundary into
            # an artifact the partner receives. An unknown change_id is the
            # CALLER'S OWN INPUT echoed back — it names no table, column or
            # statement — so it is safe to state plainly, and it is only safe
            # because we resolve it here rather than letting the exception
            # describe it.
            if change_id:
                from app.models.change_request import ChangeRequest as _CR
                if db.get(_CR, change_id) is None:
                    logger.warning(
                        "SECURITY_EVENT event=unknown_change_id severity=low "
                        "task_type=%r change_id=%r partner=%s",
                        task_type_str, change_id, getattr(partner, "id", "?"),
                    )
                    await self._fail(
                        updater, f"Unknown change_id '{change_id}'.",
                        proto.ErrorCode.UNKNOWN_ID.value,
                    )
                    return

            # Persist the audit row — identical shape to the legacy path so
            # downstream queries don't care which wire delivered the task.
            # Slice 8 populates the new audit columns from `audit_meta`
            # set by SdkAuthMiddleware: caller_ip, jwt_sub/iat/exp, mTLS
            # fingerprint (Slice 6 will fill that one).
            # T2 (THREAT_MODEL.md, non-repudiation) — SdkHmacMiddleware
            # (outer, runs before this executor) verifies the signature and
            # stashes it plus the key version on a contextvar this async
            # call chain inherits.
            from app.a2a_common.sdk_hmac_middleware import get_hmac_audit_meta
            hmac_meta = get_hmac_audit_meta()

            # T1 (THREAT_MODEL.md, at-rest integrity) — hash the EXACT
            # object being persisted as `payload` below, computed with a
            # canonical (sorted-key, no-whitespace) serialization so a
            # later read can recompute the SAME hash from the SAME column
            # deterministically. This anchors integrity to "what this
            # platform stored," not to the wire bytes (which have already
            # been re-shaped by protobuf/JSON decoding by this point) —
            # detecting AT-REST tampering (a row edited after receipt) is
            # the goal, not re-proving in-transit integrity (the HMAC
            # envelope already did that, at the wire-body layer, before
            # this function ever ran).
            import hashlib
            import json as _json
            _payload_hash = hashlib.sha256(
                _json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()

            message = A2AMessage(
                id=generate_uuid(),
                change_request_id=change_id,
                partner_id=partner.id,
                direction=A2ADirection.INBOUND,
                task_type=task_type,
                # Persist the full A2A wrapper so admin logs show the
                # same shape on outbound + inbound rows (handler
                # dispatch below still reads `payload = data["payload"]`
                # for business logic).
                payload=data,
                status="submitted",
                created_at=utcnow(),
                caller_ip=audit_meta.get("caller_ip"),
                jwt_sub=audit_meta.get("jwt_sub"),
                jwt_iat=audit_meta.get("jwt_iat"),
                jwt_exp=audit_meta.get("jwt_exp"),
                client_cert_fingerprint=audit_meta.get("client_cert_fingerprint"),
                payload_sha256=_payload_hash,
                hmac_signature=hmac_meta.hmac_signature,
                hmac_key_version=hmac_meta.hmac_key_version,
            )
            db.add(message)
            db.commit()
            db.refresh(message)

            logger.info(
                "A2A SDK task received: task_id=%s msg_id=%s corr=%s "
                "partner=%s type=%s change=%s caller_ip=%s proto=%s",
                message.id, env.message_id, env.correlation_id,
                partner.name, task_type, change_id,
                audit_meta.get("caller_ip"), env.protocol_version,
            )

            # State-machine guard (§7.14): while the cert run is ABORTED, no
            # further cert lifecycle messages are accepted for this
            # change+partner — reject with invalid_state_transition.
            # Scoped to the LATEST run: an aborted attempt is terminal for that
            # attempt, but the Authority starting a fresh run re-opens the channel — an
            # old aborted row must not brick cert for the change forever.
            _CERT_TYPES = {
                proto.A2ATaskType.CERT_CONFIG_SUBMISSION.value,
                proto.A2ATaskType.CERT_TEST_PREPARATION.value,
                proto.A2ATaskType.CERT_CASE_RESULT.value,
                proto.A2ATaskType.CERT_VERDICT_DISPUTE.value,
                proto.A2ATaskType.CERT_WAIVER_REQUEST.value,
                proto.A2ATaskType.CERT_FIX_NOTIFICATION.value,
                proto.A2ATaskType.CERT_STATUS_REQUEST.value,
            }
            if change_id and task_type in _CERT_TYPES:
                latest_run = (
                    db.query(CertRun)
                    .filter(
                        CertRun.change_request_id == change_id,
                        CertRun.partner_id == partner.id,
                    )
                    .order_by(CertRun.started_at.desc())
                    .first()
                )
                if latest_run is not None and latest_run.status == CertRunStatus.ABORTED:
                    message.error_code = proto.ErrorCode.INVALID_STATE_TRANSITION.value
                    message.status = "failed"
                    db.commit()
                    await self._fail(
                        updater,
                        "Cert run is ABORTED; no further cert messages accepted.",
                        proto.ErrorCode.INVALID_STATE_TRANSITION.value,
                    )
                    return

            # Dispatch to the same handler the legacy router would have called.
            bg = _AsyncBackgroundTasks()
            result_msg = "Task received and queued for processing"

            # ── Freeze gate (negotiation_flow): once the final kit version
            # ships, the change is frozen — reject inbound COUNTER-PROPOSALS
            # (term changes are closed). Generic + cert QUERIES stay open so the
            # partner can keep asking basic clarifying questions against the
            # final kit; EMERGENCY_ISSUE remains the channel for blockers.
            frozen = False
            if change_id and task_type == A2ATaskType.COUNTER_PROPOSAL:
                from app.models.change_request import ChangeRequest as _CR
                _cr = db.get(_CR, change_id)
                if _cr is not None and getattr(_cr, "negotiation_frozen_at", None) is not None:
                    frozen = True

            # ── Revision-hold gate: once a round closes and a kit revision is
            # being prepared (plan exists, not yet shipped), partners can't raise
            # new COUNTER-PROPOSALS — the terms are a moving target. Generic +
            # cert QUERIES stay open (a partner can always ask a clarifying
            # question). The hold clears when the new version ships;
            # EMERGENCY_ISSUE is unaffected.
            revision_hold = False
            if not frozen and change_id and task_type == A2ATaskType.COUNTER_PROPOSAL:
                from app.models.kit_revision_plan import KitRevisionPlan, RP_STATUS_SHIPPED
                _active_plan = (
                    db.query(KitRevisionPlan)
                    .filter(
                        KitRevisionPlan.change_request_id == change_id,
                        KitRevisionPlan.status != RP_STATUS_SHIPPED,
                    )
                    .first()
                )
                if _active_plan is not None:
                    revision_hold = True

            if frozen:
                message.status = "failed"
                db.commit()
                result_msg = (
                    "Negotiation for this change is frozen — the final Product Kit "
                    "version has shipped and no further counter-proposals are "
                    "accepted. You can still raise clarifying queries, or an "
                    "Emergency Issue if work is blocked."
                )
            elif revision_hold:
                message.status = "failed"
                db.commit()
                result_msg = (
                    "the Authority is preparing a revised Product Kit for this change. "
                    "Counter-proposals are on hold until the new version ships — "
                    "please review the updated kit when it arrives. You can still "
                    "raise clarifying queries in the meantime."
                )
            elif task_type == A2ATaskType.EMERGENCY_ISSUE and change_id and payload:
                result_msg = process_emergency_issue(
                    partner.id, change_id, payload, message, db,
                )
            # "status_update" is the legacy wire name for milestone_update —
            # accept both so an in-flight message from an un-upgraded partner
            # is processed instead of acked-and-dropped.
            elif task_type in (proto.A2ATaskType.MILESTONE_UPDATE.value, "status_update") and change_id and payload:
                result_msg = process_milestone_update(
                    partner.id, change_id, payload, message, db,
                )
            elif task_type == A2ATaskType.CERT_READINESS_DECLARATION and change_id:
                result_msg = process_readiness_declaration(
                    partner.id, change_id, message, db,
                )
                # If readiness was accepted AND the partner shipped role +
                # test_data, schedule the cert orchestrator (background)
                # so the partner's HTTP call returns quickly while the
                # LLM cert run unfolds async.
                if message.status == "completed":
                    outer = data or {}
                    inner = outer.get("payload") if isinstance(outer.get("payload"), dict) else outer
                    inner = inner or {}
                    role = str(inner.get("role") or "").strip()
                    td   = inner.get("test_data") if isinstance(inner.get("test_data"), dict) else {}
                    # Slice 3: optional per-case overrides keyed by tc_id.
                    # Each value is itself a dict of the same shape the
                    # cert-agent /test-cases/{id} PUT accepts.
                    td_pc = inner.get("test_data_per_case") if isinstance(inner.get("test_data_per_case"), dict) else {}
                    if role or td or td_pc:
                        # Resolves the harness the active domain pack declares
                        # (cert-agent REST or the precert engine). A domain with
                        # no certifier makes this a logged no-op.
                        from app.services.certification_dispatch import run_certification
                        bg.add_task(run_certification, change_id, partner.id, role, td or {}, td_pc or {})
                        result_msg = (
                            f"Readiness accepted; cert run starting "
                            f"(role={role or 'unspecified'}, per_case={len(td_pc)})"
                        )
            elif task_type == A2ATaskType.PROPOSAL_ACKNOWLEDGED and change_id:
                result_msg = await process_proposal_acknowledged(
                    partner.id, change_id, message, db,
                )
            elif task_type == A2ATaskType.CHANGE_ACKNOWLEDGEMENT and change_id:
                result_msg = process_change_acknowledgement(
                    partner.id, change_id, message, db,
                )
            elif task_type == A2ATaskType.COUNTER_PROPOSAL and change_id and payload:
                result_msg = process_counter_proposal(
                    partner.id, change_id, payload, message, db,
                )
                # After the CP is committed, classify it against the BRD and
                # add it to the real-time cross-partner cluster (async, background).
                if message.status == "completed":
                    from app.services.negotiation_extended import classify_and_cluster_background
                    # Find the CP we just created so we can pass its id
                    from app.models.phase_c import CounterProposal as _CP
                    _inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
                    _cp_wire_id = (_inner or {}).get("counter_proposal_id")
                    if _cp_wire_id:
                        _cp = db.query(_CP).filter(
                            _CP.change_request_id == change_id,
                            _CP.partner_id == partner.id,
                            _CP.counter_proposal_id == _cp_wire_id,
                        ).first()
                        if _cp:
                            bg.add_task(classify_and_cluster_background, _cp.id)
                # Feasibility resolver for counter-proposals.
                from app.agents.feasibility_resolver import auto_resolve_background
                bg.add_task(auto_resolve_background, message.id, change_id)
            elif task_type == A2ATaskType.COUNTER_DECISION and change_id and payload:
                result_msg = process_counter_decision(
                    partner.id, change_id, payload, message, db,
                )
            elif task_type == A2ATaskType.BLOCKER and change_id and payload:
                result_msg = process_blocker(
                    partner.id, change_id, payload, message, db,
                )
                # Run the feasibility resolver on blockers too. Previously only
                # QUERY / COUNTER_PROPOSAL got AI triage + an escalation ticket, so a
                # blocker — which the partner flags critical/high and which STOPS their
                # implementation — reached the PM with less support than a routine
                # question, and never reached Risk / InfoSec / Tech at all.
                # Deliberately NO classify_and_cluster_background: a blocker is not a spec
                # negotiation and must never be auto-rejected against BRD mandatories.
                from app.agents.feasibility_resolver import auto_resolve_background
                bg.add_task(auto_resolve_background, message.id, change_id)
            elif task_type in (A2ATaskType.QUERY, A2ATaskType.CERT_QUERY) and change_id and payload:
                # Protocol v1 folds the old cert_query task_type into query via
                # the payload `phase` field. A query is cert-channel if
                # phase=="cert" (or the legacy task_type=='cert_query' arrives).
                phase = str((payload or {}).get("phase") or "").strip().lower()
                if task_type == A2ATaskType.CERT_QUERY or phase == "cert":
                    # Cert-channel clarification — auto-draft pipeline, routed to
                    # a kind='cert' thread so Agent Messaging keeps the inbox
                    # visually separate from general Phase C Q&A.
                    bg.add_task(auto_draft_background, message.id, "cert")
                    result_msg = "Cert query received — AI response draft being generated"
                else:
                    # General query — unified negotiation flow (negotiation_flow).
                    # The thread-recording side effect is NOT optional: the PM's
                    # approve-and-respond endpoint requires the NegotiationThread +
                    # partner message to exist (404s without it).
                    from app.services.negotiation_service import record_partner_query
                    record_partner_query(message, db, "general")
                    # Post-freeze queries are generic clarifications, not term
                    # negotiations — no more kit versions can ship, so creating a
                    # CounterProposal + running clustering on them wrongly surfaces
                    # them as "1 open CP / open counter" on the PM's negotiation
                    # hub. Skip the CP-creation path once frozen; keep the
                    # feasibility resolver so the PM still gets a suggested reply.
                    from app.models.change_request import ChangeRequest as _CR
                    _ch = db.get(_CR, change_id)
                    _frozen = _ch is not None and getattr(_ch, "negotiation_frozen_at", None) is not None
                    if not _frozen:
                        # Treat every query as a negotiation item so auto-reject +
                        # clustering run on it too (not just structured counters),
                        # then the feasibility resolver produces the PM-facing draft.
                        cp = ensure_cp_for_query(partner.id, change_id, message, db)
                        if cp is not None:
                            from app.services.negotiation_extended import classify_and_cluster_background
                            bg.add_task(classify_and_cluster_background, cp.id)
                    from app.agents.feasibility_resolver import auto_resolve_background
                    bg.add_task(auto_resolve_background, message.id, change_id)
                    result_msg = (
                        "Post-freeze clarification received — routing to PM"
                        if _frozen else
                        "Query received — running negotiation pipeline"
                    )
            elif task_type == A2ATaskType.CERT_STATUS_UPDATE and change_id and payload:
                # Cert lifecycle status (received/deployed/tested/ready).
                # Linear status flip on the assignment row.
                result_msg = process_cert_status_update(
                    partner.id, change_id, payload, message, db,
                )
                # Final 'ready_for_certification' carries role + test_data
                # and triggers the cert orchestrator (same pipeline used
                # by the legacy READINESS_DECLARATION path).
                if (
                    message.status == "completed"
                    and (payload or {}).get("status") == "ready_for_certification"
                ):
                    role = str((payload or {}).get("role") or "").strip()
                    td = (payload or {}).get("test_data") if isinstance((payload or {}).get("test_data"), dict) else {}
                    # Slice 3: optional per-TC overrides on this lifecycle path too
                    td_pc = (payload or {}).get("test_data_per_case") if isinstance((payload or {}).get("test_data_per_case"), dict) else {}
                    if role or td or td_pc:
                        # Resolves the harness the active domain pack declares
                        # (cert-agent REST or the precert engine). A domain with
                        # no certifier makes this a logged no-op.
                        from app.services.certification_dispatch import run_certification
                        bg.add_task(run_certification, change_id, partner.id, role, td or {}, td_pc or {})
            elif task_type == A2ATaskType.CERT_TEST_RESPONSE and payload:
                result_msg = process_cert_test_response(payload, message, db, bg)
            elif task_type == A2ATaskType.CERT_ACKNOWLEDGEMENT:
                result_msg = process_cert_acknowledgement(payload, message, db)
            elif task_type == A2ATaskType.DEFECT_NOTICE:
                result_msg = process_defect_notice(payload, message, db)
            elif task_type == A2ATaskType.DEFECT_RESOLUTION:
                result_msg = process_defect_resolution(payload, message, db)
            elif task_type == proto.A2ATaskType.CERT_WAIVER_REQUEST.value and change_id:
                result_msg = process_cert_waiver_request(
                    partner.id, change_id, payload, message, db,
                )
            elif task_type == proto.A2ATaskType.CERT_RUN_ABORT.value and change_id:
                result_msg = process_cert_run_abort(
                    partner.id, change_id, payload, message, db,
                )
            elif task_type == proto.A2ATaskType.CERT_FIX_NOTIFICATION.value and change_id:
                result_msg = process_cert_fix_notification(
                    partner.id, change_id, payload, message, db, bg=bg,
                )
            elif task_type == proto.A2ATaskType.HTTP_EXCHANGE_REQUEST.value:
                # ITA-4 (reverse tunnel): resolve the alias against OUR
                # allowlist and perform the HTTP call — typically the
                # Simulator's callback API. `perform_exchange` never raises;
                # every failure is a structured §5.2 error the far side can
                # assert on. It blocks up to the 60s target ceiling, so it is
                # dispatched off the loop; the returned DICT rides the ITA-2
                # receipt merge home instead of being flattened into prose.
                from app.services.integration_testing.egress import perform_exchange

                _body = payload if isinstance(payload, dict) else {}
                if "exchange_id" not in _body and isinstance(_body.get("payload"), dict):
                    # Same envelope-or-inner tolerance as _inner() in the
                    # handlers: the exchange fields live one level down when
                    # the full envelope reaches this branch.
                    _body = _body["payload"]
                result_msg = await asyncio.to_thread(perform_exchange, _body)
                message.status = "completed"
                db.commit()
                # I-9: one telemetry row per hop, best effort (never raises).
                from app.services.integration_testing.observability import (
                    record_from_wire,
                )

                record_from_wire(db, direction="egress", request_payload=_body,
                                 result=result_msg)
            elif task_type == proto.A2ATaskType.CERT_CASE_RESULT.value and change_id:
                # ITA I-6 (§3.7): a `reporter: "bank"` result is THE result for
                # a partner-initiated case — upserted onto the run. Echoes and
                # reporter-less acks keep the old acknowledgement verbatim.
                result_msg = process_cert_case_result_report(
                    partner.id, change_id, payload, message, db, bg=bg,
                )
            elif task_type in (
                proto.A2ATaskType.CERT_CONFIG_SUBMISSION.value,
                proto.A2ATaskType.CERT_TEST_PREPARATION.value,
                proto.A2ATaskType.CERT_VERDICT_DISPUTE.value,
                proto.A2ATaskType.CERT_STATUS_REQUEST.value,
            ):
                # Protocol v1 cert lifecycle messages recognized + audited.
                # Rich handling (config persistence, status-report generation,
                # dispute re-triage) is deferred to Phase 6 with the cert UI.
                message.status = "completed"
                db.commit()
                result_msg = f"{task_type} received"
            elif task_type == A2ATaskType.ECHO:
                # Settings-button health probe. Zero side effects beyond
                # the audit row already written above — the round-trip
                # itself is the value (auth + HMAC + dispatch all
                # exercised). Marked completed so audit logs distinguish
                # it from genuine submissions.
                message.status = "completed"
                db.commit()
                result_msg = "echo_ok"

            # Compute latency_ms from middleware-stamped start time, if
            # available. Falls back to None when the middleware was
            # bypassed (e.g. test harness).
            started_ns = audit_meta.get("request_started_ns")
            if started_ns is not None:
                message.latency_ms = max(
                    0, (time.perf_counter_ns() - started_ns) // 1_000_000
                )
                db.commit()

            # ── 3. emit response artifact ──
            # Slice 25 — persist the response body alongside the request
            # so the admin A2A logs UI can render both halves of the
            # round-trip.
            # ITA-2: string handlers keep the four-key receipt byte for byte;
            # a dict-returning handler has its keys merged (identity keys
            # stamped by the executor — see _receipt_payload).
            response_payload = _receipt_payload(
                task_id=message.id,
                status=message.status,
                task_type=task_type,   # already the raw wire string
                result=result_msg,
            )
            try:
                message.response_body = response_payload
                db.commit()
            except Exception:  # noqa: BLE001 — never fail the artifact emit
                db.rollback()
            await updater.add_artifact(
                parts=[_dict_to_part(response_payload)],
                name="a2a-task-receipt",
                last_chunk=True,
            )
            await updater.complete()

        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "npci_a2a_executor_error task_type=%s change=%s",
                task_type_str, change_id,
            )
            # SCR #6: this handler wraps DB commits and the process_* dispatch,
            # so `exc` is routinely a SQLAlchemy error carrying table names,
            # column names and the SQL statement. Everything below crosses a
            # TRUST BOUNDARY — `_fail` puts the text in an artifact returned to
            # the PARTNER's agent, and response_body is rendered in the admin
            # UI — so the raw string must not be used. The full traceback is
            # already captured by logger.exception above.
            safe_detail = client_safe_detail(exc)

            # Best-effort: tag the audit row with the failure so the
            # error_code column has structured data for ops queries.
            # Slice 25 — also stash the error in `response_body` so the
            # admin UI shows the same shape inbound rows get on success.
            if db is not None and message is not None:
                try:
                    message.error_code = proto.ErrorCode.EXECUTOR_ERROR.value
                    message.status = "failed"
                    message.response_body = {"error": f"Executor error: {safe_detail}"}
                    db.commit()
                except Exception:  # noqa: BLE001
                    db.rollback()
            await self._fail(
                updater, f"Executor error: {safe_detail}", proto.ErrorCode.EXECUTOR_ERROR.value,
            )
        finally:
            if db is not None:
                db.close()

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue,
    ) -> None:
        """Cancellation is not yet wired — the platform's task handlers
        complete synchronously, so a cancel arriving mid-execute is a
        no-op. Slice 7+ wires this when long-running cert triage moves
        through the executor."""
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()

    # ── private ────────────────────────────────────────────────────────────

    def _extract_data(self, context: RequestContext) -> dict[str, Any]:
        """Pull the first structured Part out of the incoming message."""
        if context.message is None:
            return {}
        for part in context.message.parts:
            if part.HasField("data"):
                return _part_to_dict(part)
        return {}

    async def _fail(
        self, updater: TaskUpdater, reason: str, error_code: str | None = None,
    ) -> None:
        """Mark the Task FAILED with a structured error artifact. `error_code`
        is a protocol-v1 ErrorCode value (§10) that banks' SOC can alert on;
        `reason` is the human-readable detail."""
        body = {"error": reason}
        if error_code:
            body["error_code"] = error_code
        await updater.add_artifact(
            parts=[_dict_to_part(body)],
            name="a2a-task-error",
            last_chunk=True,
        )
        await updater.failed()
