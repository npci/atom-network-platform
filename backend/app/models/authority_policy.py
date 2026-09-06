# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Authority Policy doc — single-row table.

Holds the authoritative AUTHORITY_POLICY.md content loaded as context by the
feasibility resolver (authority side). Single-row pattern enforced by a CHECK
constraint on the primary key — there is at most one active policy doc
at any time.

Seeded on first boot from `settings.authority_policy_path` if the row is
absent. Thereafter, admins maintain content through the Admin UI; the
file remains as a reset-to-seed source.
"""
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import utcnow


class AuthorityPolicy(Base):
    # Table name predates the rename and is retained for schema stability —
    # renaming a live table is a data migration, not a refactor.
    __tablename__ = "npci_policy"
    # Singleton enforced at the DB level — the table can only hold one row.
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_npci_policy_singleton"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
