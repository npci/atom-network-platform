# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Document ingestion pipeline.

Walks knowledge_base/ → chunk → embed → upsert.

WHY this module now mixes two chunking paths:
- doc-generation-wiring added structure-preserving ingestion, change-aware
  re-ingestion, and wider KB folder coverage. Those were the "lost" features.
- main already had a markdown-specific hierarchical chunker used by the
  code/doc retrieval stack.

We keep the hierarchical markdown path for `.md` when the feature flag is on,
and use the docgen branch's structured chunker for the broader document set.
That restores the missing the Authority/KB ingestion behavior without regressing the
post-merge retrieval enhancements that depend on markdown breadcrumbs.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.document_chunk import DocCategory, DocumentChunk
from app.models.base import generate_uuid
from app.rag.chunking import chunk_file, SUPPORTED_EXTENSIONS
from app.rag.embeddings import embed_texts

logger = logging.getLogger(__name__)

# Map knowledge_base subfolder → DocCategory value
FOLDER_TO_CATEGORY: dict[str, DocCategory] = {
    # ── Original host categories (unchanged) ────────────────────────────────
    "rbi_guidelines":    DocCategory.RBI_GUIDELINE,
    "upi_product_docs":  DocCategory.NETWORK_PRODUCT_DOC,
    "past_brds":         DocCategory.PAST_BRD,
    "api_specifications": DocCategory.API_SPEC,
    "existing_xsds":     DocCategory.XSD,

    # ── Excel Testcase Engine folder mappings ──────────────────────────────
    # WHY these entries: the user's IngestDocuments tree has six folders the
    # original FOLDER_TO_CATEGORY didn't know about. Without a mapping here
    # those folders are silently skipped by ingest_all(). We map each to the
    # The Authority-specific DocCategory constants added in
    # app.models.document_chunk.DocCategory. The engine's RAG adapter
    # (app.excel_testcase_engine.adapters.rag._build_mapping) currently
    # maps these as `None` (future-ready); flip them to the constants
    # below to let the engine narrow retrieval per namespace.
    "past_tsds":         DocCategory.AUTHORITY_TSD,
    "circulars":         DocCategory.AUTHORITY_CIRCULAR,
    "error_codes":       DocCategory.AUTHORITY_ERROR_CODE,
    "product_notes":     DocCategory.AUTHORITY_PRODUCT_NOTE,
    "product_deck":      DocCategory.AUTHORITY_PRODUCT_DECK,
    "faqs":              DocCategory.AUTHORITY_FAQ,
    "cert_testcases":    DocCategory.AUTHORITY_CERT_PACK,
    "upi_xml_specs":     DocCategory.AUTHORITY_XML_SPEC,

    # ── API-design knowledge base (Phase A: API / flow / test-case design) ──
    # Distilled the Authority design guidance. Consulted broadly by Deep Researcher,
    # and scoped explicitly by the Tech-Spec agent + Excel testcase engine.
    "api_design_knowledge": DocCategory.API_DESIGN_KNOWLEDGE,
}

CHUNK_SIZE = 800        # kept for backward compatibility with callers importing it
CHUNK_OVERLAP = 100     # kept for backward compatibility with callers importing it
BATCH_SIZE = 32


def parse_file(path: Path) -> str | None:
    """Backward-compat helper used by older upload code paths.

    WHY keep this wrapper instead of deleting it:
    `api/rag.py` and any local admin/debug tooling may still import parse_file.
    Returning concatenated structured chunks lets those callers keep working
    while the ingestion pipeline moves to `chunk_file()` metadata-rich rows.
    """
    chunks = chunk_file(path)
    if not chunks:
        return None
    return "\n\n".join(c["content"] for c in chunks)


def _prepare_chunk_rows(
    file_path: Path,
    rel_path: str,
) -> tuple[list[dict], bool]:
    """Prepare chunk rows for one file, optionally via hierarchical markdown.

    WHY this helper exists:
    folder-level parallelism should speed up expensive file parsing/chunking,
    but SQLAlchemy sessions are not thread-safe. We therefore do the pure
    file-system / parsing work in worker threads and keep the DB mutation
    phase sequential in `ingest_all()`.
    """
    chunk_metas: list[dict] | None = None
    use_hier = (
        settings.use_hierarchical_chunker
        and file_path.suffix.lower() == ".md"
    )
    if use_hier:
        from app.rag import doc_chunker_hierarchical

        raw_text = file_path.read_text(encoding="utf-8", errors="ignore")
        last_modified = datetime.fromtimestamp(
            file_path.stat().st_mtime, tz=timezone.utc,
        )
        chunk_metas = doc_chunker_hierarchical.chunk_markdown(
            rel_path, raw_text, last_modified=last_modified,
        )
        chunk_rows = [
            {
                "content": c["content"],
                "section_title": c.get("title_breadcrumb"),
                "page": None,
                "chunk_type": "text",
                "legacy_meta": c,
            }
            for c in chunk_metas
        ]
    else:
        chunk_rows = chunk_file(file_path)

    return chunk_rows, use_hier


# ── Core ingestion ────────────────────────────────────────────────────────────

def ingest_all(db: Session, force: bool = False) -> dict:
    """Ingest all supported KB documents.

    WHY this is change-aware:
    the original merge dropped doc-generation-wiring's "auto re-ingest when the
    source file changes" behavior. We compare file mtime against the stored
    ingestion timestamp so new/changed KB content is picked up automatically,
    and we remove orphaned DB rows when files disappear from disk.
    """
    kb_dir = Path(settings.knowledge_base_dir)
    summary = {
        "processed": 0,
        "skipped": 0,
        "updated": 0,
        "errors": 0,
        "chunks_created": 0,
        "orphans_removed": 0,
    }

    if not kb_dir.exists():
        logger.warning("Knowledge base directory missing: %s", kb_dir)
        return summary

    disk_paths: set[str] = set()
    candidates: list[tuple[str, str, Path, bool]] = []
    existing_rows = {
        row.source_file: row
        for row in db.query(DocumentChunk).filter(
            DocumentChunk.doc_category.in_(list(FOLDER_TO_CATEGORY.values()))
        ).all()
    }

    for folder, category in FOLDER_TO_CATEGORY.items():
        folder_path = kb_dir / folder
        if not folder_path.exists():
            continue

        for file_path in sorted(folder_path.rglob("*")):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue

            rel_path = str(file_path.relative_to(kb_dir)).replace("\\", "/")
            disk_paths.add(rel_path)

            existing = existing_rows.get(rel_path)
            is_update = False
            if not force and existing:
                stored_ts = (existing.metadata_ or {}).get("ingested_at")
                try:
                    stored_epoch = datetime.fromisoformat(stored_ts).timestamp() if stored_ts else 0
                except (TypeError, ValueError):
                    stored_epoch = 0
                if stored_epoch and stored_epoch >= file_path.stat().st_mtime:
                    summary["skipped"] += 1
                    continue
                is_update = True

            candidates.append((folder, category, file_path, is_update))

    # WHY thread-pool at this stage:
    # the slow part before embedding is walking PDFs/DOCX/XLSX/XML and slicing
    # them into chunks. Doing that per file in parallel improves startup and
    # manual re-ingest latency, while the DB mutation and commit path stays
    # single-threaded to avoid session-safety issues.
    prepared_results: dict[str, tuple[str, str, Path, bool, list[dict], bool] | None] = {}
    max_workers = max(1, settings.kb_ingest_parallelism)
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="kb-ingest") as executor:
        futures = {
            executor.submit(_prepare_chunk_rows, file_path, str(file_path.relative_to(kb_dir)).replace("\\", "/")):
            (folder, category, file_path, is_update)
            for folder, category, file_path, is_update in candidates
        }
        for future in as_completed(futures):
            folder, category, file_path, is_update = futures[future]
            rel_path = str(file_path.relative_to(kb_dir)).replace("\\", "/")
            try:
                chunk_rows, use_hier = future.result()
            except Exception as e:
                logger.error("Chunk preparation failed for %s: %s", rel_path, e)
                summary["errors"] += 1
                prepared_results[rel_path] = None
                continue
            prepared_results[rel_path] = (folder, category, file_path, is_update, chunk_rows, use_hier)

    for folder, category, file_path, is_update in candidates:
        rel_path = str(file_path.relative_to(kb_dir)).replace("\\", "/")
        prepared = prepared_results.get(rel_path)
        if not prepared:
            continue
        _, _, _, _, chunk_rows, use_hier = prepared

        db.query(DocumentChunk).filter(
            DocumentChunk.source_file == rel_path
        ).delete(synchronize_session=False)
        db.flush()

        if not chunk_rows:
            summary["errors"] += 1
            continue

        chunk_objects = []
        ingested_at = datetime.now(timezone.utc).isoformat()
        for batch_start in range(0, len(chunk_rows), BATCH_SIZE):
            batch = chunk_rows[batch_start: batch_start + BATCH_SIZE]
            try:
                embeddings = embed_texts([row["content"] for row in batch])
            except Exception as e:
                logger.error("Embedding failed for %s: %s", rel_path, e)
                summary["errors"] += 1
                chunk_objects = []
                break

            for i, (row, emb) in enumerate(zip(batch, embeddings)):
                legacy_meta = row.get("legacy_meta")
                chunk_objects.append(
                    DocumentChunk(
                        id=(legacy_meta["id"] if legacy_meta else generate_uuid()),
                        source_file=rel_path,
                        doc_category=category,
                        content=row["content"],
                        embedding=emb,
                        chunk_index=batch_start + i,
                        metadata_={
                            "file_name": file_path.name,
                            "folder": folder,
                            "section_title": row.get("section_title"),
                            "page": row.get("page"),
                            "chunk_type": row.get("chunk_type", "text"),
                            "ingested_at": ingested_at,
                        },
                        # Keep main's markdown breadcrumb enrichment intact.
                        title_breadcrumb=(legacy_meta.get("title_breadcrumb") if legacy_meta else None),
                        parent_chunk_id=(legacy_meta.get("parent_chunk_id") if legacy_meta else None),
                        last_modified=(legacy_meta.get("last_modified") if legacy_meta else None),
                    )
                )

        if not chunk_objects:
            continue

        db.add_all(chunk_objects)
        db.commit()

        if is_update:
            summary["updated"] += 1
        else:
            summary["processed"] += 1
        summary["chunks_created"] += len(chunk_objects)
        logger.info(
            "%s %s → %d chunks (hierarchical=%s)",
            "Re-ingested" if is_update else "Ingested",
            rel_path,
            len(chunk_objects),
            use_hier,
        )

    # WHY orphan sweep:
    # the old merge left stale rows behind when files were renamed or removed
    # from knowledge_base/. Cleaning them up keeps retrieval aligned to the
    # actual KB contents instead of surfacing deleted references.
    known_categories = set(FOLDER_TO_CATEGORY.values())
    db_paths = {
        row[0]
        for row in (
            db.query(DocumentChunk.source_file)
            .filter(DocumentChunk.doc_category.in_(known_categories))
            .distinct()
            .all()
        )
    }
    orphans = db_paths - disk_paths
    if orphans:
        db.query(DocumentChunk).filter(
            DocumentChunk.source_file.in_(orphans),
            DocumentChunk.doc_category.in_(known_categories),
        ).delete(synchronize_session=False)
        db.commit()
        summary["orphans_removed"] = len(orphans)

    return summary


def get_ingestion_status(db: Session) -> dict:
    """Return counts of ingested chunks per category and per file."""
    from sqlalchemy import func

    rows = (
        db.query(DocumentChunk.doc_category, func.count(DocumentChunk.id))
        .group_by(DocumentChunk.doc_category)
        .all()
    )
    by_category = {str(cat): count for cat, count in rows}

    files = (
        db.query(DocumentChunk.source_file, DocumentChunk.doc_category,
                 func.count(DocumentChunk.id))
        .group_by(DocumentChunk.source_file, DocumentChunk.doc_category)
        .order_by(DocumentChunk.source_file)
        .all()
    )
    file_list = [
        {"file": f, "category": str(cat), "chunks": cnt}
        for f, cat, cnt in files
    ]

    total = sum(by_category.values())
    return {
        "total_chunks": total,
        "by_category": by_category,
        "files": file_list,
    }
