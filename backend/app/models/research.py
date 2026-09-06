# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

import enum
from sqlalchemy import String, Text, Integer, Enum, ForeignKey
from sqlalchemy.orm import mapped_column, Mapped, relationship
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid


class ArtifactStatus(str, enum.Enum):
    DRAFT = "draft"
    APPROVED = "approved"


class ResearchOutput(Base, TimestampMixin):
    __tablename__ = "research_outputs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("change_requests.id"), nullable=False
    )
    market_research: Mapped[str | None] = mapped_column(Text, nullable=True)
    product_knowledge: Mapped[str | None] = mapped_column(Text, nullable=True)
    rbi_compliance: Mapped[str | None] = mapped_column(Text, nullable=True)
    combined_report: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[ArtifactStatus] = mapped_column(
        Enum(ArtifactStatus, values_callable=lambda x: [e.value for e in x]), default=ArtifactStatus.DRAFT, nullable=False
    )

    # Relationships
    change_request: Mapped["ChangeRequest"] = relationship(
        "ChangeRequest", back_populates="research_outputs"
    )
