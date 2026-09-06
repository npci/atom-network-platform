# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase C models — Partner Collaboration via A2A protocol.

Tables:
  partner_agents             — Partner registry (four-party roles: Payer/Payee PSP,
                               Remitter, Beneficiary; + internal cert_engine)
  change_partner_assignments — Which partners are assigned to which changes
  a2a_sessions               — JWT session tracking per partner
  a2a_messages               — Audit trail of all A2A Tasks
  partner_progress           — Intermediate implementation steps
  negotiation_threads        — Q&A threads per partner per change
  negotiation_messages       — Individual Q&A messages
  cert_runs                  — Certification test run batches
  cert_test_results          — Per-test certification results
  cert_triage                — AI verdict + human override per failure
  cert_flow_states           — Persisted certification lifecycle phase (CERT-0)
"""
import enum
import secrets
from datetime import datetime

from sqlalchemy import (
    String, Text, Integer, Boolean, Float, DateTime, JSON, ForeignKey, Enum, Index,
    UniqueConstraint,
)
from sqlalchemy.orm import mapped_column, Mapped, relationship
from app.core.database import Base
from app.core.encrypted_type import EncryptedSecret
from app.models.base import TimestampMixin, generate_uuid, utcnow


def _enum(e):
    return Enum(e, values_callable=lambda x: [i.value for i in x])


# ── Enums ─────────────────────────────────────────────────────────────────────

class PartnerType(str, enum.Enum):
    # Four-party network transaction roles (the operator-selectable registry types).
    PAYER_PSP    = "payer_psp"     # PSP on the payer side (initiates the debit)
    PAYEE_PSP    = "payee_psp"     # PSP on the payee side (collects / credits)
    REMITTER     = "remitter"      # remitter (payer's account-holding) bank
    BENEFICIARY  = "beneficiary"   # beneficiary (payee's account-holding) bank
    CERT_ENGINE  = "cert_engine"   # authority-internal: cert-agent submodule, target of CERT_TEST_REQUEST
                                   #   (not operator-selectable — hidden in the registry UI)


class PartnerStatus(str, enum.Enum):
    ACTIVE    = "active"
    INACTIVE  = "inactive"
    SUSPENDED = "suspended"


class AssignmentStatus(str, enum.Enum):
    """Linear partner lifecycle. Old values (COMMUNICATED, ACKNOWLEDGED,
    IN_PROGRESS, READY) are kept for back-compat with legacy data — Postgres
    can't drop enum values cleanly. Migration 0031 moves all rows onto the
    new vocabulary, so these dormant values should not appear in new data.
    """
    ASSIGNED                = "assigned"
    # Old (dormant after migration 0031)
    COMMUNICATED            = "communicated"
    ACKNOWLEDGED            = "acknowledged"
    IN_PROGRESS             = "in_progress"
    READY                   = "ready"
    # New (active vocabulary post-0031)
    RECEIVED                = "received"
    ACCEPTED                = "accepted"
    APPLIED                 = "applied"
    TESTED                  = "tested"
    READY_FOR_CERTIFICATION = "ready_for_certification"
    CERTIFYING              = "certifying"
    CERTIFIED               = "certified"
    READY_FOR_PRODUCTION    = "ready_for_production"
    IN_PRODUCTION           = "in_production"
    WITHDRAWN               = "withdrawn"


class A2ADirection(str, enum.Enum):
    INBOUND  = "inbound"   # partner → the Authority
    OUTBOUND = "outbound"  # The Authority → partner


class A2ATaskType(str, enum.Enum):
    CHANGE_COMMUNICATION    = "change_communication"
    # Partner auto-emits this on receipt with kit_files_received[] (per
    # the rollout-doc PROPOSAL_ACKNOWLEDGED semantics). Distinct from
    # CHANGE_ACKNOWLEDGEMENT which is the explicit human-driven accept.
    PROPOSAL_ACKNOWLEDGED   = "proposal_acknowledged"
    CHANGE_ACKNOWLEDGEMENT  = "change_acknowledgement"   # partner accepts a change → status=accepted
    QUERY                   = "query"                    # partner asks clarifying question — no state impact
    # Cert-channel clarification — distinct task type so the Authority executor
    # routes it to a kind='cert' NegotiationThread (separate inbox in
    # Agent Messaging). Same wire shape as QUERY; the partition is
    # entirely in the routing + thread.kind column.
    CERT_QUERY              = "cert_query"
    # Cert lifecycle status update from partner. Wire payload:
    # { status: 'received'|'deployed'|'tested'|'ready_for_certification',
    #   change_id, role?, test_data? }. The Authority maps to AssignmentStatus:
    # received→RECEIVED, deployed→APPLIED, tested→TESTED,
    # ready_for_certification→READY_FOR_CERTIFICATION. The 'ready' status
    # additionally carries role + test_data and triggers the cert
    # orchestrator (matches today's READINESS_DECLARATION behaviour).
    CERT_STATUS_UPDATE      = "cert_status_update"
    # Partner formally proposes changes to terms. Distinct from QUERY:
    # creates a CounterProposal row that gates rollout state until PM
    # accepts/rejects. Pre-Tier-1, counters were piggybacked on QUERY
    # with a `message_kind` discriminator; that path stays back-compat
    # but new senders use this dedicated task type.
    COUNTER_PROPOSAL        = "counter_proposal"
    # Partner's verdict on an authority-originated counter (mirror of the
    # The Authority→partner COUNTER_DECISION carried inside CLARIFICATION_RESPONSE).
    # Dedicated task type because the Authority executor has no inbound
    # clarification-response branch — partners never originate one
    # except for this case.
    COUNTER_DECISION        = "counter_decision"
    CLARIFICATION_RESPONSE  = "clarification_response"
    # Partner reports an obstacle mid-implementation. Distinct from
    # QUERY: requires structured fields (severity / impact / options[])
    # and flips assignment.status to BLOCKED until PM resolves it.
    BLOCKER                 = "blocker"
    BLOCKER_RESOLUTION      = "blocker_resolution"   # The Authority's response, can carry a patched artifact
    # Post-freeze break-glass channel. After the final kit (v3) ships and the
    # change is frozen, this is the ONLY inbound task the executor accepts from
    # a partner — a critical, work-stopping issue. Creates an EmergencyIssue row.
    EMERGENCY_ISSUE         = "emergency_issue"
    STATUS_UPDATE           = "status_update"
    # The Authority→partner: the negotiation has frozen (round cap reached). Carries the
    # freeze timestamp so the partner locks its decision/composer even when the
    # version didn't climb to the legacy v3 threshold (round-based freeze).
    NEGOTIATION_FROZEN      = "negotiation_frozen"
    CERT_READINESS_DECLARATION = "cert_readiness_declaration"
    CERT_TEST_REQUEST       = "cert_test_request"
    CERT_TEST_RESPONSE      = "cert_test_response"
    CERT_ACKNOWLEDGEMENT    = "cert_acknowledgement"
    # All-PASS sign-off: The Authority ships the formal Certification Result
    # certificate (.docx) to the partner. Referenced by cert_orchestrator
    # step 8; without this member that send raised an (swallowed) AttributeError.
    CERT_COMPLETION_SIGNOFF = "cert_completion_signoff"
    DEFECT_NOTICE           = "defect_notice"
    DEFECT_RESOLUTION       = "defect_resolution"
    # No-op probe used by the partner's Settings → Test Connection button.
    # Exercises the full inbound pipeline (HMAC envelope verify → Bearer
    # JWT decode → SDK dispatch) but performs zero DB state changes beyond
    # the standard A2AMessage audit row. Surfaces real config gaps
    # (bad api_key, missing/wrong partner HMAC secret, etc.) that the
    # well-known-card reachability check can't catch.
    ECHO                    = "echo"
    # DB-compat: rows inserted by the uat branch carry this value;
    # the feature isn't wired on main but the enum must accept it so
    # SELECT queries over a2a_messages don't crash with LookupError.
    CERT_OF_COMPLIANCE      = "cert_of_compliance"
    # The Authority→partner round lifecycle notices (mirror of v1.0+ext members in
    # a2a_common/protocol.py). Executor validates task_type against the union
    # of both enums; without these values the a2a_messages audit row would
    # reject on insert.
    ROUND_OPENED            = "round_opened"
    ROUND_CLOSED            = "round_closed"


class ProgressStep(str, enum.Enum):
    DESIGN_COMPLETED  = "design_completed"
    CODING_COMPLETED  = "coding_completed"
    TESTING_COMPLETED = "testing_completed"


class NegotiationRole(str, enum.Enum):
    PARTNER     = "partner"
    AI_DRAFT    = "ai_draft"
    PO_APPROVED = "po_approved"


class ThreadStatus(str, enum.Enum):
    OPEN     = "open"
    RESOLVED = "resolved"


class CounterProposalStatus(str, enum.Enum):
    OPEN            = "open"             # awaiting decision from the other side
    ACCEPTED        = "accepted"         # other side accepted these terms — negotiation closes
    REJECTED        = "rejected"         # other side rejected — orig stays as default unless they re-counter
    COUNTERED_BACK  = "countered_back"   # other side proposed their own counter — see new row with opposite originator
    WITHDRAWN       = "withdrawn"        # originator withdrew (rare)


class BlockerSeverity(str, enum.Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    MEDIUM   = "medium"
    LOW      = "low"


class BlockerStatus(str, enum.Enum):
    OPEN     = "open"
    RESOLVED = "resolved"
    WONTFIX  = "wontfix"


class RequestCategory(str, enum.Enum):
    TIMELINE     = "timeline"
    SCOPE        = "scope"
    LIMITS       = "limits"
    API_CONTRACT = "api_contract"
    DEPENDENCY   = "dependency"
    CERT_ROLE    = "cert_role"


class BRDClassification(str, enum.Enum):
    MANDATORY_VIOLATION    = "mandatory_violation"
    OPTIONAL_IN_TOLERANCE  = "optional_in_tolerance"
    OPTIONAL_ESCALATED     = "optional_escalated"
    UNCATEGORIZED          = "uncategorized"


class AutoDisposition(str, enum.Enum):
    AUTO_REJECTED   = "auto_rejected"
    AUTO_ACCEPTED   = "auto_accepted"
    ESCALATED_TO_PM = "escalated_to_pm"
    PENDING         = "pending"


class RoundStatus(str, enum.Enum):
    OPEN               = "open"
    CLOSED_BY_PM       = "closed_by_pm"
    SILENTLY_ACCEPTED  = "silently_accepted"
    RESPONDED          = "responded"


class ClusterPMDecision(str, enum.Enum):
    PENDING = "pending"
    ACCEPT  = "accept"
    MODIFY  = "modify"
    REJECT  = "reject"


class CertRunStatus(str, enum.Enum):
    RUNNING   = "running"
    COMPLETED = "completed"
    ABORTED   = "aborted"   # protocol v1 cert_run_abort (§7.14) — terminal


class CertDirection(str, enum.Enum):
    AUTHORITY_TO_PARTNER = "npci_to_partner"
    PARTNER_TO_AUTHORITY = "partner_to_npci"


class CertTestStatus(str, enum.Enum):
    PASS  = "pass"
    FAIL  = "fail"
    SKIP  = "skip"
    ERROR = "error"


class TriageVerdict(str, enum.Enum):
    PARTNER_CODE_BUG = "partner_code_bug"
    TEST_CASE_ISSUE  = "test_case_issue"
    ENV_ISSUE        = "env_issue"


# ── Models ────────────────────────────────────────────────────────────────────

class PartnerAgent(Base, TimestampMixin):
    __tablename__ = "partner_agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    partner_type: Mapped[list | None] = mapped_column(JSON, nullable=False, default=["payer_psp"])
    endpoint_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # Encrypted at rest (reuses the same Fernet mechanism already used for
    # `app_configs` secrets — see core/encrypted_type.py). `api_key_hash`
    # below is NOT encrypted: it is looked up by equality (auth path), and
    # Fernet ciphertext is non-deterministic, so encrypting a column used
    # for exact-match lookup would break login. The hash is already a
    # one-way SHA-256 digest, which is the correct control for that column.
    api_key: Mapped[str | None] = mapped_column(EncryptedSecret, nullable=True)
    api_key_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Cert-agent's short bank_id (HDFC, ICICI001, …) for this partner.
    # Used by the readiness orchestrator to address the right
    # bank-simulator endpoint when triggering an LLM cert run.
    cert_agent_bank_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[PartnerStatus] = mapped_column(_enum(PartnerStatus), nullable=False, default=PartnerStatus.ACTIVE)
    agent_card_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    # Outbound wire selector (alembic 0033). 'legacy' = hand-rolled
    # POST /a2a-partner/api/a2a/tasks/send (today's behaviour);
    # 'a2a_sdk' = SDK JSON-RPC at the partner's /a2a-rpc/rpc.
    # Default 'legacy' so existing partners keep their wire on upgrade.
    protocol_version: Mapped[str] = mapped_column(
        String(20), nullable=False, default="legacy", server_default="legacy",
    )
    # ── Slice 3 (security hardening) — per-partner JWT signing secret ──
    # 32-byte hex string. The Authority signs outbound A2A JWTs to this partner
    # with this secret; the partner verifies with the symmetric value
    # stored on its side as the partner's stored JWT-signing secret. Null
    # means "no JWT signing configured" → outbound currently sends no
    # Authorization header (back-compat for partners not yet upgraded).
    # Encrypted at rest — see api_key's comment above for the mechanism.
    jwt_signing_secret: Mapped[str | None] = mapped_column(EncryptedSecret, nullable=True)
    # ── Slice 5 (security hardening) — HMAC envelope secret ──────────
    # Distinct from `jwt_signing_secret`: that one signs the IDENTITY
    # (Bearer JWT), this one signs the PAYLOAD (X-NPCI-Signature header
    # over body sha256 + ts + nonce). Both NULL means "back-compat" —
    # auth middleware logs a warning and lets the request through
    # unsigned. Both set means full envelope enforcement.
    # Encrypted at rest — see api_key's comment above for the mechanism.
    signing_secret: Mapped[str | None] = mapped_column(EncryptedSecret, nullable=True)
    # ── Slice 6 (security hardening) — mTLS for bank-tier partners ───
    # `tls_tier` switches transport: 'jwt' (default — Bearer only on :443)
    # or 'mtls' (pinned client cert on :8443 PLUS Bearer JWT). The
    # account-holding banks (remitter/beneficiary) are the tier-mtls
    # callers today; PSPs (payer/payee) and cert_engine stay on jwt.
    # `client_cert_fingerprint` is the SHA-256 hex of the
    # bank's client cert; nginx forwards `X-Client-Cert-Fingerprint`
    # and the auth middleware compares against this column. Layered on
    # top of JWT — both must pass.
    tls_tier: Mapped[str] = mapped_column(
        String(20), nullable=False, default="jwt", server_default="jwt",
    )
    client_cert_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # ── Outbound TLS (the Authority → this partner's HTTPS endpoint) ──────────
    # Distinct from tls_tier / client_cert_fingerprint above, which govern
    # INBOUND mTLS (partner → the Authority). These control how the backend verifies the
    # partner's SERVER cert when it calls `endpoint_url` (the Test-connectivity
    # probe AND the real A2A card fetch). `ssl_verify` NULL = inherit the global
    # `settings.partner_tls_verify`; `ca_cert_pem` is an uploaded CA/cert PEM to
    # trust for this partner (overrides the global PARTNER_CA_BUNDLE).
    ssl_verify: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ca_cert_pem: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-partner cap (bytes) on the base64-encoded size of a single kit
    # attachment shipped inline in the A2A envelope; oversize attachments are
    # omitted (metadata kept) to stay under this partner's ingress body limit.
    # NULL = inherit the global `settings.partner_max_inline_attachment_bytes`;
    # 0 = no limit (inline everything).
    max_inline_attachment_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ── Slice 7 (security hardening) — network controls ──────────────
    # `allowed_cidrs` is a JSON list of CIDR strings; NULL = no IP
    # enforcement. The middleware compares X-Real-IP (set by nginx)
    # against each entry using stdlib `ipaddress`. `rate_limit_rps`
    # is the per-partner cap; nginx today applies a flat baseline
    # zone, this column is the override hook for Slice 9.
    allowed_cidrs: Mapped[list | None] = mapped_column(JSON, nullable=True)
    rate_limit_rps: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default="100",
    )
    # ── Key rotation grace period (security hardening) ──────────────
    # Before overwriting a secret during rotation, the current value is
    # saved here. Auth/HMAC middleware tries the current secret first;
    # if that fails AND secret_rotated_at is within the last 5 minutes,
    # the previous secret is tried as a fallback.
    # Encrypted at rest — see api_key's comment above for the mechanism.
    previous_jwt_signing_secret: Mapped[str | None] = mapped_column(EncryptedSecret, nullable=True)
    previous_signing_secret: Mapped[str | None] = mapped_column(EncryptedSecret, nullable=True)
    secret_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ── T2 (THREAT_MODEL.md — "HMAC signature/key-version not persisted
    # for non-repudiation across rotations") ─────────────────────────
    # Monotonically incremented every time rotate-hmac-secret runs (never
    # reset, never reused). Persisted on each A2AMessage row alongside the
    # verified signature (see A2AMessage.hmac_key_version below) so a
    # future dispute can be adjudicated against the EXACT secret version
    # in effect at receipt time, independent of how many times the secret
    # has been rotated since. Starts at 1 for every existing/new partner.
    signing_secret_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1",
    )

    assignments: Mapped[list["ChangePartnerAssignment"]] = relationship(back_populates="partner", cascade="all, delete-orphan")

    @staticmethod
    def generate_api_key() -> str:
        return f"a2a_{secrets.token_urlsafe(32)}"

    @staticmethod
    def generate_jwt_signing_secret() -> str:
        """32-byte hex secret for HS256 outbound JWT signing (Slice 3)."""
        return secrets.token_hex(32)

    @staticmethod
    def generate_signing_secret() -> str:
        """32-byte hex secret for HMAC envelope signing (Slice 5)."""
        return secrets.token_hex(32)


class ChangePartnerAssignment(Base):
    __tablename__ = "change_partner_assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(String(36), ForeignKey("change_requests.id"), nullable=False)
    partner_id: Mapped[str] = mapped_column(String(36), ForeignKey("partner_agents.id"), nullable=False)
    status: Mapped[AssignmentStatus] = mapped_column(_enum(AssignmentStatus), nullable=False, default=AssignmentStatus.ASSIGNED)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    # Concurrent flag — orthogonal to status. Set/cleared via /block + /unblock.
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Structured acceptance / acknowledgement metadata. Written by the
    # PROPOSAL_ACKNOWLEDGED auto-receipt handler (kit_files_received,
    # received_at) and the PROPOSAL_ACCEPTANCE handler (decision,
    # accepted_by, internal_change_advisory_ref, estimated_phase_timeline).
    # Keeping it as a single JSON column rather than spreading into
    # named columns: the schema is partner-driven per the rollout doc
    # and partners may add fields we don't model. Postgres uses JSONB
    # via the migration; this attribute reads either dialect.
    acceptance_meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    partner: Mapped["PartnerAgent"] = relationship(back_populates="assignments")
    status_history: Mapped[list["AssignmentStatusHistory"]] = relationship(
        back_populates="assignment",
        cascade="all, delete-orphan",
        order_by="desc(AssignmentStatusHistory.created_at)",
    )


class AssignmentStatusHistory(Base):
    """Audit trail of every status transition on ChangePartnerAssignment.

    Written automatically on system transitions (a2a handlers + cert handlers)
    and on every admin manual action (block/unblock/approve/mark-live/withdraw).
    Funnel point: `services/assignment_status.set_status`.
    """
    __tablename__ = "assignment_status_history"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    assignment_id: Mapped[str] = mapped_column(String(36), ForeignKey("change_partner_assignments.id"), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    to_status: Mapped[str] = mapped_column(String(50), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    actor_partner_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("partner_agents.id"), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)

    assignment: Mapped["ChangePartnerAssignment"] = relationship(back_populates="status_history")


class A2ASession(Base):
    __tablename__ = "a2a_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    partner_id: Mapped[str] = mapped_column(String(36), ForeignKey("partner_agents.id"), nullable=False)
    jwt_token_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    # Slice 2 (security hardening) — admin marks revoked_at to invalidate
    # the JWT immediately. SdkAuthMiddleware refuses any JWT whose session
    # row has revoked_at IS NOT NULL.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Slice 9 (security hardening) — refresh tokens. Access TTL is 15 min;
    # refresh TTL is 24h. /auth/refresh rotates the access token (and the
    # refresh) so stolen refresh tokens become detectable: the legit
    # client's next refresh sees an unfamiliar `refresh_token_hash` on
    # the row and rejects.
    refresh_token_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class A2AMessage(Base):
    __tablename__ = "a2a_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("change_requests.id"), nullable=True)
    partner_id: Mapped[str] = mapped_column(String(36), ForeignKey("partner_agents.id"), nullable=False)
    direction: Mapped[A2ADirection] = mapped_column(_enum(A2ADirection), nullable=False)
    # Protocol-v1 migration: the audit log stores the raw wire task_type string
    # (the DB column is already varchar). Decoupling it from the live enum means
    # historical rows with retired values (status_update, cert_query, …) stay
    # readable instead of raising LookupError on coercion. Dispatch/validation
    # uses app.a2a_common.protocol.A2ATaskType; this column is a record, not a
    # constraint.
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Slice 25 (admin A2A logs UI) — response artifact persisted alongside
    # the request body. Both halves of the round-trip live on one row:
    #   inbound  → payload = partner-sent body, response_body = the Authority reply
    #   outbound → payload = the Authority-sent body,    response_body = partner reply
    response_body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="sent")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    # ── A2A SDK columns (alembic 0033) — populated only on protocol_ver='a2a_sdk' ──
    # task_id_a2a is the SDK's Task ID (distinct from `id`, the audit-row id).
    # task_state mirrors the SDK Task lifecycle (SUBMITTED / WORKING /
    # COMPLETED / FAILED / CANCELLED). protocol_ver flags which wire delivered
    # this row — useful for analysis once both wires carry production traffic.
    task_id_a2a: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    protocol_ver: Mapped[str] = mapped_column(
        String(20), nullable=False, default="legacy", server_default="legacy",
    )
    # ── Slice 8 audit columns (alembic 0036) — RBI evidence trail ──────────
    # Populated by SdkAuthMiddleware (inbound) and a2a_client (outbound).
    # caller_ip is INET in Postgres / String(45) in SQLite tests; SQLAlchemy
    # plays nice with String here since we read/write strings on the app
    # side. mTLS fingerprint arrives in Slice 6.
    caller_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    jwt_sub: Mapped[str | None] = mapped_column(String(64), nullable=True)
    jwt_iat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    jwt_exp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    client_cert_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # ── Delivery retry (alembic 0095) ──────────────────────────────────────
    # Outbound sends were single-attempt: one failure meant the partner never got the
    # message and nothing ever retried, because the table had no way to express "try
    # again later". These three columns are what the retry sweeper
    # (`a2a.retry_failed_deliveries`) schedules on.
    #   attempts      — delivery attempts made so far (1 after the first send)
    #   next_retry_at — when the sweeper may retry; NULL = not scheduled / give up
    #   last_error_at — timestamp of the most recent failure, for triage
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ── T1/T2 (THREAT_MODEL.md — "No at-rest integrity check on
    # A2AMessage.payload" / "HMAC signature/key-version not persisted for
    # non-repudiation across rotations") — migration 0127. ──────────────
    # payload_sha256: computed from the RAW inbound request body bytes at
    # receipt time (before any parsing), so a later read that finds this
    # hash not matching a recomputed hash of the CURRENT `payload` column
    # indicates at-rest tampering (a compromised DB credential or an
    # internal actor editing the row directly) — the in-transit HMAC only
    # ever proved integrity for the moment of receipt, not afterward.
    payload_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # hmac_signature: the verified X-NPCI-Signature value for this
    # message (inbound only — outbound rows leave this NULL, since the
    # PARTNER'S verification of the Authority's signature is not this platform's
    # record to keep). This is itself non-repudiation evidence: only the
    # holder of the partner's signing_secret AT THE TIME could have
    # produced it.
    hmac_signature: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # hmac_key_version: snapshot of PartnerAgent.signing_secret_version at
    # the moment this message was verified — NOT a live reference, so a
    # later rotation cannot retroactively change what this row claims was
    # true when the signature was checked.
    hmac_key_version: Mapped[int | None] = mapped_column(Integer, nullable=True)


class PartnerProgress(Base):
    __tablename__ = "partner_progress"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    assignment_id: Mapped[str] = mapped_column(String(36), ForeignKey("change_partner_assignments.id"), nullable=False)
    step: Mapped[ProgressStep] = mapped_column(_enum(ProgressStep), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ThreadKind(str, enum.Enum):
    """Channel partition for negotiation threads.

    GENERAL — existing Phase C clarifications (BRD/spec questions, scope,
              timing, behaviour). Triggered by A2ATaskType.QUERY.
    CERT    — certification-only clarifications (test scenarios, fixture
              data, env access, defect triage Q&A). Triggered by
              A2ATaskType.CERT_QUERY. Strictly separate inbox; no row is
              ever shared with GENERAL.
    """
    GENERAL = "general"
    CERT    = "cert"


class NegotiationThread(Base):
    __tablename__ = "negotiation_threads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(String(36), ForeignKey("change_requests.id"), nullable=False)
    partner_id: Mapped[str] = mapped_column(String(36), ForeignKey("partner_agents.id"), nullable=False)
    # Channel partition. varchar(20) on the DB side (alembic 0047) — kept
    # as a plain string column rather than a Postgres ENUM so future
    # additions don't require ALTER TYPE.
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default=ThreadKind.GENERAL.value)
    status: Mapped[ThreadStatus] = mapped_column(_enum(ThreadStatus), nullable=False, default=ThreadStatus.OPEN)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    messages: Mapped[list["NegotiationMessage"]] = relationship(back_populates="thread", cascade="all, delete-orphan")


class NegotiationMessage(Base):
    __tablename__ = "negotiation_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    thread_id: Mapped[str] = mapped_column(String(36), ForeignKey("negotiation_threads.id"), nullable=False)
    role: Mapped[NegotiationRole] = mapped_column(_enum(NegotiationRole), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    ai_draft: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Partner-originated message ID (UUID). Set on `role='partner'` rows so
    # the matching CLARIFICATION_RESPONSE we send back can carry it and
    # the partner attaches the answer to the exact OutgoingQuery — not
    # whichever is "most recent". NULL on AI_DRAFT / PO_APPROVED rows and
    # on legacy rows that pre-date the protocol change.
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # Optional links back to the structured records this message
    # represents — set when a NegotiationMessage is created as the
    # spine of a Counter or Blocker event. Used by the unified-
    # conversation timeline (Step 4 of model proposal) to render
    # structured payloads inline. Nullable on free-text chat rows.
    counter_proposal_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("counter_proposals.id"), nullable=True, index=True)
    blocker_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("blockers.id"), nullable=True, index=True)
    # NULL on free-text chat rows. Set on rows that are the spine of
    # a structured event: 'proposal' | 'resolution' | 'blocker' |
    # 'blocker_resolution'. The renderer uses this + the FK columns to
    # decide between a regular chat bubble and a structured-event bubble.
    # 40, not 20 (alembic 0106): the blocker split introduced
    # 'blocker_status_update' — 21 chars, which silently overflowed the old width and
    # raised StringDataRightTruncation on insert. Leaves room for future event kinds.
    event_kind: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    thread: Mapped["NegotiationThread"] = relationship(back_populates="messages")


class CounterProposal(Base):
    """A formal partner proposal to modify rollout terms.

    Distinct from a NegotiationMessage (which is a free-text Q&A entry):
    a CounterProposal is a contract-level transaction with status that
    gates the rollout state machine. While `status='open'`, the
    assignment cannot transition past ACCEPTED — the partner can't
    start implementation against terms they're still negotiating.

    PM resolves via the /accept or /reject endpoint. Either way, the Authority
    sends a CLARIFICATION_RESPONSE back to the partner so they see
    the decision in their thread.
    """
    __tablename__ = "counter_proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(String(36), ForeignKey("change_requests.id"), nullable=False, index=True)
    partner_id: Mapped[str] = mapped_column(String(36), ForeignKey("partner_agents.id"), nullable=False, index=True)
    assignment_id: Mapped[str] = mapped_column(String(36), ForeignKey("change_partner_assignments.id"), nullable=False, index=True)

    counter_proposal_id: Mapped[str] = mapped_column(String(64), nullable=False)  # originator-supplied id
    status: Mapped[CounterProposalStatus] = mapped_column(_enum(CounterProposalStatus), nullable=False, default=CounterProposalStatus.OPEN)
    # Which side proposed: 'partner' (default — bank counters the Authority's
    # rollout proposal) or 'npci' (PM counters back partner's terms).
    # Each round of multi-round negotiation alternates.
    originator: Mapped[str] = mapped_column(String(20), nullable=False, default="partner")
    negotiation_round: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # full inbound payload for audit

    # ── Negotiation classification fields (migration 0056) ────────────────
    # Structured request type: timeline|scope|limits|api_contract|dependency|cert_role
    request_category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # BRD evaluation result set by the classifier agent
    brd_classification: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # What the system decided to do automatically (before PM review)
    auto_disposition: Mapped[str] = mapped_column(String(30), nullable=False, default=AutoDisposition.PENDING.value)
    # FK to the cluster this CP was grouped into (nullable until clustering runs)
    cluster_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    resolution_text: Mapped[str | None] = mapped_column(Text, nullable=True)


class Blocker(Base):
    """Partner-reported obstacle mid-implementation.

    Per the rollout-doc Journey C: when a partner discovers a real
    technical impediment after PROPOSAL_ACCEPTANCE, they emit a
    structured BLOCKER with their investigation evidence + options
    they considered. Assignment.status flips to BLOCKED until PM
    resolves it via a BLOCKER_RESOLUTION (which can carry an updated
    artifact — e.g., a patched SDK).

    Distinct from a CounterProposal: a counter is about negotiating
    terms before/during acceptance; a blocker is about getting unstuck
    after acceptance, with the Authority typically extending timelines without
    penalty.
    """
    __tablename__ = "blockers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(String(36), ForeignKey("change_requests.id"), nullable=False, index=True)
    partner_id: Mapped[str] = mapped_column(String(36), ForeignKey("partner_agents.id"), nullable=False, index=True)
    assignment_id: Mapped[str] = mapped_column(String(36), ForeignKey("change_partner_assignments.id"), nullable=False, index=True)

    blocker_id: Mapped[str] = mapped_column(String(64), nullable=False)  # partner-supplied (e.g. BLK-001)
    severity: Mapped[BlockerSeverity] = mapped_column(_enum(BlockerSeverity), nullable=False)
    status: Mapped[BlockerStatus] = mapped_column(_enum(BlockerStatus), nullable=False, default=BlockerStatus.OPEN)

    description: Mapped[str] = mapped_column(Text, nullable=False)
    impact: Mapped[str | None] = mapped_column(Text, nullable=True)
    investigation_done: Mapped[list | None] = mapped_column(JSON, nullable=True)
    options_considered: Mapped[list | None] = mapped_column(JSON, nullable=True)
    requested_action_from_npci: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # full inbound payload for audit

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    resolution_action: Mapped[str | None] = mapped_column(Text, nullable=True)  # which option was picked
    resolution_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_artifact_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)


# ── New negotiation models ────────────────────────────────────────────────────

class BRDRequirement(Base):
    """PM-configured requirement flags for a given change.

    The hybrid BRD classification model: PM marks which requirements are
    mandatory (counter-proposals touching those are auto-rejected); for
    optional requirements, the classifier agent evaluates tolerance using
    `tolerance_config` (e.g. {"date_shift_days": 14}).
    """
    __tablename__ = "brd_requirements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(String(36), ForeignKey("change_requests.id"), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False, default="general")
    is_mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tolerance_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 'manual' (PM-entered) or 'ai' (extracted + classified from the BRD by the
    # brd_extractor agent). PM edits/toggles win regardless of source.
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="manual", server_default="manual")
    # One-line LLM rationale for the mandatory/optional call — shown as a hint
    # in the Negotiation Hub so the PM can sanity-check the classification.
    ai_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class NegotiationRoundState(Base):
    """Per-partner, per-change round 1/2 deadline tracking.

    Round 1: created when partner sends PROPOSAL_ACKNOWLEDGED; deadline = 24h.
    Round 2: created when the Authority sends a counter-back; deadline = 24h.
    Silent acceptance: background task sets status=silently_accepted once
    deadline passes with no partner response.
    """
    __tablename__ = "negotiation_round_states"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(String(36), ForeignKey("change_requests.id"), nullable=False, index=True)
    partner_id: Mapped[str] = mapped_column(String(36), ForeignKey("partner_agents.id"), nullable=False, index=True)
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default=RoundStatus.OPEN.value)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    silent_acceptance_cp_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class NegotiationCluster(Base):
    """Cross-partner grouping of similar counter-proposals for a change.

    Built in real-time: each incoming CP is routed by an LLM (cluster_router)
    into an existing cluster with the same underlying ask, or into a new one.
    The AI then generates a summary + recommendation + confidence score for the
    PM's cluster decision view. `cluster_key` is a stable deterministic label
    (f"{category}::{topic_slug}") kept for debugging — it is no longer the
    match criterion.
    """
    __tablename__ = "negotiation_clusters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(String(36), ForeignKey("change_requests.id"), nullable=False, index=True)
    cluster_key: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    topic_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    partner_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_recommendation: Mapped[str | None] = mapped_column(String(20), nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    pm_decision: Mapped[str] = mapped_column(String(20), nullable=False, default=ClusterPMDecision.PENDING.value)
    pm_decision_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    pm_modified_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    pm_decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pm_decided_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    conflict_with_cluster_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    members: Mapped[list["NegotiationClusterMember"]] = relationship(back_populates="cluster", cascade="all, delete-orphan")


class NegotiationClusterMember(Base):
    """Maps individual counter_proposals into their cluster."""
    __tablename__ = "negotiation_cluster_members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    cluster_id: Mapped[str] = mapped_column(String(36), ForeignKey("negotiation_clusters.id"), nullable=False, index=True)
    counter_proposal_id: Mapped[str] = mapped_column(String(36), ForeignKey("counter_proposals.id"), nullable=False, index=True)
    partner_id: Mapped[str] = mapped_column(String(36), ForeignKey("partner_agents.id"), nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    cluster: Mapped["NegotiationCluster"] = relationship(back_populates="members")


class CertRun(Base):
    __tablename__ = "cert_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(String(36), ForeignKey("change_requests.id"), nullable=False)
    partner_id: Mapped[str] = mapped_column(String(36), ForeignKey("partner_agents.id"), nullable=False)
    # Protocol v1 (§5): cflow_id is the master identifier threading the whole
    # certification lifecycle for one (change, partner) cert effort — stable
    # across re-runs. `cert_attempt` in the wire protocol maps to `run_number`
    # below (the run-level attempt within a cflow_id). Migration 0067.
    cflow_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    passed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    skipped: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[CertRunStatus] = mapped_column(_enum(CertRunStatus), nullable=False, default=CertRunStatus.RUNNING)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Stamped when the partner confirms receipt of the all-PASS
    # cert_completion_signoff (migration 0050). A stronger non-repudiation
    # event than partner_acknowledged_at: The Authority asserting "this run is final
    # and the partner is certified", and the partner accepting that assertion.
    completion_signed_off_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # CERT-6 round audit (migration 0131): who dispatched this run and the
    # chain back to what triggered it. `dispatched_by` is 'operator' (default
    # behaviour, also NULL on legacy rows) or 'auto' (the loop);
    # `previous_run_id` links round N to round N-1; the message id is the
    # inbound fix notification the auto-dispatch acted on.
    dispatched_by: Mapped[str | None] = mapped_column(String(20), nullable=True)
    previous_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    fix_notification_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # CERT-7 (migration 0136): the C-1 coverage note AS BUILT for this round —
    # uncoverable APIs, unconstrained changed fields, §3.1 gaps, the baseline
    # fallback flag. Copied, never re-derived: a mid-cert registry edit must
    # not retroactively change what a round's report says was covered.
    coverage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # SIM-6/I-6b (migration 0133): which contract graded this run (§3.4) and
    # which CLASS executed each side (simulator | application) — the mode
    # axis layers on the harness axis, never merged into one enum.
    pack_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    pack_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    npci_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    partner_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)

    results: Mapped[list["CertTestResult"]] = relationship(back_populates="cert_run", cascade="all, delete-orphan")


class CertTestResult(Base):
    __tablename__ = "cert_test_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    cert_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("cert_runs.id"), nullable=False)
    test_case_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    direction: Mapped[CertDirection] = mapped_column(_enum(CertDirection), nullable=False)
    status: Mapped[CertTestStatus] = mapped_column(_enum(CertTestStatus), nullable=False)
    expected_response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    actual_response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # SIM-6/I-6b (migration 0133) — see CertRun; per-result because a round
    # may mix modes (bank-initiated legs) and §3.4 wants the contract ON the
    # result row, not joined at read time.
    pack_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    pack_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    npci_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    partner_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    cert_run: Mapped["CertRun"] = relationship(back_populates="results")
    triage: Mapped["CertTriage | None"] = relationship(back_populates="test_result", uselist=False)


class CertTriage(Base):
    __tablename__ = "cert_triage"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    cert_test_result_id: Mapped[str] = mapped_column(String(36), ForeignKey("cert_test_results.id"), nullable=False)
    ai_verdict: Mapped[TriageVerdict] = mapped_column(_enum(TriageVerdict), nullable=False)
    ai_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_override: Mapped[str | None] = mapped_column(String(50), nullable=True)
    final_verdict: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    test_result: Mapped["CertTestResult"] = relationship(back_populates="triage")


class CertWaiver(Base):
    """Protocol v1 cert waiver (§7.8–7.9). A partner requests a waiver for a
    cert case it cannot/will not pass; the Authority's Risk+Product gate grants/rejects.
    Migration 0068."""

    __tablename__ = "cert_waivers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(String(36), ForeignKey("change_requests.id"), nullable=False)
    partner_id: Mapped[str] = mapped_column(String(36), ForeignKey("partner_agents.id"), nullable=False)
    cflow_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    case_id: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str | None] = mapped_column(String(40), nullable=True)   # non_applicable|deferred|infeasible|policy
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # requested → granted | rejected
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="requested")
    conditions: Mapped[str | None] = mapped_column(Text, nullable=True)
    valid_until: Mapped[str | None] = mapped_column(String(40), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CertFlowState(TimestampMixin, Base):
    """Persisted certification lifecycle state — one row per cflow_id (CERT-0).

    `phase` holds the `precert_engine.state_machine.Phase` VALUE. `history` is
    an append-only list of `[trigger, phase, at]` transition triples that
    accumulates ACROSS rounds: each dispatch builds a fresh in-memory
    `FlowState` and appends what IT fired (see `cert_agent/flow_store.py` for
    the watermark that makes that safe). `halted_reason` is written by the
    loop (CERT-6) when it stops dispatching — the operator who unblocks the
    change reads it here.

    Deliberately domain-neutral (authority/partner, never a network's name)
    and FK-free: the flow record is an audit trail that must outlive whatever
    happens to the rows around it. Migration 0131.
    """

    __tablename__ = "cert_flow_states"

    cflow_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    change_request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    partner_id: Mapped[str] = mapped_column(String(36), nullable=False)
    phase: Mapped[str] = mapped_column(String(30), nullable=False, default="NOT_STARTED")
    current_round: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    history: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    halted_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        Index("ix_cert_flow_states_change_partner", "change_request_id", "partner_id"),
    )


class CertRequestVariant(TimestampMixin, Base):
    """One executable input combination for one certification case (§3.1).

    A variant executes ONCE; the assertion rows that grade its captured
    request/response reference it. A materially different value combination is
    a different variant with its own row. `variant_id` is the deterministic
    content hash from `cert_variants.variant_id_for` — same registry snapshot,
    rules and test data → same ids, which is what makes re-dispatch replacement
    (not accumulation) checkable. Migration 0132.

    Domain-neutral on purpose: `initiator` is authority|partner (the wire's
    npci|bank spelling is mapped at the wire boundary), and `wire_format` is a
    SNAPSHOT of the pack's codec key at generation time so re-evaluating an
    old round never consults the pack.
    """

    __tablename__ = "cert_request_variants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    cflow_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_number: Mapped[int] = mapped_column(Integer, nullable=False)
    case_id: Mapped[str] = mapped_column(String(100), nullable=False)
    variant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    api_message_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_messages.id"), nullable=True)
    initiator: Mapped[str] = mapped_column(String(10), nullable=False, default="authority")
    wire_format: Mapped[str] = mapped_column(String(20), nullable=False, default="xml")
    input_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    fixture_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    expected: Mapped[dict] = mapped_column(JSON, nullable=False)
    # template|pairwise|three_way|enumerated|boundary|negative|baseline|manual
    strategy: Mapped[str] = mapped_column(String(30), nullable=False)
    covered_rules: Mapped[list | None] = mapped_column(JSON, nullable=True)
    is_negative: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # The ONE intentionally-invalid field of a negative variant — one fault at
    # a time, so a rejection is attributable.
    fault_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    provenance: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        # The key includes case_id: a variant is identified WITHIN its case.
        # Omitting it made two cases whose request inputs happen to match
        # collide and abort the whole round's storage.
        UniqueConstraint("cflow_id", "run_number", "case_id", "variant_id",
                         name="uq_cert_request_variants_round_case_variant"),
        Index("ix_cert_request_variants_round_case", "cflow_id", "run_number", "case_id"),
    )


class CertCaseSpec(TimestampMixin, Base):
    """One ASSERTION over one variant's captured payload (CERT-1).

    One row is one rule evaluated, NOT one executed case: a message with forty
    constrained fields is forty-odd rows and ONE simulator transaction.
    `expected` and `field_path` are COPIED from the registry at generation
    time — a mid-cert registry edit must not retroactively change what a round
    asserted, and a self-contained row is what lets the assertion engine stay
    model-free. `authority_data` ships on the wire under the protocol's
    `authority_batch` key (partner-shared; mapped at the wire boundary).
    Migration 0132.
    """

    __tablename__ = "cert_case_specs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    cflow_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_number: Mapped[int] = mapped_column(Integer, nullable=False)
    case_id: Mapped[str] = mapped_column(String(100), nullable=False)
    # FK to the variant ROW this spec grades (cert_request_variants.id, not the
    # content hash). Nullable: harness-baseline fallback specs may predate any
    # generated variant.
    variant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("cert_request_variants.id"), nullable=True)
    api_message_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_messages.id"), nullable=True)
    api_field_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_fields.id"), nullable=True)
    field_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # occurrence|datatype|length|mandatory|enum|pattern|response_code
    assertion_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    expected: Mapped[dict] = mapped_column(JSON, nullable=False)
    # registry_delta|harness_baseline|manual
    origin: Mapped[str] = mapped_column(String(20), nullable=False)
    wire_format: Mapped[str] = mapped_column(String(20), nullable=False, default="xml")
    authority_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_cert_case_specs_round_case", "cflow_id", "run_number", "case_id"),
    )
