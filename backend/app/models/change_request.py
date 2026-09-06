# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

import enum
from datetime import datetime

from sqlalchemy import String, Text, Integer, Boolean, Enum, ForeignKey, DateTime
from sqlalchemy.orm import mapped_column, Mapped, relationship
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid


class ChangeStatus(str, enum.Enum):
    PROMPT_ENHANCEMENT = "prompt_enhancement"
    RESEARCH = "research"
    CANVAS = "canvas"
    CLARIFICATION = "clarification"
    BRD = "brd"
    TECH_SPEC = "tech_spec"
    XSD = "xsd"
    PRODUCT_KIT = "product_kit"
    COMPLETED = "completed"


class ChangeRequest(Base, TimestampMixin):
    __tablename__ = "change_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    initial_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    enhanced_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optional SOURCE DOCUMENT (detailed BRD/requirements) uploaded at change creation.
    # Rich INPUT to Phase A (enhancer/research/canvas/BRD gen) so the pipeline starts
    # from the PM's facts instead of assuming — it never replaces a generated artifact.
    # Injected via services.source_material.source_block (wrapped untrusted, bounded).
    source_doc_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_doc_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ChangeStatus] = mapped_column(
        Enum(ChangeStatus, values_callable=lambda x: [e.value for e in x]), default=ChangeStatus.PROMPT_ENHANCEMENT, nullable=False
    )
    created_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )

    # Relationships
    creator: Mapped["User"] = relationship(
        "User", back_populates="change_requests", foreign_keys=[created_by]
    )
    conversations: Mapped[list["Conversation"]] = relationship(
        "Conversation", back_populates="change_request", cascade="all, delete-orphan"
    )
    research_outputs: Mapped[list["ResearchOutput"]] = relationship(
        "ResearchOutput", back_populates="change_request", cascade="all, delete-orphan"
    )
    product_canvases: Mapped[list["ProductCanvas"]] = relationship(
        "ProductCanvas", back_populates="change_request", cascade="all, delete-orphan"
    )
    brds: Mapped[list["BRD"]] = relationship(
        "BRD", back_populates="change_request", cascade="all, delete-orphan"
    )
    tech_specs: Mapped[list["TechSpec"]] = relationship(
        "TechSpec", back_populates="change_request", cascade="all, delete-orphan"
    )
    decline_specs: Mapped[list["DeclineSpec"]] = relationship(
        "DeclineSpec", back_populates="change_request", cascade="all, delete-orphan"
    )
    xsds: Mapped[list["XSD"]] = relationship(
        "XSD", back_populates="change_request", cascade="all, delete-orphan"
    )
    product_kit_documents: Mapped[list["ProductKitDocument"]] = relationship(
        "ProductKitDocument", back_populates="change_request", cascade="all, delete-orphan"
    )

    # ── Negotiation governance (migration 0056) ───────────────────────────────
    # Set by PM when round 2 closes and all clusters are decided. Once set,
    # all open counter-proposals are auto-closed and no new ones are accepted.
    negotiation_finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Starts at 1; incremented each time the Authority publishes a revised version.
    # Partners must explicitly accept a new version before proceeding.
    negotiation_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    # ── Agentic codegen opt-in (migration M-B) ────────────────────────────────
    # Per-change gate for the durable agentic Phase-B path (THE BOOK §1.3). The
    # global settings.use_agentic_tool_loop must also be on. Default off keeps
    # the legacy single-shot path the behaviour for every existing change.
    agentic_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Set when the final kit version (v3) is shipped. Once set, the executor
    # rejects inbound QUERY / CERT_QUERY / COUNTER_PROPOSAL — partners can only
    # raise an EmergencyIssue (Slice 5).
    negotiation_frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Accuracy upgrade (migration 0085) ─────────────────────────────────────
    # Stage-order / gate-map / staleness-map version. v2 (BRD→XSD→TSD) is now THE
    # default flow — the TSD is authored against approved real schemas. The column is
    # kept (rather than hardcoding the order) only so any legacy v1 rows still in flight
    # keep the order they actually executed. See docs/PLAN_AGENTIC_ACCURACY.md S5.
    workflow_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
