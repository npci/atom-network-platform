# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Capability-pack store rows (SIM-2/SIM-5, migration 0134).

`SimPackRecord.content` holds the pack EXACTLY as stamped — canonical data,
never edited in place: packs are immutable, and `pack_id` is the content
address that proves it. `pack_ref` is unique (the human identity); the same
`pack_id` may legitimately recur across refs (identical behaviour republished
under a new revision keeps its address).

`SimPackPublication` records each push of a pack to a simulator instance —
one row per (pack, target). A pack "published" to a simulator that did not
store it is the failure this table makes visible. The built-in Python
simulator runtime reads `sim_packs` directly (greenfield decision
2026-08-31); its publications carry target "local".
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid


class SimPackRecord(Base, TimestampMixin):
    __tablename__ = "sim_packs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    pack_ref: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    pack_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    change_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True,
                                                          index=True)
    # Chain parent ref; NULL only for a root (baseline) pack.
    base_pack_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    engine_min: Mapped[str] = mapped_column(String(20), nullable=False, default="1.0")
    requires: Mapped[list | None] = mapped_column(JSON, nullable=True)
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    coverage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # draft | published | withdrawn — only published packs resolve at run time.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    created_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                          nullable=True)


class SimPackPublication(Base):
    __tablename__ = "sim_pack_publications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    pack_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    pack_id: Mapped[str] = mapped_column(String(80), nullable=False)
    target: Mapped[str] = mapped_column(String(500), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    echoed_pack_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # WHO published — a pack changes what "certified" means (plan S-5).
    published_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                   nullable=False,
                                                   server_default=func.now())
