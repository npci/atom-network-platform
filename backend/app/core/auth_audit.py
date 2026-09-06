# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Best-effort writer for the authentication audit trail (InfoSec phase 4).

`record` persists one `auth_audit` row. It NEVER breaks the auth flow — a write
failure is logged and rolled back (only the audit insert). Call it at points
where the caller's own writes are already committed, so its commit/rollback can't
disturb them.
"""
import logging

logger = logging.getLogger(__name__)

# Event names (kept as constants so call sites and queries agree).
LOGIN_SUCCESS      = "login_success"
LOGIN_FAILED       = "login_failed"
CAPTCHA_FAILED     = "captcha_failed"
LOCKOUT            = "lockout"
MFA_ACTIVATED      = "mfa_activated"
MFA_VERIFY_FAILED  = "mfa_verify_failed"
MFA_DISABLED       = "mfa_disabled"
MFA_RESET          = "mfa_reset"
LDAP_PROVISIONED   = "ldap_provisioned"
LOGOUT             = "logout"
PASSWORD_CHANGED   = "password_changed"
ROLE_SWITCHED      = "role_switched"


def record(db, event: str, *, username: str | None = None, user_id: str | None = None,
           ip: str | None = None, detail: str | None = None) -> None:
    try:
        from app.models.auth_audit import AuthAudit
        db.add(AuthAudit(event=event, username=(username or None), user_id=user_id, ip=ip, detail=detail))
        db.commit()
    except Exception as e:  # noqa: BLE001 — auditing must never break auth
        logger.warning("auth audit write failed (event=%s): %s", event, e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
