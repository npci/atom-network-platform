# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

import enum
from datetime import datetime
from sqlalchemy import String, Text, Enum, ForeignKey, DateTime
from sqlalchemy.orm import mapped_column, Mapped, relationship
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid


class ApprovalArtifactType(str, enum.Enum):
    BRD = "brd"
    TECH_SPEC = "tech_spec"
    XSD = "xsd"
    PRODUCT_CANVAS = "product_canvas"
    DECLINE_SPEC = "decline_spec"


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Approval(Base, TimestampMixin):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    artifact_type: Mapped[ApprovalArtifactType] = mapped_column(
        Enum(ApprovalArtifactType, values_callable=lambda x: [e.value for e in x]), nullable=False
    )
    artifact_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # Nullable: NULL when this is a role-based placeholder (no specific user assigned yet)
    approver_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    # Role name for placeholder approvals (e.g. "product_manager")
    reviewer_role: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus, values_callable=lambda x: [e.value for e in x]), default=ApprovalStatus.PENDING, nullable=False
    )
    comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    approver: Mapped["User"] = relationship("User", back_populates="approvals", foreign_keys=[approver_id])
