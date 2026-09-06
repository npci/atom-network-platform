# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Kit publications — immutable snapshot of a Product Kit as shipped to partners.

One row per (change_request_id, negotiation_version). Captured at the moment
`communicate_change` dispatches the kit, so the Authority retains the exact envelope the
partner negotiated against — even after the working ProductKitDocument rows are
regenerated to a newer version.

`negotiation_version` is the partner-facing published version (mirrors
ChangeRequest.negotiation_version). `source_doc_versions` records which working
doc version went into this publication, e.g. {"product_doc": 2, "faq": 1,
"xsd": 3} — the binding between the published version and the working-doc history.
"""
from datetime import datetime

from sqlalchemy import (
    JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import generate_uuid, utcnow

# Postgres JSONB in prod; plain JSON on the SQLite test harness (see 0040/0044).
_JSON = JSONB().with_variant(JSON(), "sqlite")


class KitPublication(Base):
    __tablename__ = "kit_publications"
    __table_args__ = (
        UniqueConstraint(
            "change_request_id", "negotiation_version",
            name="uq_kit_publications_change_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("change_requests.id"), nullable=False, index=True,
    )
    negotiation_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # The exact `product_kit` envelope dispatched to partners.
    envelope: Mapped[dict] = mapped_column(_JSON, nullable=False)
    # SHA-256 over json.dumps(envelope, sort_keys=True) — non-repudiation +
    # cheap "did this version change?" check between publications.
    envelope_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # {doc_type: working version} manifest of what went into this publication.
    source_doc_versions: Mapped[dict] = mapped_column(_JSON, nullable=False)
    # Why this revision was published (e.g. "placeholder revision"); null for v1.
    revision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Resolver action that triggered the revision (refine_kit / revise_workflow);
    # null today, populated once the regeneration pipeline drives revisions.
    resolver_action: Mapped[str | None] = mapped_column(String(50), nullable=True)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False,
    )
    published_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True,
    )
