# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

import enum
from datetime import datetime
from sqlalchemy import String, Text, Integer, BigInteger, Boolean, Enum, ForeignKey, DateTime
from sqlalchemy.orm import mapped_column, Mapped, relationship
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid
from app.models.document_source import DocumentSource


class XSDStatus(str, enum.Enum):
    DRAFT = "draft"
    DOWNLOADED = "downloaded"


class XSD(Base, TimestampMixin):
    __tablename__ = "xsds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("change_requests.id"), nullable=False
    )
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    docx_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[XSDStatus] = mapped_column(
        Enum(XSDStatus, values_callable=lambda x: [e.value for e in x]), default=XSDStatus.DRAFT, nullable=False
    )
    # Provenance — set when the user uploads a document in place of generating it.
    source: Mapped[DocumentSource] = mapped_column(
        Enum(DocumentSource, values_callable=lambda x: [e.value for e in x]).with_variant(String(20), "sqlite"),
        default=DocumentSource.GENERATED, server_default="generated", nullable=False,
    )
    original_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    uploaded_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Ship-time override (migration 0115) — see ProductKitDocument.
    override_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    override_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    override_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    override_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    override_mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    override_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    override_uploaded_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)

    # Relationships
    change_request: Mapped["ChangeRequest"] = relationship(
        "ChangeRequest", back_populates="xsds"
    )
