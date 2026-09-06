# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Notifications API — the read side of operational alerts.

The `notifications` table existed but was entirely dead: nothing wrote it, no router
exposed it, no UI read it. `app.services.notifications` now writes operational alerts
(failed bank deliveries, BRD mandatory auto-rejections); without this router those rows
would be write-only, which is barely better than the log line they replaced.

Scoped to the calling user — a notification is addressed to a person.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from app.core.deps import CurrentUser, DbDep
from app.models.notification import Notification

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/notifications", tags=["notifications"])


def _serialize(n: Notification) -> dict:
    return {
        "id": n.id,
        "title": n.title,
        "message": n.message,
        "type": n.type.value if hasattr(n.type, "value") else n.type,
        "related_id": n.related_id,
        "is_read": n.is_read,
        "created_at": n.created_at.isoformat() if n.created_at else None,
    }


@router.get("")
def list_notifications(db: DbDep, user: CurrentUser,
                       unread_only: bool = Query(False),
                       limit: int = Query(50, ge=1, le=200)):
    """Newest-first notifications for the calling user, plus an unread count."""
    q = db.query(Notification).filter(Notification.user_id == user.id)
    if unread_only:
        q = q.filter(Notification.is_read.is_(False))
    rows = q.order_by(Notification.created_at.desc()).limit(limit).all()
    unread = (db.query(Notification)
                .filter(Notification.user_id == user.id,
                        Notification.is_read.is_(False)).count())
    return {"items": [_serialize(n) for n in rows], "unread": int(unread)}


@router.post("/{notification_id}/read")
def mark_read(notification_id: str, db: DbDep, user: CurrentUser):
    n = db.get(Notification, notification_id)
    if n is None or n.user_id != user.id:
        raise HTTPException(status_code=404, detail="notification not found")
    n.is_read = True
    db.commit()
    return {"id": n.id, "is_read": True}


@router.post("/read-all")
def mark_all_read(db: DbDep, user: CurrentUser):
    n = (db.query(Notification)
           .filter(Notification.user_id == user.id, Notification.is_read.is_(False))
           .update({"is_read": True}, synchronize_session=False))
    db.commit()
    return {"marked_read": int(n)}
