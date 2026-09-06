# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Operational notifications — makes silent failures visible.

Background: the `notifications` table existed but was entirely dead code — nothing in the
backend ever constructed a `Notification`, there was no API router, and no UI read it. So
when an outbound A2A message to a bank failed, the ONLY trace was a `logger.error(...)`
that nobody watches, and the change quietly proceeded as if the bank had been told.

This module is the write path. Notifications are addressed to *people* (the model requires
a `user_id`), so operational alerts fan out to admins + product managers — the roles that
can actually act on a failed dispatch.

Every helper here is BEST-EFFORT: a notification failure must never break the flow that
raised it (a dispatch must not 500 because we couldn't file an alert).
"""
from __future__ import annotations

import logging

from app.models.notification import Notification, NotificationType
from app.models.user import User, UserRole

logger = logging.getLogger(__name__)

# Roles that should hear about operational failures.
_ALERT_ROLES = (UserRole.ADMIN, UserRole.PRODUCT_MANAGER)


def _alert_recipients(db) -> list[User]:
    try:
        return list(db.query(User).filter(User.role.in_(_ALERT_ROLES)).all())
    except Exception as exc:  # noqa: BLE001
        logger.warning("notification recipient lookup failed: %s", exc)
        return []


def notify(db, *, title: str, message: str, ntype: NotificationType,
           related_id: str | None = None, users: list[User] | None = None) -> int:
    """Create one notification per recipient. Returns the number written. Never raises."""
    try:
        recipients = users if users is not None else _alert_recipients(db)
        if not recipients:
            logger.warning("notify(%s): no recipients — alert dropped: %s", ntype.value, title)
            return 0
        for u in recipients:
            db.add(Notification(user_id=u.id, title=title[:500], message=message,
                                type=ntype, related_id=related_id))
        db.commit()
        return len(recipients)
    except Exception as exc:  # noqa: BLE001 — an alert must never break its caller
        logger.warning("notify(%s) failed: %s", getattr(ntype, "value", ntype), exc)
        try:
            db.rollback()
        except Exception:
            pass
        return 0


def notify_delivery_failure(db, message, partner, *, context: str = "A2A message") -> int:
    """Raise an alert that an outbound message to a bank/partner was not delivered.

    `message` is an A2AMessage row already marked `delivery_failed` / `pending`,
    OR a `core.domain.contract.Delivery` from a partner channel. Both are read
    duck-typed: the two carry the same facts under different names (`id` vs
    `reference`, `change_request_id` vs `change_id`), and without accepting both
    a channel-delivered failure would lose its task id — leaving the operator an
    alert saying "Task id: ?" and a broken resend link.

    The alert carries the task id so an operator can find it in /admin/a2a-logs
    and resend it.
    """
    _mid = getattr(message, "id", None) or getattr(message, "reference", None) or "?"
    _cid = (getattr(message, "change_request_id", None)
            or getattr(message, "change_id", None))
    pname = getattr(partner, "name", None) or getattr(message, "partner_id", "unknown")
    status = getattr(message, "status", "unknown")
    reason = (getattr(message, "error_code", None)
              or ("no A2A endpoint configured" if status == "pending" else "unknown error"))
    return notify(
        db,
        title=f"Delivery FAILED to {pname} — {context}",
        message=(
            f"{context} to partner '{pname}' was not delivered (status={status}, "
            f"error={reason}).\n"
            f"The partner has NOT received it. Task id: {_mid}, "
            f"change: {_cid or '?'}.\n"
            "Resend from Admin → A2A Logs, or POST "
            f"/admin/a2a-logs/{_mid}/resend."
        ),
        ntype=NotificationType.DELIVERY_FAILED,
        related_id=_cid,
    )


def notify_mandatory_rejection(db, *, change_id: str, partner_name: str,
                               cp_id: str, requirement_label: str | None,
                               reason: str | None = None) -> int:
    """Raise an alert that a partner counter-proposal was auto-rejected for violating a
    BRD requirement marked mandatory."""
    req = requirement_label or "a mandatory BRD requirement"
    return notify(
        db,
        title=f"Counter-proposal auto-rejected ({partner_name}) — violates {req}",
        message=(
            f"Partner '{partner_name}' submitted a counter-proposal that conflicts with "
            f"the mandatory BRD requirement: {req}.\n"
            + (f"Assessment: {reason}\n" if reason else "")
            + f"It was auto-rejected and the partner has been notified. "
              f"Counter-proposal: {cp_id}, change: {change_id}."
        ),
        ntype=NotificationType.MANDATORY_REJECTION,
        related_id=change_id,
    )


def notify_round_silently_closed(
    db, *, change_id: str, partner_name: str, round_number: int,
    cap_reached: bool = False,
) -> int:
    """Raise an alert that a negotiation round auto-closed (24h lapse with no
    partner response). Silent-close is intentionally passive — the PM would
    otherwise never notice a bank went quiet and any implicit acceptances
    could sit unreviewed for days. This fires per (change, partner, round)
    the sweep flips, so PM/admin see it in the notification tray in real-time.
    """
    tail = (" · Negotiation cap reached — change is now frozen."
            if cap_reached else "")
    return notify(
        db,
        title=(
            f"Round {round_number} auto-closed (no response from {partner_name})"
            + (" — negotiation frozen" if cap_reached else "")
        ),
        message=(
            f"The {round_number}-th negotiation round for change {change_id} "
            f"closed automatically because partner '{partner_name}' did not "
            "respond within the round window. Any open counter-proposals for "
            "this round were treated as silently accepted.{tail}\n"
            "Review the Negotiation Hub for this change to confirm the auto-"
            "accepted decisions before the next kit revision ships."
        ).format(tail=tail),
        ntype=NotificationType.INFO,
        related_id=change_id,
    )


def notify_brd_guard_inactive(db, *, change_id: str, cp_id: str) -> int:
    """Warn that the BRD mandatory guard could not run because the change has NO
    requirements configured — so nothing can be auto-rejected and every counter-proposal
    silently escalates to a human with zero BRD grounding."""
    return notify(
        db,
        title="BRD mandatory guard INACTIVE — no requirements configured",
        message=(
            f"A counter-proposal ({cp_id}) was received for change {change_id}, but that "
            "change has NO rows in brd_requirements, so the mandatory-violation check could "
            "not run and the proposal was escalated unchecked.\n"
            "Generate them via POST /changes/{change_id}/brd-requirements/generate "
            "(or Phase C → BRD Requirements) so mandatory items are enforced."
        ),
        # Categorised with the BRD/negotiation alerts, not delivery — this is about the
        # mandatory guard, and mis-typing it makes the alert list read wrong.
        ntype=NotificationType.MANDATORY_REJECTION,
        related_id=change_id,
    )
