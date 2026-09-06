# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

from datetime import datetime
from sqlalchemy import String, Integer, Enum, ForeignKey, DateTime, JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import mapped_column, Mapped, relationship
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid
from app.models.research import ArtifactStatus


class DeclineSpec(Base, TimestampMixin):
    """Per-feature Decline & Timeout design artifact (one approved row per
    essential failure case). Authored during BRD/TSD by the ``decline_designer``
    agent and consumed deterministically by the cert engine.

    ``spec_json`` holds a serialized
    ``excel_testcase_engine.schemas.decline_spec.FeatureDeclineSpec``. We keep it
    as a single JSON blob (not normalized rows) because it is authored, reviewed,
    and consumed as one unit — the same shape the engine validates against.
    """

    __tablename__ = "decline_specs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("change_requests.id"), nullable=False
    )
    # FeatureDeclineSpec, JSONB on Postgres / JSON on the SQLite test backend.
    spec_json: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=dict
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Only APPROVED specs drive certification (same gate discipline as BRD/TSD).
    status: Mapped[ArtifactStatus] = mapped_column(
        Enum(ArtifactStatus, values_callable=lambda x: [e.value for e in x]),
        default=ArtifactStatus.DRAFT, nullable=False,
    )
    approved_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    change_request: Mapped["ChangeRequest"] = relationship(
        "ChangeRequest", back_populates="decline_specs"
    )
