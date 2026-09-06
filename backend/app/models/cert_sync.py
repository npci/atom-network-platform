# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Cert simulator sync log — audit trail of test-case pushes from Phase A
into cert-agent's `tc_store`.

Two operations are logged:
  - "diff_view" — operator opened the diff modal (POST /cert-simulator/diff)
  - "apply"     — operator confirmed and changes were pushed (POST /cert-simulator/apply)

The "apply" rows feed the Cert Status timeline as `test_suite_registered` events.

Also defines `NetworkXmlTemplate` — cached Mustache XML templates per network API
(see migration 0074). Backs `xml_template_resolver.resolve_or_generate()`
and the operator approval gate for LLM-drafted new-API templates.
"""
from datetime import datetime

from sqlalchemy import String, Text, DateTime, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import generate_uuid, utcnow


class CertSimulatorSyncLog(Base):
    __tablename__ = "cert_simulator_sync_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("change_requests.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    cert_engine_partner_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("partner_agents.id"), nullable=True,
    )
    actor_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True,
    )
    operation: Mapped[str] = mapped_column(String(20), nullable=False)
    summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True,
    )


class NetworkXmlTemplate(Base):
    """Cached Mustache XML template for a network API.

    `source` is one of {'catalog','llm','operator'}. LLM rows ship with
    `approved_at=NULL` and the `/cert-simulator/apply` path refuses to
    register the parent flow until an operator approves via the SyncDiffModal
    (sets `approved_by` + `approved_at`).
    """
    __tablename__ = "upi_xml_templates"

    api_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    flow_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    xml_template: Mapped[str] = mapped_column(Text, nullable=False)
    placeholders_used: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False,
    )
