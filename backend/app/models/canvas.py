# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

from datetime import datetime
from sqlalchemy import String, Text, Integer, Enum, ForeignKey, DateTime
from sqlalchemy.orm import mapped_column, Mapped, relationship
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid
from app.models.research import ArtifactStatus
from app.models.document_source import DocumentSource


class ProductCanvas(Base, TimestampMixin):
    __tablename__ = "product_canvases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("change_requests.id"), nullable=False
    )
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    docx_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[ArtifactStatus] = mapped_column(
        Enum(ArtifactStatus, values_callable=lambda x: [e.value for e in x]), default=ArtifactStatus.DRAFT, nullable=False
    )
    approved_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
        "ChangeRequest", back_populates="product_canvases"
    )
