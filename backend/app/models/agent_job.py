# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""ORM model for the `agent_jobs` table — durable long-running job tracking.

Schema lives in alembic 0025_agent_jobs.py. This module is the read/write
surface used by `app.services.job_registry` and the REST endpoints in
`app.api.jobs`. WS handlers go through job_registry, never touch this
model directly.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Column, DateTime, Enum, ForeignKey, Integer, String, Text, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import JSON as _SA_JSON

# JSONB on Postgres, plain JSON on the SQLite test harness (house pattern —
# see kit_publication.py; raw JSONB breaks create_all under SQLite).
_JSON = JSONB().with_variant(_SA_JSON(), "sqlite")
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AgentJobStatus(str, enum.Enum):
    """Lifecycle states. Transitions:
        pending → running → succeeded | failed | cancelled
    """
    PENDING   = "pending"
    RUNNING   = "running"
    SUCCEEDED = "succeeded"
    FAILED    = "failed"
    CANCELLED = "cancelled"


# Set of statuses that count as "still active" for filtering.
ACTIVE_STATUSES: tuple[AgentJobStatus, ...] = (
    AgentJobStatus.PENDING,
    AgentJobStatus.RUNNING,
)

# Statuses that count as "terminal" — won't change again, safe to expire chunks.
TERMINAL_STATUSES: tuple[AgentJobStatus, ...] = (
    AgentJobStatus.SUCCEEDED,
    AgentJobStatus.FAILED,
    AgentJobStatus.CANCELLED,
)


class AgentJob(Base):
    __tablename__ = "agent_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    change_request_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("change_requests.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    module:  Mapped[str]         = mapped_column(String(64),  nullable=False, index=True)
    subtype: Mapped[str | None]  = mapped_column(String(128), nullable=True)

    status: Mapped[AgentJobStatus] = mapped_column(
        Enum(
            AgentJobStatus,
            name="agent_job_status",
            values_callable=lambda x: [e.value for e in x],
            native_enum=True,
        ),
        nullable=False,
        default=AgentJobStatus.PENDING,
        index=True,
    )

    progress_pct:  Mapped[int | None]  = mapped_column(Integer,      nullable=True)
    current_stage: Mapped[str | None]  = mapped_column(String(255),  nullable=True)

    started_at:   Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at:   Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    started_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    result_payload: Mapped[dict[str, Any] | None] = mapped_column(
        _JSON, nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Underscore suffix mirrors the schema column name `metadata_` —
    # SQLAlchemy reserves the bare `metadata` attribute on declarative bases.
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        _JSON, nullable=False, default=dict, server_default="{}",
    )

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the REST response. Excludes the result_payload by
        default — those can be large (a full BRD markdown). Callers that
        need it pull it explicitly via /api/jobs/{id}.
        """
        return {
            "id":               self.id,
            "change_request_id": self.change_request_id,
            "module":           self.module,
            "subtype":          self.subtype,
            "status":           self.status.value if hasattr(self.status, "value") else str(self.status),
            "progress_pct":     self.progress_pct,
            "current_stage":    self.current_stage,
            "started_at":       self.started_at.isoformat() if self.started_at else None,
            "updated_at":       self.updated_at.isoformat() if self.updated_at else None,
            "completed_at":     self.completed_at.isoformat() if self.completed_at else None,
            "started_by_user_id": self.started_by_user_id,
            "error_message":    self.error_message,
            "metadata":         self.metadata_ or {},
        }
