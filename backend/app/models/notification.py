# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

import enum
from sqlalchemy import String, Text, Boolean, Enum, ForeignKey
from sqlalchemy.orm import mapped_column, Mapped, relationship
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid


class NotificationType(str, enum.Enum):
    APPROVAL_REQUEST = "approval_request"
    APPROVAL_DONE = "approval_done"
    REVISION_READY = "revision_ready"
    INFO = "info"
    # Operational alerts (app.services.notifications). DELIVERY_FAILED is raised when an
    # outbound A2A message to a bank/partner could not be delivered — previously that was
    # only a logger.error() nobody watched, so a bank silently never got its kit.
    DELIVERY_FAILED = "delivery_failed"
    MANDATORY_REJECTION = "mandatory_rejection"


class Notification(Base, TimestampMixin):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[NotificationType] = mapped_column(
        Enum(NotificationType, values_callable=lambda x: [e.value for e in x]), default=NotificationType.INFO, nullable=False
    )
    related_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="notifications")
