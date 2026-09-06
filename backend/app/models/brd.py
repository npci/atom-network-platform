# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

import enum
from datetime import datetime
from sqlalchemy import String, Text, Integer, Enum, ForeignKey, DateTime
from sqlalchemy.orm import mapped_column, Mapped, relationship
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid
from app.models.document_source import DocumentSource


class BRDStatus(str, enum.Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    REVISION = "revision"
    APPROVED = "approved"


class BRD(Base, TimestampMixin):
    __tablename__ = "brds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("change_requests.id"), nullable=False
    )
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    docx_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[BRDStatus] = mapped_column(
        Enum(BRDStatus, values_callable=lambda x: [e.value for e in x]), default=BRDStatus.DRAFT, nullable=False
    )
    # Provenance — set when the user uploads a document in place of generating it.
    source: Mapped[DocumentSource] = mapped_column(
        Enum(DocumentSource, values_callable=lambda x: [e.value for e in x]).with_variant(String(20), "sqlite"),
        default=DocumentSource.GENERATED, server_default="generated", nullable=False,
    )
    original_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    uploaded_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    change_request: Mapped["ChangeRequest"] = relationship(
        "ChangeRequest", back_populates="brds"
    )
    approvals: Mapped[list["Approval"]] = relationship(
        "Approval",
        primaryjoin="and_(Approval.artifact_type=='brd', foreign(Approval.artifact_id)==BRD.id)",
        viewonly=True,
    )
