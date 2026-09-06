# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Generic admin ACTION audit recorder — completes THREAT_MODEL.md T8.

T8 is "no COMPREHENSIVE admin action audit log". `core/admin_action_audit.py`
supplied the writer and two endpoints call it explicitly, but that leaves the
actual gap open: coverage that depends on every developer remembering to add a
`record()` call to every new admin endpoint is not comprehensive, and silently
degrades over time as endpoints are added. This middleware closes it by
recording EVERY mutating request that passed an admin authorization check.

Design notes
------------
**Keyed on the admin dependency, not a URL prefix.** `core/deps.py::
require_admin` stamps `request.state.admin_audit_user_id` once it has
authorized the caller. This middleware records a row only when that marker is
present. That matters because admin-gated routes are NOT all under
`/api/admin/*` — `users.py` (`/api/users`), `rag.py` (`/api/rag`),
`change_requests.py`/`resolver.py` (`/api/changes`), `logs.py` (`/api/logs`),
`eval.py`, `agents.py` and others also depend on `AdminUser`. A prefix-keyed
middleware (the shape `admin_rate_limit.py` uses, which is correct for
throttling) would silently miss those and recreate T8's gap in a new form.
Keying on "did the admin dependency actually grant access" is exact by
construction and cannot drift as routers are reorganised.

**Mutating methods only.** GET/HEAD/OPTIONS are reads; recording them would
bury real state changes under routine dashboard polling. Read auditing, if
ever wanted, is a different requirement (access logging) with a different
retention profile — conflating the two would make this table unusable for its
stated purpose.

**Only successful actions.** A 4xx/5xx means the action did not take effect
(or is already covered by the auth-failure trail in `auth_audit`). Recording
rejected attempts here would mix "what changed" with "what was attempted",
again defeating the table's purpose. Rejected ADMIN attempts are still
observable: `require_admin` raises 403 and the rate limiter emits its own
`SECURITY_EVENT`.

**Never double-records.** An endpoint that already wrote a rich, semantic row
(e.g. `partner.rotate_hmac_secret`, with a before/after key-version diff) sets
`request.state.admin_audit_recorded = True` via `mark_explicitly_recorded()`.
This middleware then skips it, so the richer record stands alone rather than
being shadowed by a generic duplicate of the same action.

**Fail-open, always.** Identical policy to `admin_action_audit.record` and
`auth_audit.record`: an audit write failure is logged and swallowed. An audit
subsystem that can take down the admin API is a worse outcome than a missing
audit row, and this middleware runs on the response path where the action has
already been committed — refusing the request at that point is not even
possible, only corrupting the response would be.

**Its own DB session.** The request's session is closed by the time the
response is produced, so this opens a short-lived session purely for the audit
insert. That also guarantees the audit write cannot roll back or otherwise
disturb the transaction that performed the action.
"""
from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Query strings can carry sensitive values (tokens, filters containing
# identifiers). The path alone is the useful audit fact; the query string is
# dropped rather than persisted, per the same "never persist a secret into the
# audit trail" rule `admin_action_audit.redact_secret_fields` enforces for
# before/after dicts.
_MAX_PATH_LEN = 500


def mark_explicitly_recorded(request) -> None:
    """Call from an endpoint that has already written its own rich audit row,
    so the generic middleware does not add a second, lower-fidelity row for
    the same action.

    Safe to call unconditionally — tolerates a missing/read-only `state`.
    """
    try:
        request.state.admin_audit_recorded = True
    except Exception:  # noqa: BLE001
        pass


def _action_name_for(method: str, path: str) -> str:
    """Derive a stable, readable action name from the request.

    Example: POST /api/admin/partners/<id>/rotate-key -> "admin.post.partners.rotate-key"

    UUID-ish and numeric path segments are dropped so the same logical action
    against different resources aggregates under one action name (the specific
    resource is already recorded in `path`/`resource_id`). This keeps the
    `action` column queryable ("how many rotate-key actions this month")
    rather than producing one distinct value per resource id.
    """
    parts = [p for p in (path or "").split("/") if p]
    # Strip the routing prefix ("api", then an optional "admin").
    if parts and parts[0] == "api":
        parts = parts[1:]
    if parts and parts[0] == "admin":
        parts = parts[1:]
    keep: list[str] = []
    for p in parts:
        # Drop identifier-looking segments: long hex/uuid strings and pure numbers.
        if p.isdigit():
            continue
        if len(p) >= 16 and all(c.isalnum() or c in "-_" for c in p) and any(c.isdigit() for c in p):
            continue
        keep.append(p)
    slug = ".".join(keep[:4]) or "root"
    return f"admin.{method.lower()}.{slug}"[:80]


def _resource_id_from_path(path: str) -> str | None:
    """Best-effort: the last identifier-looking path segment, so a generic row
    still points at the specific resource acted upon."""
    for p in reversed([p for p in (path or "").split("/") if p]):
        if p.isdigit():
            return p[:36]
        if len(p) >= 16 and all(c.isalnum() or c in "-_" for c in p) and any(c.isdigit() for c in p):
            return p[:36]
    return None


class AdminActionAuditMiddleware(BaseHTTPMiddleware):
    """Records one `admin_action_audit` row per successful, mutating,
    admin-authorized request that did not already record its own."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)

        try:
            from app.core.config import settings
            if not getattr(settings, "admin_action_audit_enabled", True):
                return response
            if request.method.upper() not in _MUTATING_METHODS:
                return response

            state = request.state
            user_id = getattr(state, "admin_audit_user_id", None)
            if not user_id:
                # Not an admin-authorized request (require_admin never ran, or
                # ran and rejected) — nothing for THIS table to record.
                return response
            if getattr(state, "admin_audit_recorded", False):
                # The endpoint already wrote a richer, semantic row.
                return response
            status_code = getattr(response, "status_code", 0) or 0
            if not (200 <= status_code < 400):
                # Action did not take effect — see module docstring.
                return response

            self._write_row(request, user_id, status_code)
        except Exception:  # noqa: BLE001 — auditing must never break the response
            logger.exception("admin action audit middleware failed (response unaffected)")

        return response

    @staticmethod
    def _write_row(request, user_id: str, status_code: int) -> None:
        from app.core import admin_action_audit
        from app.core.database import SessionLocal

        path = (request.url.path or "")[:_MAX_PATH_LEN]
        method = request.method.upper()
        client = getattr(request, "client", None)

        db = SessionLocal()
        try:
            admin_action_audit.record(
                db,
                user_id=user_id,
                username=getattr(request.state, "admin_audit_username", None),
                action=_action_name_for(method, path),
                resource_type=None,
                resource_id=_resource_id_from_path(path),
                # No field-level diff: the middleware cannot know what changed.
                # `source="middleware"` marks this row as HTTP-level evidence
                # so a reviewer never mistakes it for an endpoint-authored diff.
                before=None,
                after=None,
                ip=client.host if client else None,
                detail=None,
                http_method=method,
                path=path,
                status_code=status_code,
                source="middleware",
            )
        finally:
            db.close()
