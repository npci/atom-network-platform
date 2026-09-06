# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Best-effort writer for the admin ACTION audit trail — closes
THREAT_MODEL.md T8 ("No comprehensive admin ACTION audit log (vs.
auth-event audit)").

`record` persists one `admin_action_audit` row. It NEVER breaks the
admin action itself — a write failure is logged and rolled back (only
the audit insert), mirroring `app.core.auth_audit.record`'s exact
fail-open policy. Call it AFTER the caller's own writes are committed,
so the audit insert's own rollback (on failure) can't disturb the
action that already succeeded.

Redaction policy: `before`/`after` dicts should contain ONLY the
fields that changed, and callers MUST NOT pass a raw secret value
(signing_secret, jwt_signing_secret, api_key) even if that field
technically changed — pass a placeholder like `{"signing_secret":
"<rotated>"}` instead. This module does not itself redact (it has no
way to know which keys are sensitive across every possible caller), so
the discipline is enforced at the call site — see `redact_secret_fields()`
below for a shared helper that call sites should use.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Field names that must never appear with their real value in an audit
# row, across ANY resource type — extend this set as new secret-bearing
# fields are added anywhere in the admin surface.
_SECRET_FIELD_NAMES = frozenset({
    "signing_secret", "jwt_signing_secret", "api_key",
    "previous_signing_secret", "previous_jwt_signing_secret",
    "mfa_secret", "password", "secret_key",
})


def redact_secret_fields(fields: dict | None) -> dict | None:
    """Replaces any known secret-bearing field's value with a fixed
    placeholder, leaving all other fields untouched. Call sites should
    run both `before` and `after` dicts through this before passing them
    to `record()`."""
    if not fields:
        return fields
    return {
        k: ("<redacted>" if k in _SECRET_FIELD_NAMES else v)
        for k, v in fields.items()
    }


def record(
    db,
    *,
    user_id: str,
    action: str,
    username: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    ip: str | None = None,
    detail: str | None = None,
    http_method: str | None = None,
    path: str | None = None,
    status_code: int | None = None,
    source: str = "endpoint",
) -> None:
    """Persist one admin-action audit row. `before`/`after` are passed
    through `redact_secret_fields()` automatically, so call sites do not
    need to remember to do it themselves (though doing so explicitly at
    the call site for clarity is also fine — redaction is idempotent).

    `source` defaults to `"endpoint"` so every existing call site is
    correctly tagged as a rich, endpoint-authored record with no change
    required at the call site. The generic middleware passes
    `source="middleware"` plus the HTTP-level fields."""
    try:
        from app.models.admin_action_audit import AdminActionAudit
        db.add(AdminActionAudit(
            user_id=user_id,
            username=username,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            before=redact_secret_fields(before),
            after=redact_secret_fields(after),
            ip=ip,
            detail=detail,
            http_method=http_method,
            path=path,
            status_code=status_code,
            source=source,
        ))
        db.commit()
    except Exception as e:  # noqa: BLE001 — auditing must never break the admin action
        logger.warning("admin action audit write failed (action=%s): %s", action, e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
