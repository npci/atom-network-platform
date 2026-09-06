# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""XSD schema graph + JAXB binding model (THE BOOK §7).

Replaces the weak ``parent_xsd_id`` lineage idea with a real schema graph
(include/import edges) plus an element→Java link record carrying evidence,
confidence, and provenance. Judgement is agentic (the XSD-Discovery subagent);
the *record* is deterministic (§7.4).
"""
import enum
from datetime import datetime

from sqlalchemy import String, Text, Float, ForeignKey, JSON, DateTime
from sqlalchemy.orm import mapped_column, Mapped, relationship

from app.core.database import Base
from app.models.base import generate_uuid, utcnow


class XsdEdgeType(str, enum.Enum):
    INCLUDE = "include"   # xs:include — same target namespace
    IMPORT  = "import"    # xs:import  — different namespace


class XsdLinkSource(str, enum.Enum):
    """Provenance of an element→Java link, ordered by confidence tier (§7.3)."""
    JAXB_ROOT      = "jaxb_root"        # @XmlRootElement exact          0.9-0.95
    OBJECT_FACTORY = "object_factory"   # ObjectFactory @XmlElementDecl  0.9-0.95
    XJB            = "xjb"              # external .xjb binding          0.9
    CATALOGUE      = "catalogue"        # endpoint/type catalogue        0.85
    DOC_LINK       = "doc_link"         # LLM-scored doc_code_linker
    IMPACT         = "impact"           # impact analyzer (×0.7)
    RAG            = "rag"              # semantic retrieval (≤0.5)


class XsdSchemaNode(Base):
    """One schema file in the graph (§7.1)."""
    __tablename__ = "xsd_schema_nodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    repo_id: Mapped[str] = mapped_column(String(36), ForeignKey("code_repos.id"), nullable=False, index=True)
    path: Mapped[str] = mapped_column(String(1000), nullable=False)
    target_namespace: Mapped[str | None] = mapped_column(String(500), nullable=True)
    base_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    out_edges: Mapped[list["XsdSchemaEdge"]] = relationship(
        "XsdSchemaEdge", back_populates="from_node",
        foreign_keys="XsdSchemaEdge.from_node_id", cascade="all, delete-orphan",
    )


class XsdSchemaEdge(Base):
    """An include/import edge between two schema nodes (§7.1)."""
    __tablename__ = "xsd_schema_edges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    from_node_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("xsd_schema_nodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    to_node_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("xsd_schema_nodes.id", ondelete="CASCADE"), nullable=True, index=True
    )
    edge_type: Mapped[str] = mapped_column(String(20), nullable=False)
    schema_location: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    namespace: Mapped[str | None] = mapped_column(String(500), nullable=True)

    from_node: Mapped["XsdSchemaNode"] = relationship(
        "XsdSchemaNode", back_populates="out_edges", foreign_keys=[from_node_id]
    )


class XsdJavaLink(Base):
    """Element→Java binding with evidence + confidence + provenance (§7.3).

    Links below ``xsd_link_min_confidence`` (0.55) enter a verification list
    and are never presented as definite.
    """
    __tablename__ = "xsd_java_links"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    repo_id: Mapped[str] = mapped_column(String(36), ForeignKey("code_repos.id"), nullable=False, index=True)
    node_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("xsd_schema_nodes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    xpath: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # Either a DocumentChunk id or a clone-relative path to the bound symbol.
    symbol_chunk_id_or_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    base_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
