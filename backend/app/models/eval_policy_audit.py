# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Audit trail for eval policy mode changes."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import generate_uuid, utcnow


class EvalPolicyAudit(Base):
    __tablename__ = "eval_policy_audit"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    checkpoint_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    old_policy_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    new_policy_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    actor_username: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    app_env: Mapped[str] = mapped_column(String(32), nullable=False, default="development")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
        index=True,
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "checkpoint_id": self.checkpoint_id,
            "old_policy_mode": self.old_policy_mode,
            "new_policy_mode": self.new_policy_mode,
            "actor_user_id": self.actor_user_id,
            "actor_username": self.actor_username,
            "reason": self.reason,
            "app_env": self.app_env,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
