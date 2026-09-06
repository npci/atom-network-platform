# >>> a2a-core vendored header >>>
# GENERATED FILE — DO NOT EDIT HERE. Your change will be overwritten.
#
# Canonical source: packages/a2a-core/a2a_common/protocol.py
# Edit there, then run: scripts/ci/sync-a2a-core.sh
#
# This is security-critical A2A wire code shared byte-for-byte across services
# that cannot import each other (separate Docker build contexts). A fix applied
# to one copy and forgotten on the others is the failure mode this guards.
# <<< a2a-core vendored header <<<
# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""A2A wire protocol v1 — the single source of truth for the message contract.

Implements `A2A_PROTOCOL_DESIGN.pdf` (v1.0): 28 typed messages + `echo`, one
common envelope, 22 structured error codes, and per-message metadata
(direction / lifecycle phase / correlation key). The PDF is *descriptive*; this
module is *authoritative* — the drift test (`tests/a2a_common/test_protocol_contract.py`)
asserts the frozen 28 here match the PDF appendix exactly.

MIRROR: this file is byte-identical with the partner platform's copy, which lives
in its own repository   and is not
covered by this repo's sync + hygiene gate. Change one, change both.

Deltas from the frozen PDF (see `docs/A2A_PROTOCOL_MIGRATION.md` for rationale):
  1. envelope carries `protocol_version`
  2. envelope carries `message_id` — the uniform dedup/idempotency key
  3. `echo` uses the full envelope like every other message
  4. `change_id` lives in the envelope only (not duplicated into payloads)

Phase 0 establishes the enum, error codes, envelope, and per-message metadata.
Concrete per-message payload models are filled in as each message-group phase
lands (Phases 2–4); until then a payload validates against the permissive
`PayloadBase` (extra fields allowed, matching the "preserve extensions" decision).

Pure module: stdlib + pydantic only. No SDK, no app models, no DB — so it
imports cleanly in both the NPCI app graph and the partner SQLite harness.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

# Bump on any breaking envelope/contract change. Delta #1.
PROTOCOL_VERSION = "1.0"

# The cert-agent is not a distinct wire identity — it signs as the NPCI
# platform. Outbound cert-agent messages set from=SENDER_AUTHORITY and
# agent_id=CERT_ORCHESTRATOR_AGENT_ID (PDF §3 caution).
SENDER_AUTHORITY = "npci-platform"
CERT_ORCHESTRATOR_AGENT_ID = "npci.cert_orchestrator.v1"


class A2ATaskType(str, enum.Enum):
    """Every message type on the unified wire.

    The first 28 members are the frozen PDF contract (Parts A/B) plus the
    `echo` utility. The trailing `CERT_WITNESS_*` members are documented
    v1.0+ext extensions — tracked separately by `is_ext()` so the drift
    test does not compare them against the frozen appendix.
    """

    # --- Part A: Phase C lifecycle (keyed on change_id) ---
    CHANGE_COMMUNICATION    = "change_communication"
    PROPOSAL_ACKNOWLEDGED   = "proposal_acknowledged"
    CHANGE_ACKNOWLEDGEMENT  = "change_acknowledgement"
    QUERY                   = "query"
    CLARIFICATION_RESPONSE  = "clarification_response"
    COUNTER_PROPOSAL        = "counter_proposal"
    COUNTER_DECISION        = "counter_decision"
    MILESTONE_UPDATE        = "milestone_update"
    MILESTONE_STATUS_REQUEST = "milestone_status_request"
    MILESTONE_STATUS_REPORT = "milestone_status_report"
    CERT_READINESS_DECLARATION = "cert_readiness_declaration"
    BLOCKER                 = "blocker"
    BLOCKER_STATUS_UPDATE   = "blocker_status_update"
    BLOCKER_RESOLUTION      = "blocker_resolution"

    # --- Part B: Certification lifecycle (keyed on cflow_id + cert_attempt) ---
    CERT_CONFIG_REQUEST       = "cert_config_request"
    CERT_CONFIG_SUBMISSION    = "cert_config_submission"
    CERT_SETUP_NOTIFICATION   = "cert_setup_notification"
    CERT_TEST_PREPARATION     = "cert_test_preparation"
    CERT_CASE_RESULT          = "cert_case_result"
    CERT_VERDICT_NOTIFICATION = "cert_verdict_notification"
    CERT_VERDICT_DISPUTE      = "cert_verdict_dispute"
    CERT_WAIVER_REQUEST       = "cert_waiver_request"
    CERT_WAIVER_DECISION      = "cert_waiver_decision"
    CERT_FIX_NOTIFICATION     = "cert_fix_notification"
    CERT_SIGNOFF_NOTIFICATION = "cert_signoff_notification"
    CERT_STATUS_REQUEST       = "cert_status_request"
    CERT_STATUS_REPORT        = "cert_status_report"
    CERT_RUN_ABORT            = "cert_run_abort"

    # --- Part C: Utility ---
    ECHO = "echo"

    # --- v1.0+ext (not in the frozen 28) ---
    CERT_WITNESS_REQUEST   = "cert_witness_request"
    CERT_WITNESS_SCHEDULED = "cert_witness_scheduled"
    # NPCI→bank advisory: a kit revision is in progress (round closed, new
    # version being prepared). Partners hold queries until it ships.
    REVISION_IN_PROGRESS   = "revision_in_progress"
    # NPCI→bank per-partner round lifecycle notices. Round-state lives only on
    # the NPCI side (`negotiation_round_states`); these give the partner a
    # first-class signal of "you're in round N, deadline T" and "round N is
    # closed because X" so it stays in sync when a round transitions without a
    # coinciding kit dispatch (PM force-advance, silent-acceptance sweep).
    ROUND_OPENED           = "round_opened"
    ROUND_CLOSED           = "round_closed"
    # Integration-testing tunnel (ITA I-0). One ENCAPSULATED HTTP exchange,
    # direction EITHER so a single pair serves both the forward and the reverse
    # flow. The response normally rides the synchronous reply;
    # HTTP_EXCHANGE_RESPONSE exists for the deferred case (ITA plan §7).
    HTTP_EXCHANGE_REQUEST  = "http_exchange_request"
    HTTP_EXCHANGE_RESPONSE = "http_exchange_response"
    # ITA I-6 (§3.4): the START SIGNAL for the partner-initiated half of a
    # suite — which case ids the partner owns, the suite deadline, and the
    # alias its system under test calls back through. A DISTINCT instruction,
    # deliberately not folded into CERT_SETUP_NOTIFICATION: "here is the
    # suite" and "begin now" are separated in the requirement because test-
    # data validation sits between them.
    CERT_EXECUTION_START   = "cert_execution_start"

    def is_ext(self) -> bool:
        return self in _EXT_TASK_TYPES


_EXT_TASK_TYPES = frozenset(
    {
        A2ATaskType.CERT_WITNESS_REQUEST,
        A2ATaskType.CERT_WITNESS_SCHEDULED,
        A2ATaskType.REVISION_IN_PROGRESS,
        A2ATaskType.ROUND_OPENED,
        A2ATaskType.ROUND_CLOSED,
        A2ATaskType.HTTP_EXCHANGE_REQUEST,
        A2ATaskType.HTTP_EXCHANGE_RESPONSE,
        A2ATaskType.CERT_EXECUTION_START,
    }
)


class Direction(str, enum.Enum):
    AUTHORITY_TO_PARTNER = "npci_to_bank"
    PARTNER_TO_AUTHORITY = "bank_to_npci"
    EITHER       = "either"


class CorrelationKey(str, enum.Enum):
    """Which top-level id threads a message's lifecycle (PDF §5)."""

    CHANGE_ID = "change_id"
    CFLOW_ID  = "cflow_id"
    NONE      = "none"


class ErrorCode(str, enum.Enum):
    """The 22 structured rejection codes (PDF §10). Returned in the A2A
    response so banks' SOC can alert on the string without log parsing."""

    # HMAC envelope
    SIGNATURE_MISMATCH       = "signature_mismatch"
    TIMESTAMP_SKEW           = "timestamp_skew"
    REPLAY_DETECTED          = "replay_detected"
    MISSING_ENVELOPE_HEADERS = "missing_envelope_headers"
    # JWT
    INVALID_TOKEN   = "invalid_token"
    SESSION_REVOKED = "session_revoked"
    SESSION_EXPIRED = "session_expired"
    # Authorisation
    PARTNER_INACTIVE          = "partner_inactive"
    MTLS_REQUIRED             = "mtls_required"
    MTLS_FINGERPRINT_MISMATCH = "mtls_fingerprint_mismatch"
    # Network
    IP_NOT_ALLOWED = "ip_not_allowed"
    # Correlation
    PARTNER_MISMATCH = "partner_mismatch"
    UNKNOWN_ID       = "unknown_id"
    # State machine
    INVALID_STATE_TRANSITION = "invalid_state_transition"
    # Agent identity (optional, PDF §4 Options B/C)
    UNKNOWN_AGENT                 = "unknown_agent"
    AGENT_NOT_AUTHORIZED_FOR_TASK = "agent_not_authorized_for_task"
    # Protocol / schema
    UNKNOWN_TASK_TYPE       = "unknown_task_type"
    PAYLOAD_VALIDATION_ERROR = "payload_validation_error"
    # Validation
    BANK_IDENTITY_MISMATCH  = "bank_identity_mismatch"
    CERT_FINGERPRINT_MISMATCH = "cert_fingerprint_mismatch"
    BANK_UNREACHABLE        = "bank_unreachable"
    # Handler
    EXECUTOR_ERROR = "executor_error"

    @property
    def layer(self) -> str:
        return _ERROR_LAYERS[self]


_ERROR_LAYERS: dict[ErrorCode, str] = {
    ErrorCode.SIGNATURE_MISMATCH: "hmac",
    ErrorCode.TIMESTAMP_SKEW: "hmac",
    ErrorCode.REPLAY_DETECTED: "hmac",
    ErrorCode.MISSING_ENVELOPE_HEADERS: "hmac",
    ErrorCode.INVALID_TOKEN: "jwt",
    ErrorCode.SESSION_REVOKED: "jwt",
    ErrorCode.SESSION_EXPIRED: "jwt",
    ErrorCode.PARTNER_INACTIVE: "authz",
    ErrorCode.MTLS_REQUIRED: "authz",
    ErrorCode.MTLS_FINGERPRINT_MISMATCH: "authz",
    ErrorCode.IP_NOT_ALLOWED: "network",
    ErrorCode.PARTNER_MISMATCH: "correlation",
    ErrorCode.UNKNOWN_ID: "correlation",
    ErrorCode.INVALID_STATE_TRANSITION: "state_machine",
    ErrorCode.UNKNOWN_AGENT: "agent",
    ErrorCode.AGENT_NOT_AUTHORIZED_FOR_TASK: "agent",
    ErrorCode.UNKNOWN_TASK_TYPE: "protocol",
    ErrorCode.PAYLOAD_VALIDATION_ERROR: "schema",
    ErrorCode.BANK_IDENTITY_MISMATCH: "validation",
    ErrorCode.CERT_FINGERPRINT_MISMATCH: "validation",
    ErrorCode.BANK_UNREACHABLE: "validation",
    ErrorCode.EXECUTOR_ERROR: "handler",
}


# The live auth/HMAC middleware emit a few finer-grained operational codes than
# the canonical 22 (extra diagnostic detail for ops). They are NOT rewritten —
# they're catalogued here with their canonical mapping so the full emitted set
# is auditable in one place and SOC rules can normalise to the doc-22.
MIDDLEWARE_ERROR_CODES: dict[str, ErrorCode] = {
    "missing_bearer_token": ErrorCode.INVALID_TOKEN,
    "missing_envelope_headers": ErrorCode.MISSING_ENVELOPE_HEADERS,
    "session_unknown":      ErrorCode.INVALID_TOKEN,
    "partner_unknown":      ErrorCode.PARTNER_INACTIVE,
    "mtls_not_provisioned": ErrorCode.MTLS_REQUIRED,
}


@dataclass(frozen=True)
class MessageSpec:
    """Static contract metadata for one task type (PDF §6.x / §7.x summaries)."""

    task_type: A2ATaskType
    direction: Direction
    correlation_key: CorrelationKey
    cardinality: str
    ext: bool = False
    # ── PII classification (Tier 2 of docs/PII_DATA_CLASSIFICATION.md §3) ──
    #
    # True when this message type is DESIGNED to carry partner-authored free
    # text or an account/transaction reference — i.e. content a consumer's PII
    # could legitimately appear in. This is a DESIGN-TIME judgement recorded by
    # a reviewer, deliberately NOT a runtime inference: Tier 1's regex filter
    # already does the runtime guessing, and its whole weakness is that it can
    # only catch what its patterns match. Tagging the CONTRACT means a message
    # type known to carry PII gets mandatory filtering even when the heuristic
    # would have found nothing.
    #
    # Defaults False so this field is additive — it does not disturb the frozen
    # direction/correlation contract the drift test guards. But "unclassified"
    # is NOT allowed to mean "no PII": `PII_CLASSIFICATION_RATIONALE` below
    # must contain an entry for every task type, and a test enforces that, so a
    # newly-added message type fails CI until a human has actually decided.
    carries_pii: bool = False


def _spec(tt, d, ck, card, ext=False, carries_pii=False) -> MessageSpec:
    return MessageSpec(tt, d, ck, card, ext, carries_pii)


_T = A2ATaskType
_D = Direction
_C = CorrelationKey

# ── PII classification rationale (Tier 2, docs/PII_DATA_CLASSIFICATION.md §3) ─
#
# One entry per task type, REQUIRED. `test_every_task_type_has_a_pii_rationale`
# fails if a task type is missing, so adding a message to the enum forces a
# human decision instead of silently inheriting `carries_pii=False`. That
# default-deny-by-omission failure mode is the main thing this table prevents.
#
# The judgement applied: does this message type, BY DESIGN, carry
# partner-authored free text or an account/transaction reference? "A partner
# could stuff anything anywhere" is not the test — Tier 1's heuristic filter
# covers that residual case. This is about what the CONTRACT says the message
# is for.
PII_CLASSIFICATION_RATIONALE: dict[A2ATaskType, str] = {
    # ── Carries PII by design ────────────────────────────────────────────────
    _T.QUERY: "Partner-authored free-text question. May quote a live transaction "
              "or account holder to illustrate the issue being asked about.",
    _T.CLARIFICATION_RESPONSE: "NPCI's free-text answer to a QUERY; quotes the "
                               "question's context back, so it inherits the query's exposure.",
    _T.COUNTER_PROPOSAL: "Partner-authored justification free text. Already the "
                         "surface Tier 1 wires redaction into "
                         "(agents/negotiation_classifier.py).",
    _T.COUNTER_DECISION: "NPCI's free-text reasoning on a counter-proposal; "
                         "restates partner-supplied justification.",
    _T.BLOCKER: "Partner-authored description of what is blocking them — the "
                "message most likely to include a concrete failing transaction.",
    _T.BLOCKER_STATUS_UPDATE: "Free-text progress narrative on a blocker.",
    _T.BLOCKER_RESOLUTION: "Free-text resolution narrative on a blocker.",
    _T.CERT_CASE_RESULT: "Carries per-case request/response bodies from real UPI "
                         "switch traffic (see api/phase_c.py::cert_txns) — "
                         "VPAs, account references and mobile numbers are the "
                         "expected content, not an accident.",
    _T.CERT_VERDICT_DISPUTE: "Partner-authored free-text dispute, typically "
                             "quoting the transaction the verdict was based on.",
    _T.CERT_WAIVER_REQUEST: "Partner-authored free-text justification for waiving "
                            "a failing case.",
    _T.CERT_VERDICT_NOTIFICATION: "NPCI verdict text citing the failing case's "
                                  "observed values.",
    _T.CERT_TEST_PREPARATION: "Partner-supplied test data descriptions; test "
                              "accounts and VPAs are the point of the message.",
    _T.CERT_CONFIG_SUBMISSION: "Partner-supplied connectivity/config values, which "
                               "in practice include test account identifiers.",
    _T.CHANGE_COMMUNICATION: "Carries BRD/TSD document content, classified "
                             "PII-POSSIBLE in §2 of the classification doc.",
    _T.HTTP_EXCHANGE_REQUEST: "Carries an ENCAPSULATED HTTP request verbatim — "
                              "arbitrary body bytes plus forwarded Authorization "
                              "and Cookie headers (ITA plan §5.3 forwards them "
                              "deliberately, because a tunnel that strips "
                              "credentials cannot test anything). Under a cert "
                              "run the body IS live switch traffic. This is the "
                              "one message type whose whole purpose is to carry "
                              "someone else's bytes untouched, so it is "
                              "PII-bearing by construction, not by accident.",
    _T.HTTP_EXCHANGE_RESPONSE: "The encapsulated HTTP response, same reasoning "
                               "as the request: verbatim third-party body and "
                               "headers, including Set-Cookie.",

    # ── Does NOT carry PII by design ─────────────────────────────────────────
    _T.PROPOSAL_ACKNOWLEDGED: "Receipt only: change_id + version_received.",
    _T.CHANGE_ACKNOWLEDGEMENT: "Acceptance only: change_id + version_accepted.",
    _T.MILESTONE_UPDATE: "Milestone enum + timestamp.",
    _T.MILESTONE_STATUS_REQUEST: "Request for status; carries identifiers only.",
    _T.MILESTONE_STATUS_REPORT: "Milestone states + timestamps.",
    _T.CERT_READINESS_DECLARATION: "Readiness flag + version_implementing.",
    _T.CERT_CONFIG_REQUEST: "NPCI asking the partner to submit config; no partner "
                            "content yet.",
    _T.CERT_SETUP_NOTIFICATION: "Environment/attempt identifiers.",
    _T.CERT_WAIVER_DECISION: "Approve/reject + waiver_request_id.",
    _T.CERT_FIX_NOTIFICATION: "Which cases were fixed, by id.",
    _T.CERT_SIGNOFF_NOTIFICATION: "Terminal sign-off flag.",
    _T.CERT_STATUS_REQUEST: "Asks for the current cert-run status; carries the "
                            "cflow_id and cert_attempt only, no partner content.",
    _T.CERT_STATUS_REPORT: "Aggregate counts and lifecycle states; no per-case "
                           "request/response bodies (those ride CERT_CASE_RESULT).",
    _T.CERT_RUN_ABORT: "Abort reason code + identifiers.",
    _T.ECHO: "Connectivity probe used to verify the wire is up; the payload is "
             "an opaque round-trip token, not partner business content.",
    _T.CERT_WITNESS_REQUEST: "Scheduling request — session identifiers/times.",
    _T.CERT_WITNESS_SCHEDULED: "Scheduling confirmation — times and identifiers.",
    _T.REVISION_IN_PROGRESS: "Advisory flag that a kit revision is underway.",
    _T.ROUND_OPENED: "Round number + deadline.",
    _T.ROUND_CLOSED: "Round number + close reason code.",
    # NOTE: HTTP_EXCHANGE_REQUEST/RESPONSE deliberately do NOT appear in this
    # section — they are PII-bearing (see above). Duplicate keys here would
    # silently OVERWRITE the security rationale, which is exactly what an
    # earlier revision of this dict did.
    _T.CERT_EXECUTION_START: "Start signal for the partner-initiated half of a "
                             "suite: case ids, the suite deadline and the "
                             "callback ALIAS — identifiers only; the case DATA "
                             "already travelled on CERT_TEST_PREPARATION.",
}

# Derived from the table above so the two can never disagree: the set of task
# types whose content MUST be filtered before reaching an external LLM.
PII_BEARING_TASK_TYPES: frozenset = frozenset({
    _T.QUERY, _T.CLARIFICATION_RESPONSE,
    _T.COUNTER_PROPOSAL, _T.COUNTER_DECISION,
    _T.BLOCKER, _T.BLOCKER_STATUS_UPDATE, _T.BLOCKER_RESOLUTION,
    _T.CERT_CASE_RESULT, _T.CERT_VERDICT_DISPUTE, _T.CERT_WAIVER_REQUEST,
    _T.CERT_VERDICT_NOTIFICATION, _T.CERT_TEST_PREPARATION,
    _T.CERT_CONFIG_SUBMISSION, _T.CHANGE_COMMUNICATION,
    _T.HTTP_EXCHANGE_REQUEST, _T.HTTP_EXCHANGE_RESPONSE,
})


def carries_pii(task_type) -> bool:
    """True if this task type is classified as PII-bearing by design.

    Accepts an `A2ATaskType` or its raw wire string, because callers on the
    inbound path often hold the string form straight off the envelope. An
    UNKNOWN string returns True — fail CLOSED. An unrecognised task type is
    either a protocol addition that has not been classified yet or a malformed
    inbound message; treating either as "no PII" would be the exact silent
    downgrade this classification exists to prevent.
    """
    if isinstance(task_type, str):
        try:
            task_type = A2ATaskType(task_type)
        except ValueError:
            return True   # unknown wire value → assume the worst
    return task_type in PII_BEARING_TASK_TYPES

# Source of truth for direction + correlation per message. Built from the PDF's
# Part A / Part B summary tables and the §5 identifiers table.
MESSAGES: dict[A2ATaskType, MessageSpec] = {
    # Part A
    _T.CHANGE_COMMUNICATION:     _spec(_T.CHANGE_COMMUNICATION, _D.AUTHORITY_TO_PARTNER, _C.CHANGE_ID, "once per (change_id, version)", carries_pii=True),
    _T.PROPOSAL_ACKNOWLEDGED:    _spec(_T.PROPOSAL_ACKNOWLEDGED, _D.PARTNER_TO_AUTHORITY, _C.CHANGE_ID, "once per (change_id, version_received)"),
    _T.CHANGE_ACKNOWLEDGEMENT:   _spec(_T.CHANGE_ACKNOWLEDGEMENT, _D.PARTNER_TO_AUTHORITY, _C.CHANGE_ID, "once per (change_id, version_accepted)"),
    _T.QUERY:                    _spec(_T.QUERY, _D.PARTNER_TO_AUTHORITY, _C.CHANGE_ID, "any; per question", carries_pii=True),
    _T.CLARIFICATION_RESPONSE:   _spec(_T.CLARIFICATION_RESPONSE, _D.AUTHORITY_TO_PARTNER, _C.CHANGE_ID, "one per query_id", carries_pii=True),
    _T.COUNTER_PROPOSAL:         _spec(_T.COUNTER_PROPOSAL, _D.PARTNER_TO_AUTHORITY, _C.CHANGE_ID, "any; per proposal", carries_pii=True),
    _T.COUNTER_DECISION:         _spec(_T.COUNTER_DECISION, _D.AUTHORITY_TO_PARTNER, _C.CHANGE_ID, "optional; per counter_proposal_id", carries_pii=True),
    _T.MILESTONE_UPDATE:         _spec(_T.MILESTONE_UPDATE, _D.PARTNER_TO_AUTHORITY, _C.CHANGE_ID, "per milestone transition"),
    _T.MILESTONE_STATUS_REQUEST: _spec(_T.MILESTONE_STATUS_REQUEST, _D.EITHER, _C.CHANGE_ID, "any time"),
    _T.MILESTONE_STATUS_REPORT:  _spec(_T.MILESTONE_STATUS_REPORT, _D.EITHER, _C.CHANGE_ID, "reply to request"),
    _T.CERT_READINESS_DECLARATION: _spec(_T.CERT_READINESS_DECLARATION, _D.PARTNER_TO_AUTHORITY, _C.CHANGE_ID, "once per (change_id, version_implementing)"),
    _T.BLOCKER:                  _spec(_T.BLOCKER, _D.PARTNER_TO_AUTHORITY, _C.CHANGE_ID, "per impediment", carries_pii=True),
    _T.BLOCKER_STATUS_UPDATE:    _spec(_T.BLOCKER_STATUS_UPDATE, _D.AUTHORITY_TO_PARTNER, _C.CHANGE_ID, "any per blocker_id", carries_pii=True),
    _T.BLOCKER_RESOLUTION:       _spec(_T.BLOCKER_RESOLUTION, _D.AUTHORITY_TO_PARTNER, _C.CHANGE_ID, "once per blocker_id (terminal)", carries_pii=True),
    # Part B
    _T.CERT_CONFIG_REQUEST:      _spec(_T.CERT_CONFIG_REQUEST, _D.AUTHORITY_TO_PARTNER, _C.CFLOW_ID, "once per cflow_id"),
    _T.CERT_CONFIG_SUBMISSION:   _spec(_T.CERT_CONFIG_SUBMISSION, _D.PARTNER_TO_AUTHORITY, _C.CFLOW_ID, "once per cflow_id", carries_pii=True),
    _T.CERT_SETUP_NOTIFICATION:  _spec(_T.CERT_SETUP_NOTIFICATION, _D.AUTHORITY_TO_PARTNER, _C.CFLOW_ID, "once per cert_attempt"),
    _T.CERT_TEST_PREPARATION:    _spec(_T.CERT_TEST_PREPARATION, _D.PARTNER_TO_AUTHORITY, _C.CFLOW_ID, "incremental", carries_pii=True),
    _T.CERT_CASE_RESULT:         _spec(_T.CERT_CASE_RESULT, _D.EITHER, _C.CFLOW_ID, "per (case_id, attempt)", carries_pii=True),
    _T.CERT_VERDICT_NOTIFICATION: _spec(_T.CERT_VERDICT_NOTIFICATION, _D.AUTHORITY_TO_PARTNER, _C.CFLOW_ID, "per failed case", carries_pii=True),
    _T.CERT_VERDICT_DISPUTE:     _spec(_T.CERT_VERDICT_DISPUTE, _D.PARTNER_TO_AUTHORITY, _C.CFLOW_ID, "per disputed verdict", carries_pii=True),
    _T.CERT_WAIVER_REQUEST:      _spec(_T.CERT_WAIVER_REQUEST, _D.PARTNER_TO_AUTHORITY, _C.CFLOW_ID, "per case", carries_pii=True),
    _T.CERT_WAIVER_DECISION:     _spec(_T.CERT_WAIVER_DECISION, _D.AUTHORITY_TO_PARTNER, _C.CFLOW_ID, "per waiver request"),
    _T.CERT_FIX_NOTIFICATION:    _spec(_T.CERT_FIX_NOTIFICATION, _D.PARTNER_TO_AUTHORITY, _C.CFLOW_ID, "per fix batch"),
    _T.CERT_SIGNOFF_NOTIFICATION: _spec(_T.CERT_SIGNOFF_NOTIFICATION, _D.AUTHORITY_TO_PARTNER, _C.CFLOW_ID, "once per cflow_id"),
    _T.CERT_STATUS_REQUEST:      _spec(_T.CERT_STATUS_REQUEST, _D.EITHER, _C.CFLOW_ID, "any time"),
    _T.CERT_STATUS_REPORT:       _spec(_T.CERT_STATUS_REPORT, _D.EITHER, _C.CFLOW_ID, "reply or push"),
    _T.CERT_RUN_ABORT:           _spec(_T.CERT_RUN_ABORT, _D.EITHER, _C.CFLOW_ID, "once; terminal"),
    # Part C
    _T.ECHO:                     _spec(_T.ECHO, _D.PARTNER_TO_AUTHORITY, _C.NONE, "any time"),
    # ext
    _T.CERT_WITNESS_REQUEST:     _spec(_T.CERT_WITNESS_REQUEST, _D.PARTNER_TO_AUTHORITY, _C.CFLOW_ID, "per witness session", ext=True),
    _T.CERT_WITNESS_SCHEDULED:   _spec(_T.CERT_WITNESS_SCHEDULED, _D.AUTHORITY_TO_PARTNER, _C.CFLOW_ID, "per witness session", ext=True),
    _T.REVISION_IN_PROGRESS:     _spec(_T.REVISION_IN_PROGRESS, _D.AUTHORITY_TO_PARTNER, _C.CHANGE_ID, "per revision round", ext=True),
    _T.ROUND_OPENED:             _spec(_T.ROUND_OPENED, _D.AUTHORITY_TO_PARTNER, _C.CHANGE_ID, "per (change_id, partner_id, round_number)", ext=True),
    _T.ROUND_CLOSED:             _spec(_T.ROUND_CLOSED, _D.AUTHORITY_TO_PARTNER, _C.CHANGE_ID, "per (change_id, partner_id, round_number)", ext=True),
    _T.HTTP_EXCHANGE_REQUEST:    _spec(_T.HTTP_EXCHANGE_REQUEST, _D.EITHER, _C.NONE, "one per tunnelled exchange", ext=True, carries_pii=True),
    _T.HTTP_EXCHANGE_RESPONSE:   _spec(_T.HTTP_EXCHANGE_RESPONSE, _D.EITHER, _C.NONE, "at most one per exchange_id (deferred case)", ext=True, carries_pii=True),
    _T.CERT_EXECUTION_START:     _spec(_T.CERT_EXECUTION_START, _D.AUTHORITY_TO_PARTNER, _C.CFLOW_ID, "once per (cflow_id, cert_attempt)", ext=True),
}

# The 28 frozen messages + echo, in PDF order. Used by the drift test and by
# callers that need to enumerate the non-ext contract.
FROZEN_TASK_TYPES: tuple[A2ATaskType, ...] = tuple(
    tt for tt in MESSAGES if not MESSAGES[tt].ext
)


class PayloadBase(BaseModel):
    """Base for per-message payload models.

    `extra="allow"` honours the "preserve extensions" decision: payload bodies
    may carry fields beyond the PDF (e.g. multi-round counter-back, observation
    metadata) without failing validation. Concrete subclasses with required
    fields land per message-group phase.
    """

    model_config = ConfigDict(extra="allow")


# task_type → payload model. Phase 0 maps every message to PayloadBase; Phases
# 2–4 replace entries with concrete schemas as each group is built.
PAYLOAD_SCHEMAS: dict[A2ATaskType, type[PayloadBase]] = {
    tt: PayloadBase for tt in MESSAGES
}


class Envelope(BaseModel):
    """The common outer structure every message rides in (PDF §3 + deltas 1–4).

    `extra="forbid"`: the envelope is a strict contract. Extensions belong in
    `payload` (which is permissive), never as new envelope fields.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    protocol_version: str = Field(default=PROTOCOL_VERSION)  # delta #1
    message_id: str                                          # delta #2 (dedup key)
    task_type: A2ATaskType
    # `from` is a Python keyword — store as from_, (de)serialise as "from".
    from_: str = Field(alias="from")
    agent_id: str | None = None
    agent_run_id: str | None = None
    correlation_id: str | None = None
    timestamp: str | None = None
    change_id: str | None = None   # delta #4 — envelope only, not in payloads
    cflow_id: str | None = None
    cert_attempt: int | None = None
    payload: dict = Field(default_factory=dict)


def spec_for(task_type: A2ATaskType) -> MessageSpec:
    return MESSAGES[task_type]


def parse_task_type(value: str) -> A2ATaskType:
    """Map a wire string to the enum. Raises ValueError on unknown values —
    callers translate that into an `unknown_task_type` rejection."""
    return A2ATaskType(value)


# --- Envelope helpers (Phase 1 plumbing) ---------------------------------------
#
# These build/read the envelope as a plain dict and accept `task_type` as a
# raw string. During the migration the wire still carries pre-rename task_types
# (e.g. status_update) that aren't in A2ATaskType yet, so we must NOT validate
# membership here — that tightening lands with the renames (Phase 2) and strict
# `Envelope` validation (Phase 5). The strict `Envelope` model above remains the
# authoritative contract for the post-cutover state.


def make_envelope(
    task_type,
    *,
    message_id: str,
    from_: str,
    payload: dict | None = None,
    change_id: str | None = None,
    cflow_id: str | None = None,
    cert_attempt: int | None = None,
    correlation_id: str | None = None,
    agent_id: str | None = None,
    agent_run_id: str | None = None,
    timestamp: str | None = None,
) -> dict:
    """Assemble the wire envelope dict with the v1 fields (deltas 1–2 always set).

    `task_type` may be an enum (any str-enum) or a plain string. Optional ids are
    omitted when None so the wire stays clean; receivers treat absent optionals
    as null. `message_id` (dedup key) and `protocol_version` are always present.
    """
    tt = task_type.value if isinstance(task_type, enum.Enum) else str(task_type)
    env: dict = {
        "protocol_version": PROTOCOL_VERSION,
        "message_id": message_id,
        "task_type": tt,
        "from": from_,
        "payload": payload or {},
    }
    for key, val in (
        ("change_id", change_id),
        ("cflow_id", cflow_id),
        ("cert_attempt", cert_attempt),
        ("correlation_id", correlation_id),
        ("agent_id", agent_id),
        ("agent_run_id", agent_run_id),
        ("timestamp", timestamp),
    ):
        if val is not None:
            env[key] = val
    return env


@dataclass(frozen=True)
class InboundEnvelope:
    """Tolerant read of an inbound envelope. Every field optional so legacy
    (pre-v1) messages parse without error during the migration window."""

    task_type: str
    payload: dict
    from_: str | None
    message_id: str | None
    correlation_id: str | None
    change_id: str | None
    cflow_id: str | None
    cert_attempt: int | None
    agent_id: str | None
    agent_run_id: str | None
    timestamp: str | None
    protocol_version: str | None


def _as_str(value: object) -> str | None:
    """Accept a string, reject any other non-null type.

    Returns None (treated as "absent") rather than coercing: `str(["a","b"])`
    would silently produce the literal `"['a', 'b']"` and carry a
    type-confused value deeper into the system, which is exactly the failure
    this guard exists to stop.
    """
    return value if isinstance(value, str) else None


def _as_int(value: object) -> int | None:
    """Accept an int, or a string of digits (JSON senders vary). Reject the
    rest. `bool` is excluded explicitly — in Python `isinstance(True, int)` is
    True, and a `cert_attempt` of `True` silently becoming attempt 1 is the
    kind of quiet nonsense this whole function is here to prevent."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def read_envelope(data: dict | None) -> InboundEnvelope:
    """Extract envelope fields from an inbound message dict.

    TYPE-checked, but still field-tolerant — a deliberate middle position
    between the two things the security contract could have meant by
    `validation_failure_behavior: "reject"`:

      * **Missing fields stay tolerated.** Rejecting an envelope that omits
        `message_id`/`correlation_id`/`protocol_version` would break a legacy
        or older-version NPCI that never sends them. That compatibility
        promise is why this reader exists, and it is unchanged.

      * **Wrong-typed fields are no longer passed through.** Previously
        `payload` was `data.get("payload") or {}`, so a JSON body sending
        `"payload": "some string"` handed a `str` to every downstream handler
        that is annotated `dict` and calls `.get()` on it — an AttributeError
        deep inside a handler at best, and at worst a value used in a context
        that accepts both. `change_id` typed as a dict flowed into DB lookups.
        A field of the wrong type is now read as absent, so the existing
        `if not task_type` / `if not payload` guards catch it at the boundary
        where the error is legible.

    This closes the actual exploitable half of the gap (type confusion from an
    H3 boundary) at zero compatibility cost. Full strict rejection of unknown
    or missing fields remains a protocol-version decision to make jointly with
    NPCI, not something to impose unilaterally from the partner side.
    """
    data = data if isinstance(data, dict) else {}
    payload = data.get("payload")
    return InboundEnvelope(
        task_type=_as_str(data.get("task_type")) or "",
        # Handlers are annotated `payload: dict` and index into it; anything
        # else is read as empty so the caller's `if not payload` guard fires.
        payload=payload if isinstance(payload, dict) else {},
        from_=_as_str(data.get("from")),
        message_id=_as_str(data.get("message_id")),
        correlation_id=_as_str(data.get("correlation_id")),
        change_id=_as_str(data.get("change_id")),
        cflow_id=_as_str(data.get("cflow_id")),
        cert_attempt=_as_int(data.get("cert_attempt")),
        agent_id=_as_str(data.get("agent_id")),
        agent_run_id=_as_str(data.get("agent_run_id")),
        timestamp=_as_str(data.get("timestamp")),
        protocol_version=_as_str(data.get("protocol_version")),
    )
