# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Admin ACTION audit trail — closes THREAT_MODEL.md T8.

Distinct from `auth_audit.py`: that table records AUTHENTICATION events
(login, MFA, lockout). This one records what an authenticated admin
DID — a state-changing action against a specific resource, with a
before/after snapshot of the fields that changed. Satisfies
security_architecture_skills.md §3.5's "every important control
decision... traceable" requirement for the admin surface specifically,
which §8.4's MFA requirement (closed separately, see T7) does not by
itself provide — MFA proves WHO logged in; this proves WHAT they did
once logged in.

Written best-effort by `app.core.admin_action_audit.record`; never
blocks the admin action itself (same fail-open-on-audit-write-failure
policy as `auth_audit.record`).
"""
from sqlalchemy import JSON, Integer, String, Text
from sqlalchemy.orm import mapped_column, Mapped

from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid


class AdminActionAudit(Base, TimestampMixin):
    __tablename__ = "admin_action_audit"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    # The acting admin — user_id is the durable identifier; username is
    # denormalized here so a query doesn't need a join to display a
    # human-readable audit trail even if the user account is later renamed.
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(150), nullable=True)
    # What was done — a short, stable action name (e.g.
    # "partner.rotate_hmac_secret", "partner.rate_limit_update") rather
    # than the raw HTTP method+path, so the audit trail reads clearly
    # even if routes are later restructured.
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    # What it was done TO — the resource type + id (e.g.
    # resource_type="partner_agent", resource_id="<uuid>").
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # Before/after snapshot of the CHANGED fields only (never the full
    # row — avoids incidentally persisting a secret value that happened
    # to be on the same row as the field that actually changed). NULL
    # for actions with no meaningful field-level diff (e.g. a delete).
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── T8 completion (middleware-based generic coverage) ────────────────
    # The columns above describe a SEMANTIC action, written by an endpoint
    # that knows what it changed (e.g. "partner.rotate_hmac_secret" with a
    # before/after key-version diff). That is the richest form of audit
    # record, but it only exists where a developer remembered to add an
    # explicit `record()` call — which is precisely the coverage gap T8
    # names.
    #
    # `core/admin_action_audit_middleware.py` closes that gap by recording
    # EVERY mutating /api/admin/* request generically. Such a row has no
    # field-level diff to report (the middleware cannot know what changed),
    # so it carries the HTTP-level facts instead. `source` distinguishes
    # the two kinds so a reviewer can tell a rich, endpoint-authored record
    # from a generic, middleware-authored one rather than silently
    # comparing rows of different fidelity:
    #   source="endpoint"   → written by an explicit record() call
    #   source="middleware" → written generically by request interception
    http_method: Mapped[str | None] = mapped_column(String(10), nullable=True)
    path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
