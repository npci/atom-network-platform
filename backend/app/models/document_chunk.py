# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

from datetime import datetime

from sqlalchemy import String, Text, JSON, Integer, DateTime, Float, Boolean
from sqlalchemy.orm import mapped_column, Mapped
from pgvector.sqlalchemy import Vector
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid


# Default category constants (kept for backward compatibility)
class DocCategory:
    RBI_GUIDELINE = "rbi_guideline"
    NETWORK_PRODUCT_DOC = "upi_product_doc"
    PAST_BRD = "past_brd"
    API_SPEC = "api_spec"
    XSD = "xsd"
    JAVA_SOURCE = "java_source"
    # Slice 22d — polyglot code source categories. Strings match the values
    # written by `ingest_polyglot_repo` so retrieval filters can fan out.
    PYTHON_SOURCE = "python_source"
    TYPESCRIPT_SOURCE = "typescript_source"
    JAVASCRIPT_SOURCE = "javascript_source"

    # ── Excel Testcase Engine future-ready categories ───────────────────────
    # WHY these exist as constants today (without backfilled docs):
    # The engine's RAG adapter (app.excel_testcase_engine.adapters.rag)
    # references namespace names like "circulars", "error_codes",
    # "past_tsds", etc. when retrieving context for the Writer / Validator
    # / Enhancer agents. Today the host's knowledge base is ingested as
    # RBI_GUIDELINE / NETWORK_PRODUCT_DOC / PAST_BRD / API_SPEC — the engine's
    # adapter maps `past_tsds -> API_SPEC` as a stand-in.
    #
    # When the host team starts ingesting the Authority Circulars and the Authority error
    # code reference tables AS DEDICATED CATEGORIES (rather than mixed in
    # with API_SPEC), simply switch the `_ENGINE_TO_HOST` mapping in
    # `excel_testcase_engine/adapters/rag.py:_build_mapping()` from `None`
    # to the matching constant below. No engine code change needed.
    #
    # Strings stay distinct from existing values, so an Alembic migration
    # is unnecessary unless the host team wants strict enum constraints
    # — `doc_category` is a free-form String column, not a SQL ENUM.
    AUTHORITY_CIRCULAR     = "npci_circular"      # The Authority operational circulars (OCs)
    AUTHORITY_ERROR_CODE   = "npci_error_code"    # error/response code reference tables
    AUTHORITY_TSD          = "npci_tsd"           # technical specification documents
    AUTHORITY_PRODUCT_NOTE = "npci_product_note"  # product guideline PDFs
    AUTHORITY_FAQ          = "npci_faq"           # FAQ docs
    AUTHORITY_PRODUCT_DECK = "npci_product_deck"  # product slide decks
    AUTHORITY_CERT_PACK    = "npci_cert_pack"     # cert testcase reference packs (.xlsx)
    AUTHORITY_XML_SPEC     = "npci_xml_spec"      # network wire-format reference XML samples 

    # Distilled the Authority design guidance (API/flow, test-case, error-code design) used
    # by Phase A to design new APIs, flows and certification test cases. Ingested
    # from knowledge_base/api_design_knowledge/.
    API_DESIGN_KNOWLEDGE = "api_design_knowledge"


# Slice 22d — All code-source categories. Code-change retrieval (and any
# future code-aware agent) should filter by this tuple, not JAVA_SOURCE
# alone. Order is irrelevant for the SQL `IN` clause.
CODE_SOURCE_CATEGORIES: tuple[str, ...] = (
    DocCategory.JAVA_SOURCE,
    DocCategory.PYTHON_SOURCE,
    DocCategory.TYPESCRIPT_SOURCE,
    DocCategory.JAVASCRIPT_SOURCE,
)


class DocumentChunk(Base, TimestampMixin):
    __tablename__ = "document_chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    source_file: Mapped[str] = mapped_column(String(1000), nullable=False)
    doc_category: Mapped[str] = mapped_column(String(100), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(768), nullable=True)
    chunk_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)

    # Slice 3 — tree-sitter-derived symbol metadata (nullable; populated only for
    # code chunks emitted by the AST-aware chunker. Regex chunker leaves NULL).
    symbol_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    symbol_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    signature:   Mapped[str | None] = mapped_column(Text, nullable=True)
    line_start:  Mapped[int | None] = mapped_column(Integer, nullable=True)
    line_end:    Mapped[int | None] = mapped_column(Integer, nullable=True)
    language:    Mapped[str | None] = mapped_column(String(30), nullable=True)

    # Slice 4 — 3-view embedding grouping. When USE_CODE_MULTIVIEW_EMBEDDING is
    # on, each code symbol becomes 1-3 rows sharing a parent_symbol_id:
    #   - view_kind='body'       content=full source of the symbol
    #   - view_kind='signature'  content=declaration line + symbol_kind label
    #   - view_kind='nl_summary' content=LLM-generated natural-language summary
    # Retrieval dedups by parent_symbol_id so top-k doesn't repeat the same
    # symbol via multiple views.
    view_kind:         Mapped[str | None] = mapped_column(String(20), nullable=True)
    parent_symbol_id:  Mapped[str | None] = mapped_column(String(36), nullable=True)

    # Slice 7 — Doc metadata + hierarchical-chunking linkage (all nullable).
    # Populated for markdown rows emitted by the hierarchical chunker
    # (USE_HIERARCHICAL_CHUNKER=True) and any future source that carries
    # explicit freshness / deprecation flags. Retrieval applies
    # `deprecated IS NOT TRUE` as a default filter.
    title_breadcrumb: Mapped[str | None]      = mapped_column(String(1000), nullable=True)
    last_modified:    Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    author:           Mapped[str | None]      = mapped_column(String(200), nullable=True)
    product_area:     Mapped[str | None]      = mapped_column(String(100), nullable=True)
    freshness_score:  Mapped[float | None]    = mapped_column(Float, nullable=True)
    deprecated:       Mapped[bool | None]     = mapped_column(Boolean, nullable=True)
    parent_chunk_id:  Mapped[str | None]      = mapped_column(String(36), nullable=True)

    # Slice 17 — Symbol-graph edges extracted via tree-sitter for Java (and
    # later Python/JS/TS via sub-slices 23/24). Cross-file edges require a
    # global symbol index — future slice. All nullable; populated only by
    # the symbol-graph extractor when USE_SYMBOL_GRAPH_EXTRACTOR is on.
    imports:    Mapped[list | None] = mapped_column(JSON,         nullable=True)
    inherits:   Mapped[str | None]  = mapped_column(String(500),  nullable=True)
    implements: Mapped[list | None] = mapped_column(JSON,         nullable=True)
    calls:      Mapped[list | None] = mapped_column(JSON,         nullable=True)
    called_by:  Mapped[list | None] = mapped_column(JSON,         nullable=True)

    # Slice 23 — Cross-file call resolution via Python LSP. Each entry is
    # `{"callee_symbol": str, "callee_path": str, "line": int|None,
    #   "language": str}`. Populated only when `use_python_lsp` is True
    # and the LSP successfully resolved the call. Cross-file inheritance
    # (and TS via Slice 24) reuse this same column.
    cross_file_calls: Mapped[list | None] = mapped_column(JSON,    nullable=True)
