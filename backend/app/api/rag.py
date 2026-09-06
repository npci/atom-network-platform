# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""RAG management API — admin-only endpoints for the knowledge base pipeline."""
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy import select, func, distinct

from app.core.deps import DbDep, AdminUser
from app.models.document_chunk import DocumentChunk, DocCategory
from app.models.base import generate_uuid
from app.rag.ingestion import get_ingestion_status, BATCH_SIZE
from app.rag.chunking import chunk_file, SUPPORTED_EXTENSIONS
from app.rag.retrieval import retrieve
from app.rag.embeddings import embed_texts
from app.services.celery_tasks import ingest_knowledge_base

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/rag", tags=["rag"])

MAX_RAG_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB


# ── Request / Response schemas ────────────────────────────────────────────────

class IngestRequest(BaseModel):
    force: bool = False

class SearchRequest(BaseModel):
    query: str
    top_k: int = 6
    categories: list[str] | None = None


# ── Default category suggestions ─────────────────────────────────────────────

DEFAULT_CATEGORIES = [
    {"value": "npci_circular",     "label": "NPCI Circulars"},
    {"value": "rbi_guideline",   "label": "RBI Guidelines"},
    {"value": "upi_product_doc", "label": "UPI Product Docs"},
    {"value": "past_brd",        "label": "Past BRDs"},
    {"value": "npci_tsd",        "label": "Past TSDs"},
    {"value": "npci_product_note", "label": "Product Notes"},
    {"value": "npci_cert_pack",  "label": "Certification Testcases"},
    {"value": "npci_error_code", "label": "Error Codes"},
    {"value": "npci_faq",        "label": "FAQs"},
    {"value": "npci_product_deck", "label": "Product Decks"},
    {"value": "api_spec",        "label": "API Specifications"},
    {"value": "xsd",             "label": "Existing XSDs"},
]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/ingest")
def trigger_ingest(payload: IngestRequest, _: AdminUser):
    """Enqueue Celery task to ingest all knowledge_base/ documents."""
    task = ingest_knowledge_base.delay(force=payload.force)
    return {"task_id": task.id, "status": "queued"}


@router.post("/reingest")
def reingest_now(payload: IngestRequest, db: DbDep, _: AdminUser):
    """Run KB ingestion inline and immediately rebuild BM25.

    WHY this endpoint exists in addition to `/ingest`:
    the doc-generation-wiring branch had a synchronous re-ingest path that was
    handy in local/dev flows where Celery workers were not running. Restoring
    it gives admins a deterministic "apply KB changes now" button again.
    """
    from app.rag.ingestion import ingest_all
    from app.rag import bm25_search

    summary = ingest_all(db, force=payload.force)
    try:
        summary["bm25_indexed"] = bm25_search.build_index(db)
    except Exception as e:
        logger.warning("BM25 rebuild after reingest failed: %s", e)
        summary["bm25_indexed"] = None
    return {"status": "complete", **summary}


@router.get("/task/{task_id}")
def get_task_status(task_id: str, _: AdminUser):
    """Poll ingestion task status."""
    from app.services.celery_tasks import celery_app
    result = celery_app.AsyncResult(task_id)
    return {
        "task_id": task_id,
        "state":   result.state,
        "info":    result.info if isinstance(result.info, dict) else str(result.info),
    }


@router.get("/status")
def knowledge_base_status(db: DbDep, _: AdminUser):
    """Return counts of ingested chunks per category and per file."""
    return get_ingestion_status(db)


@router.get("/categories")
def list_categories(db: DbDep, _: AdminUser):
    """List all distinct categories (excluding java_source) with counts."""
    rows = (
        db.execute(
            select(DocumentChunk.doc_category, func.count(DocumentChunk.id))
            .where(DocumentChunk.doc_category != DocCategory.JAVA_SOURCE)
            .group_by(DocumentChunk.doc_category)
        ).all()
    )
    existing = {row[0]: row[1] for row in rows}

    # Merge with defaults
    result = []
    seen = set()
    for d in DEFAULT_CATEGORIES:
        result.append({
            "value": d["value"],
            "label": d["label"],
            "chunks": existing.get(d["value"], 0),
        })
        seen.add(d["value"])

    # Add any user-created categories not in defaults
    for cat, count in existing.items():
        if cat not in seen:
            result.append({
                "value": cat,
                "label": cat.replace("_", " ").title(),
                "chunks": count,
            })

    return result


@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    category: str = Form(...),
    db: DbDep = None,
    _: AdminUser = None,
):
    """Upload a document, structure-chunk it, and store it in pgvector.

    WHY this no longer uses the legacy plain-text splitter:
    bulk KB ingestion already moved back to the richer chunker from
    doc-generation-wiring. Using the same path here keeps ad-hoc uploads
    consistent with filesystem ingestion instead of producing lower-fidelity
    chunks with different metadata.
    """

    filename = file.filename or "unknown"
    suffix = Path(filename).suffix.lower()

    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {suffix}. Allowed: {', '.join(sorted(SUPPORTED_EXTENSIONS))}",
        )

    logger.info("Document upload: file=%s category=%s", filename, category)

    # Size guard. Reject early on the declared size when present, and cap the
    # actual read at the limit + 1 byte so an oversized payload can never be
    # pulled fully into memory before we bail.
    declared = getattr(file, "size", None)
    if declared is not None and declared > MAX_RAG_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large (max {MAX_RAG_UPLOAD_BYTES // (1024 * 1024)} MB).")
    content = await file.read(MAX_RAG_UPLOAD_BYTES + 1)
    if len(content) > MAX_RAG_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large (max {MAX_RAG_UPLOAD_BYTES // (1024 * 1024)} MB).")

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)

    try:
        structured = chunk_file(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    if not structured:
        raise HTTPException(status_code=400, detail="Could not extract content from file.")

    logger.info("Document chunked: file=%s chunks=%d", filename, len(structured))

    db.query(DocumentChunk).filter(
        DocumentChunk.source_file == filename,
        DocumentChunk.doc_category != DocCategory.JAVA_SOURCE,
    ).delete(synchronize_session=False)
    db.flush()

    uploaded_at = datetime.now(timezone.utc).isoformat()
    total_stored = 0
    for batch_start in range(0, len(structured), BATCH_SIZE):
        batch = structured[batch_start: batch_start + BATCH_SIZE]
        embeddings = embed_texts([row["content"] for row in batch])

        for idx, (row, embedding) in enumerate(zip(batch, embeddings)):
            db.add(DocumentChunk(
                id=generate_uuid(),
                source_file=filename,
                doc_category=category,
                content=row["content"],
                embedding=embedding,
                chunk_index=batch_start + idx,
                metadata_={
                    "file_name": filename,
                    "category": category,
                    "section_title": row.get("section_title"),
                    "page": row.get("page"),
                    "chunk_type": row.get("chunk_type", "text"),
                    "uploaded_at": uploaded_at,
                },
            ))
            total_stored += 1
        db.flush()

    db.commit()
    logger.info("Document stored: file=%s category=%s chunks=%d", filename, category, total_stored)

    return {"file_name": filename, "category": category, "chunks_created": total_stored}


@router.delete("/file/{file_name:path}")
def delete_file_chunks(file_name: str, db: DbDep, _: AdminUser):
    """Delete all chunks for a specific file."""
    chunks = (
        db.query(DocumentChunk)
        .filter(DocumentChunk.source_file == file_name)
        .filter(DocumentChunk.doc_category != DocCategory.JAVA_SOURCE)
        .all()
    )
    if not chunks:
        raise HTTPException(status_code=404, detail="File not found in knowledge base")

    count = len(chunks)
    for c in chunks:
        db.delete(c)
    db.commit()
    logger.info("File deleted: file=%s chunks=%d", file_name, count)
    return {"deleted": True, "file_name": file_name, "chunks_deleted": count}


@router.delete("/chunks")
def clear_chunks(db: DbDep, _: AdminUser):
    """Delete all product knowledge chunks (not java_source)."""
    count = db.query(DocumentChunk).filter(DocumentChunk.doc_category != DocCategory.JAVA_SOURCE).count()
    db.query(DocumentChunk).filter(DocumentChunk.doc_category != DocCategory.JAVA_SOURCE).delete()
    db.commit()
    return {"deleted_chunks": count}


@router.post("/search")
def test_search(payload: SearchRequest, db: DbDep, _: AdminUser):
    """Test retrieval — returns top-k chunks for a query."""
    if not payload.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty.")

    categories = payload.categories if payload.categories else None
    chunks = retrieve(payload.query, db, top_k=payload.top_k, categories=categories)
    return {"query": payload.query, "results": chunks, "count": len(chunks)}


@router.post("/bm25/rebuild")
def rebuild_bm25(db: DbDep, _: AdminUser):
    """Force a BM25 index rebuild for this worker process.

    Useful when the Redis generation counter hasn't propagated, or to
    confirm the current state of the in-memory index.
    """
    from app.rag import bm25_search
    count = bm25_search.build_index(db)
    return {"indexed_chunks": count, "ready": bm25_search.is_ready()}


@router.get("/bm25/status")
def bm25_status(_: AdminUser):
    """Report the current BM25 index state in this worker."""
    from app.rag import bm25_search
    return {
        "ready":          bm25_search.is_ready(),
        "indexed_chunks": bm25_search.size(),
    }


class ClassifyRequest(BaseModel):
    feature_description: str


@router.post("/taxonomy/classify")
async def taxonomy_classify(payload: ClassifyRequest, _: AdminUser):
    """Classify a feature description into the active domain's taxonomy for inspection."""
    from app.agents.taxonomy import classify
    result = await classify(payload.feature_description)
    # Drop the heavy 'bucket' key (already reflected in primary + required_fields)
    bucket = result.pop("bucket", {})
    return {
        **result,
        "required_fields":  bucket.get("required_fields", []),
        "analogue_queries": bucket.get("analogue_queries", []),
    }
