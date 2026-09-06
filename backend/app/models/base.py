# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

import uuid
from datetime import datetime, timezone
from sqlalchemy import DateTime
from sqlalchemy.orm import mapped_column, Mapped
from app.core.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, onupdate=utcnow, nullable=True
    )


def generate_uuid() -> str:
    return str(uuid.uuid4())
