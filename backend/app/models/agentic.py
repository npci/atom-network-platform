# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Agentic XSD-driven code-change subsystem — durable state-machine models.

This is the persistent spine for the agentic Phase-B replacement (THE BOOK
v3.3, §3/§9/§10/§11). The legacy single-shot Phase-B path
(``phase_b.py``) is untouched and runs when the feature flag is off.

Design notes for the next session:

* **State-machine fields are plain ``String`` columns, not native enums.**
  ``phase``/``status``/``decision``/``category``/``source`` carry companion
  ``str, enum.Enum`` classes for in-code validation, but the DB column is a
  ``String`` so adding a new phase never requires an ``ALTER TYPE`` migration.
  The state machine is still settling (the plan's §3 diagram does not yet draw
  ``rebase_reverify``), so we keep these fields cheap to evolve. This is a
  deliberate deviation from the repo's usual ``_enum()`` convention.
* **JSON columns use the generic ``JSON`` type** (as ``CodeIteration.files_changed``
  and ``CodePlan.plan_data`` already do), not Postgres ``JSONB``.
"""
import enum
from datetime import datetime

from sqlalchemy import String, Text, Integer, Boolean, Float, ForeignKey, JSON, DateTime
from sqlalchemy.orm import mapped_column, Mapped, relationship

from app.core.database import Base
from app.models.base import generate_uuid, utcnow


# ── Enumerations (validation in code; stored as String) ────────────────────────

class AgenticPhase(str, enum.Enum):
    """Operational position in the §3 state machine (where the run *is*)."""
    PENDING                = "pending"
    WORKSPACE_READY        = "workspace_ready"
    CONTEXT_READY          = "context_ready"
    XSD_DISCOVERY          = "xsd_discovery"
    # Change-Analysis stage (kind='analysis', accuracy upgrade S2): read code →
    # ask the PM → propose the plan. Read-only; no manifest/push.
    ANALYZING              = "analyzing"
    AWAITING_CLARIFICATIONS = "awaiting_clarifications"   # PM clarification batch gate
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"     # PM + tech-lead plan ratification gate
    AWAITING_APPROACH_DECISION = "awaiting_approach_decision"  # reuse-vs-new options gate
    AWAITING_XSD_APPROVAL  = "awaiting_xsd_approval"   # Phase-A gate (kind='xsd')
    # ADR-0005 / SDLC review gap 4 — the TSD approval + version-lock gate (only
    # reached when agentic_tsd_approval_gate_enforce=True and the change's TSD is
    # not status=APPROVED at CODE_CHANGE entry). Parks the run instead of
    # generating code against an unapproved contract.
    AWAITING_TSD_APPROVAL  = "awaiting_tsd_approval"
    CODE_CHANGE            = "code_change"
    VERIFICATION           = "verification"
    # After the auto-retry budget is spent (3 failed verifications) the run PARKS
    # here for a human decision — retry once more, or skip verification and proceed.
    AWAITING_VERIFY_DECISION = "awaiting_verify_decision"
    # A3 — the code agent surfaced a decision it must not make itself (a binding
    # directive conflicts with code reality, or a required decision is missing):
    # the run parks here until a human answers via decide-code-decision.
    AWAITING_CODE_DECISION = "awaiting_code_decision"
    # Fix 2 — the code phase needs a change to the human-approved schema. It cannot make
    # one itself, so the exact edit is STAGED and the run parks here until a human approves
    # it (applied verbatim, then code resumes) or rejects it (a binding directive to
    # implement around it). Without this gate a genuinely-needed schema fix had nowhere to
    # go: the write was refused, the reviewer kept demanding it, and the run deadlocked.
    AWAITING_SCHEMA_AMENDMENT = "awaiting_schema_amendment"
    REVIEW                 = "review"
    AWAITING_HUMAN_APPROVAL = "awaiting_human_approval"
    PUSHING                = "pushing"
    REBASE_REVERIFY        = "rebase_reverify"
    COMPLETED              = "completed"
    FAILED                 = "failed"
    GAVE_UP                = "gave_up"
    CANCELLED              = "cancelled"


class AgenticStatus(str, enum.Enum):
    """Lifecycle bucket driving the partial-unique active-run constraint (§3).

    ``awaiting_human_approval`` and ``rebase_reverify`` keep status ``active`` —
    only these four are terminal and release the active-run uniqueness.
    """
    ACTIVE    = "active"
    COMPLETED = "completed"
    FAILED    = "failed"
    GAVE_UP   = "gave_up"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset(
    {AgenticStatus.COMPLETED, AgenticStatus.FAILED,
     AgenticStatus.GAVE_UP, AgenticStatus.CANCELLED}
)


class VerifyDecision(str, enum.Enum):
    VERIFIED  = "verified"
    NEEDS_FIX = "needs_fix"
    GAVE_UP   = "gave_up"
    FAILED    = "failed"


class ReviewSeverity(str, enum.Enum):
    INFO     = "info"
    WARNING  = "warning"
    ERROR    = "error"
    BLOCKER  = "blocker"


class ReviewCategory(str, enum.Enum):
    CORRECTNESS = "correctness"
    SECURITY    = "security"
    CONVENTION  = "convention"
    REUSE       = "reuse"
    REGULATORY  = "regulatory"


# ── Models ─────────────────────────────────────────────────────────────────────

class AgenticRun(Base):
    """The durable spine (§3). One row per agentic Phase-B run.

    A partial-unique index (migration M-D) enforces at most one row per
    ``change_request_id`` whose ``status = 'active'`` — terminal runs release
    it so a failed/abandoned change can be retried as a fresh run.
    """
    __tablename__ = "agentic_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("change_requests.id"), nullable=False, index=True
    )
    phase: Mapped[str] = mapped_column(String(40), nullable=False, default=AgenticPhase.PENDING.value)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=AgenticStatus.ACTIVE.value)

    # Phase A/B split (THE BOOK v3.4): a run is "full" (legacy single run, xsd→code
    # chained), "xsd" (Phase A — generate + human-approve the schema, then stop), or
    # "code" (Phase B — adopt Phase A's workspace and finish the code, one combined MR).
    kind: Mapped[str] = mapped_column(String(8), nullable=False, default="full", server_default="full")
    # The Phase-A run this Phase-B run continues (null for full/xsd runs).
    parent_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agentic_runs.id"), nullable=True
    )
    # The run whose on-disk clone holds the working tree. null ⇒ self; a Phase-B run
    # points this at its parent so it edits/verifies/pushes Phase A's XSD tree (combined MR).
    workspace_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Phase-A → Phase-B handoff: serialized XsdScope + full content of each changed XSD
    # file, so Phase B inherits the schema decisions and can re-materialize the XSDs if
    # the shared workspace was GC'd before it starts. No context gap across the two runs.
    handoff_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # {phase_value: attempt_count} — drives the iteration/verify caps (§3).
    attempts_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    selected_repo_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Durable progress ledger. Two uses:
    #  - "blocker_keys": stall-escalation window — written by DEFAULT (gated only by
    #    agentic_max_stall_rounds > 0) so a resumed run keeps its stall window.
    #  - "files_read"/"verify_failures": CODE_CHANGE-boundary re-derivation skip —
    #    gated by agentic_progress_ledger (default OFF). ADVISORY only: a file whose
    #    sha changed (or that the agent edited) is never suppressed from re-read;
    #    tried-fixes are surfaced as context, never as a prohibition.
    # NOTE: because of the stall ledger, this column IS written by default operation
    # (not purely opt-in) — the column exists via migration 0090.
    progress_ledger_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Lease (§6): renewed by heartbeat; recovery beat resumes expired leases.
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # platform.system() captured by the ToolchainReport preflight (§18.1).
    platform: Mapped[str | None] = mapped_column(String(20), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Structured failure category for ops triage (e.g. anthropic_rate_limit,
    # git_clone_timeout, verify_timeout) — set alongside `error` on terminal failure.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The user who started the run — per-run authz (author + admin only) + audit.
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Last time the driving worker renewed its lease — lets the health endpoint
    # flag a run as stuck (active but no heartbeat for > 2 lease windows).
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ADR-0005 / SDLC review gap 4 — the TechSpec.version that was APPROVED at the
    # moment this run entered CODE_CHANGE (migration 0125). read_doc(doc='tsd')
    # resolves against THIS version, not "latest" — a TSD regenerated mid-run
    # cannot change the contract under the code agent. NULL on a run that has not
    # yet passed the TSD approval gate (or a legacy run created before this
    # column existed) — callers must treat NULL as "resolve latest" for
    # backward compatibility, never as "resolve version 0".
    tsd_version_locked: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    run_repos: Mapped[list["AgenticRunRepo"]] = relationship(
        "AgenticRunRepo", back_populates="run", cascade="all, delete-orphan"
    )
    events: Mapped[list["AgenticEvent"]] = relationship(
        "AgenticEvent", back_populates="run", cascade="all, delete-orphan",
        order_by="AgenticEvent.seq",
    )
    manifest: Mapped["ChangeManifest | None"] = relationship(
        "ChangeManifest", back_populates="run", uselist=False, cascade="all, delete-orphan"
    )
    verification_runs: Mapped[list["VerificationRun"]] = relationship(
        "VerificationRun", back_populates="run", cascade="all, delete-orphan"
    )
    review_findings: Mapped[list["ReviewFinding"]] = relationship(
        "ReviewFinding", back_populates="run", cascade="all, delete-orphan"
    )


class AgenticRunRepo(Base):
    """Per-repo state inside a run (§12) — branch, MR, idempotent push state.

    Distinct from the legacy ``PhaseBRunRepo`` (which scopes the old Phase-B
    runs); this one is scoped to ``agentic_runs``.
    """
    __tablename__ = "agentic_run_repos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agentic_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    repo_id: Mapped[str] = mapped_column(String(36), ForeignKey("code_repos.id"), nullable=False)
    base_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(200), nullable=True)
    mr_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # "pending" | "pushed" | "skipped" — idempotent resumable push (§12).
    push_state: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # manifest_hash that was on git when this repo was pushed. A later re-freeze that
    # changes the run's manifest makes this row STALE (branch holds older approved
    # content) — _push_all then re-pushes instead of treating "pushed once" as done.
    # NULL on legacy rows = unknown → treated as current (never auto re-pushed).
    pushed_manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    run: Mapped["AgenticRun"] = relationship("AgenticRun", back_populates="run_repos")


class AgenticEvent(Base):
    """Append-only event log — the source of truth the WebSocket replays (§3/§21)."""
    __tablename__ = "agentic_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agentic_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    run: Mapped["AgenticRun"] = relationship("AgenticRun", back_populates="events")


class LlmUsageRecord(Base):
    """One persisted LLM-call usage row — the source of truth for the Usage dashboard.

    Written best-effort at the single observability chokepoint (`core.observability.record_llm_call`),
    so it captures EVERY call — agentic code-gen AND the non-flow agents (BRD/TSD/docgen/eval/
    Family-A helpers). ``change_request_id`` / ``run_id`` / ``kind`` come from a usage-context
    contextvar set by the orchestrator (codegen) when available; calls outside any change context
    land with those null and roll up under 'other (non-flow) usage by section'. ``section`` is the
    agent name. Append-only telemetry — never on the request critical path (a write failure is
    swallowed)."""
    __tablename__ = "llm_usage_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    change_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    kind: Mapped[str | None] = mapped_column(String(32), nullable=True)     # run kind / 'doc' / 'eval' / ...
    section: Mapped[str | None] = mapped_column(String(64), nullable=True)  # agent_name
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)


class ChangeManifest(Base):
    """Immutable approved manifest frozen at ``awaiting_human_approval`` (§11)."""
    __tablename__ = "change_manifests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agentic_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    selected_repo_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # [{repo_id, base_commit_sha, shared_branch_name}]
    per_repo: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # [{op: add|modify|delete, repo_id, path, content_hash, diff}]
    operations: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # {repo_id: full unified git diff} captured at freeze — the durable "changes artifact"
    # the UI shows during AND after the run (survives workspace GC + the post-push commit).
    diffs: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    verification: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    review: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # The human-ratified plan/SPEC this manifest was frozen against. Folded into
    # ``manifest_hash`` by build_manifest() so a re-ratified plan forces re-approval,
    # and persisted here as the audit trail of exactly what plan each approval was
    # granted against (freeze_manifest passes plan= unconditionally).
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    run: Mapped["AgenticRun"] = relationship("AgenticRun", back_populates="manifest")


class VerificationRun(Base):
    """One verification round — raw result + Claude reasoning + the gate verdict (§9)."""
    __tablename__ = "verification_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agentic_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timed_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    smoke_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # The runtime-owned VerificationPlan + per-gate pass/fail (§9.4).
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    gates: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    run: Mapped["AgenticRun"] = relationship("AgenticRun", back_populates="verification_runs")


class ReviewFinding(Base):
    """Anthropic review finding (§10)."""
    __tablename__ = "review_findings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agentic_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    severity: Mapped[str | None] = mapped_column(String(20), nullable=True)
    category: Mapped[str | None] = mapped_column(String(20), nullable=True)
    repo_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    file: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    why: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_fix: Mapped[str | None] = mapped_column(Text, nullable=True)
    blocking: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Always an Anthropic model id, asserted in acceptance (§16).
    reviewer_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    run: Mapped["AgenticRun"] = relationship("AgenticRun", back_populates="review_findings")


class ChangeReport(Base):
    """Deterministic context/impact report for a change (§4/§7).

    Keyed by ``(change_request_id, input_hash)`` (unique in M-D) so an
    unchanged input reuses the cached report instead of recomputing.
    """
    __tablename__ = "change_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("change_requests.id"), nullable=False, index=True
    )
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
