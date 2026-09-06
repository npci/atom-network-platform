# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

import enum
from datetime import datetime
from sqlalchemy import String, Text, Integer, BigInteger, Enum, ForeignKey, DateTime, JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import mapped_column, Mapped, relationship
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid
from app.models.research import ArtifactStatus
from app.models.document_source import DocumentSource


class ProductKitDocType(str, enum.Enum):
    PRODUCT_DOC = "product_doc"
    PRODUCT_DECK = "product_deck"
    PROMO_VIDEO = "promo_video"
    EXPLAINER_VIDEO = "explainer_video"
    FAQ = "faq"
    CERT_TEST_CASES = "cert_test_cases"
    CIRCULAR = "circular"
    MANIFEST = "manifest"
    PROTOTYPE_SCREENS = "prototype_screens"
    # Docgen merge (Session 20+) — new Product Kit doc type routed through
    # the LangGraph docgen pipeline. Postgres enum value added in
    # alembic migration 0024_product_note_doc_type.
    PRODUCT_NOTE = "product_note"


# Doc types that must never be generated or shipped again. The enum MEMBER stays so
# historical rows still deserialize and remain individually fetchable; these are excluded
# from generation and from the "current kit" that goes to partners.
#
# product_doc — superseded by PRODUCT_NOTE, which is the docgen-pipeline document the UI
# already labels "Product Document". PRODUCT_DOC has no section blueprint, is not a docgen
# DocType, and the Product Kit page already filtered it out as retired — but it stayed in
# this enum, and `VALID_DOC_TYPES` is derived from the enum, so a "generate all" run still
# produced it and dispatch still shipped it. That put TWO overlapping narrative documents
# in a bank's kit with no stated precedence — the worst outcome for a regulated rollout,
# since nothing says which one is normative when they disagree.
RETIRED_DOC_TYPES: frozenset[str] = frozenset({"product_doc"})


def active_doc_types() -> set[str]:
    """Doc-type values that may be generated / shipped today (enum minus retired)."""
    return {dt.value for dt in ProductKitDocType} - set(RETIRED_DOC_TYPES)


class ProductKitDocument(Base, TimestampMixin):
    __tablename__ = "product_kit_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("change_requests.id"), nullable=False
    )
    doc_type: Mapped[ProductKitDocType] = mapped_column(Enum(ProductKitDocType, values_callable=lambda x: [e.value for e in x]), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    docx_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    pptx_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Which published kit version (ChangeRequest.negotiation_version) this doc
    # version was generated/cloned for. Lets "docs for publication N" be a
    # cheap filter; the KitPublication snapshot stays the authoritative manifest.
    negotiation_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
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

    # ── Video generation (promo_video / explainer_video) ──────────────────
    # Serialized agents.schemas.video_script.VideoScript — the 8s-segmented
    # script the cert/video pipeline consumes. file_path holds the final
    # merged MP4 (served by the existing video download endpoint).
    script_json: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
    video_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    video_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    video_duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── Ship-time override (migration 0115) ───────────────────────────────
    # A hand-authored file the PM chose to substitute for the generated
    # artifact on the next kit ship. `override_path` present is the sole
    # signal to build_kit_envelope that a substitution exists.
    override_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    override_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    override_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    override_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    override_mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    override_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    override_uploaded_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)

    # Relationships
    change_request: Mapped["ChangeRequest"] = relationship(
        "ChangeRequest", back_populates="product_kit_documents"
    )
