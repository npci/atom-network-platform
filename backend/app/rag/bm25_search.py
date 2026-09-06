# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Sparse keyword search backing the hybrid retrieval pipeline.

Two backends, selected via `settings.bm25_backend`:

  - "tsvector" (default — Phase 1.2): runs a single Postgres query using
    the `content_tsv` GIN index built by migration 0027. Stateless,
    incrementally updated by Postgres on every INSERT/UPDATE, no per-process
    memory footprint.

  - "rank_bm25" (legacy): in-process `rank_bm25.BM25Okapi` index built at
    startup and rebuilt after each ingest. Higher fidelity for very short
    documents but costly in RAM (linear in chunk count, per worker) and
    requires a generation-counter polling dance to stay fresh.

The two backends share a single public surface so callers don't change:

    bm25_search.is_ready()
    bm25_search.ensure_fresh(db)
    bm25_search.build_index(db)
    bm25_search.search(query, top_k=10) -> list[(chunk_id, score)]
    bm25_search.tokenize(text) -> list[str]      # used by hybrid overlap filter

`tokenize` always returns the in-process Python tokenisation — even when
the active backend is tsvector — because the hybrid overlap filter
(`MIN_OVERLAP_TOKENS`) has to score Python-side against retrieved chunk
contents and shouldn't depend on a Postgres round-trip.
"""
import logging
import re
import threading
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.document_chunk import DocumentChunk

logger = logging.getLogger(__name__)


# ── Tokenizer ────────────────────────────────────────────────────────────────

# Minimal English stopwords — BM25 itself down-weights common terms,
# but pre-stripping reduces index size and speeds scoring.
_STOPWORDS = frozenset([
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "do", "does", "did",
    "will", "would", "should", "could", "may", "might", "can", "in",
    "on", "at", "to", "for", "of", "with", "by", "from", "as", "this",
    "that", "these", "those", "it", "its", "if", "then", "than", "so",
    "not", "no", "yes",
])

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")


def tokenize(text_: str) -> list[str]:
    """Lowercase + split on non-alphanumeric + drop stopwords + min length 2."""
    if not text_:
        return []
    raw = _TOKEN_RE.findall(text_.lower())
    return [t for t in raw if len(t) >= 2 and t not in _STOPWORDS]


# ── Backend selection ────────────────────────────────────────────────────────

def _backend() -> str:
    """Return the active backend string. Defaults to tsvector. Unknown
    values fall back to rank_bm25 for safety."""
    name = (getattr(settings, "bm25_backend", None) or "tsvector").lower().strip()
    if name in ("tsvector", "rank_bm25"):
        return name
    logger.warning("Unknown bm25_backend=%r — falling back to rank_bm25", name)
    return "rank_bm25"


# ── In-memory rank_bm25 backend (legacy) ──────────────────────────────────────

_lock = threading.RLock()

# Parallel arrays — row i of each corresponds to the same chunk.
_chunk_ids: list[str] = []
_corpus_tokens: list[list[str]] = []
_bm25: Any = None

# Generation tracking — bumped in Redis on every ingestion.
_local_generation: int = 0


def _read_remote_generation() -> int:
    """Read the ingestion generation counter from Redis (0 if unavailable)."""
    try:
        import redis
        r = redis.from_url(settings.redis_url, socket_timeout=0.5)
        val = r.get("bm25:generation")
        return int(val) if val else 0
    except Exception:
        return 0


def _build_rank_bm25(db: Session) -> int:
    """Build the in-memory BM25Okapi index. Returns rows indexed."""
    global _chunk_ids, _corpus_tokens, _bm25, _local_generation

    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        logger.warning("rank-bm25 not installed; rank_bm25 backend disabled")
        with _lock:
            _chunk_ids = []
            _corpus_tokens = []
            _bm25 = None
        return 0

    rows = db.query(DocumentChunk.id, DocumentChunk.content).all()
    new_chunk_ids = [row.id for row in rows]
    new_tokens = [tokenize(row.content) for row in rows]

    if not new_chunk_ids:
        with _lock:
            _chunk_ids = []
            _corpus_tokens = []
            _bm25 = None
            _local_generation = _read_remote_generation()
        return 0

    new_bm25 = BM25Okapi(new_tokens)

    with _lock:
        _chunk_ids = new_chunk_ids
        _corpus_tokens = new_tokens
        _bm25 = new_bm25
        _local_generation = _read_remote_generation()

    logger.info("BM25 (rank_bm25) index built: %d chunks (generation=%d)",
                len(new_chunk_ids), _local_generation)
    return len(new_chunk_ids)


def _search_rank_bm25(query: str, top_k: int) -> list[tuple[str, float]]:
    with _lock:
        if _bm25 is None or not _chunk_ids:
            return []
        bm25 = _bm25
        ids = _chunk_ids

    tokens = tokenize(query)
    if not tokens:
        return []

    scores = bm25.get_scores(tokens)
    ranked = sorted(
        ((ids[i], float(s)) for i, s in enumerate(scores) if s > 0),
        key=lambda t: t[1],
        reverse=True,
    )
    return ranked[:top_k]


def _ensure_fresh_rank_bm25(db: Session) -> bool:
    remote = _read_remote_generation()
    with _lock:
        if remote <= _local_generation and _bm25 is not None:
            return False
    logger.info("BM25 generation changed (local=%d remote=%d) — rebuilding",
                _local_generation, remote)
    _build_rank_bm25(db)
    return True


# ── Postgres tsvector backend (default — Phase 1.2) ───────────────────────────

# Cached probe result — `content_tsv` column is checked once per process.
_TSV_AVAILABLE: bool | None = None


def _tsv_available(db: Session) -> bool:
    """True if migration 0027 has run and the `content_tsv` column exists.

    Cached in-process. Fail-closed when the probe itself raises so we
    don't keep retrying on a broken DB.
    """
    global _TSV_AVAILABLE
    if _TSV_AVAILABLE is not None:
        return _TSV_AVAILABLE
    try:
        row = db.execute(text("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'document_chunks'
              AND column_name = 'content_tsv'
            LIMIT 1
        """)).first()
        _TSV_AVAILABLE = bool(row)
    except Exception as e:
        logger.warning("tsvector probe failed: %s", e)
        _TSV_AVAILABLE = False
    return _TSV_AVAILABLE


def _reset_tsv_probe_for_tests() -> None:
    """Test hook — clear the cached probe result."""
    global _TSV_AVAILABLE
    _TSV_AVAILABLE = None


def _tsv_config_name() -> str:
    """Phase 8.2 — return the text-search config to use for tsquery.
    'english' (default) stems aggressively; 'simple_code' (requires
    migration 0035) skips stemming + stoplist filtering, preserving
    code identifiers verbatim.
    """
    name = (getattr(settings, "bm25_text_search_config", None) or "english").strip()
    # Defensive whitelist — `plainto_tsquery(:cfg, :q)` would also work
    # with bind params, but pg_catalog.regconfig binding is awkward across
    # drivers. We inline the config name after whitelisting.
    if name not in ("english", "simple", "simple_code"):
        logger.warning("Unknown bm25_text_search_config=%r — falling back to 'english'", name)
        return "english"
    return name


def _search_tsvector(db: Session, query: str, top_k: int) -> list[tuple[str, float]]:
    """Run BM25-equivalent ranking via Postgres `ts_rank_cd`.

    `plainto_tsquery` is robust to user-supplied input — it ignores
    quotes, operators, and stop-words automatically. The dictionary
    used depends on `settings.bm25_text_search_config` (Phase 8.2):

      - 'english' (default) — generated-column-compatible, prose-friendly.
      - 'simple_code' — Phase 8.2 — preserves code identifiers verbatim
        (no stemming, no English stoplist). Requires migration 0035.

    Both use the same `content_tsv` column and GIN index, so switching is
    a hot operation — no rebuild.
    """
    if not query or not query.strip():
        return []
    cfg = _tsv_config_name()
    sql = text(f"""
        SELECT id,
               ts_rank_cd(content_tsv, plainto_tsquery('{cfg}', :q)) AS score
        FROM document_chunks
        WHERE content_tsv @@ plainto_tsquery('{cfg}', :q)
          AND (deprecated IS NOT TRUE)
        ORDER BY score DESC
        LIMIT :top_k
    """)
    try:
        rows = db.execute(sql, {"q": query, "top_k": top_k}).fetchall()
    except Exception as e:
        # If the column / index isn't there (migration not applied yet on
        # an env that flipped the flag), OR the config name doesn't exist
        # in this DB, fail soft and return [].
        logger.warning("tsvector search failed (%s, cfg=%s); returning []", e, cfg)
        if cfg != "english":
            # Auto-retry once with the safe default so a misconfigured env
            # doesn't lose retrieval entirely.
            sql_fallback = text("""
                SELECT id,
                       ts_rank_cd(content_tsv, plainto_tsquery('english', :q)) AS score
                FROM document_chunks
                WHERE content_tsv @@ plainto_tsquery('english', :q)
                  AND (deprecated IS NOT TRUE)
                ORDER BY score DESC
                LIMIT :top_k
            """)
            try:
                rows = db.execute(sql_fallback, {"q": query, "top_k": top_k}).fetchall()
            except Exception as e2:
                logger.warning("english fallback also failed: %s", e2)
                return []
        else:
            return []
    return [(r.id, float(r.score)) for r in rows]


# ── Public surface ───────────────────────────────────────────────────────────

def size() -> int:
    """Return number of chunks in the active in-memory index.

    For the tsvector backend this is meaningless (the index is in
    Postgres) so we return 0 — callers that care about index size should
    consult `is_ready()` instead, which the tsvector backend implements
    via a column-existence probe.
    """
    if _backend() == "tsvector":
        return 0
    with _lock:
        return len(_chunk_ids)


def is_ready(db: Session | None = None) -> bool:
    """True if the active backend can answer a search."""
    backend = _backend()
    if backend == "tsvector":
        if db is None:
            # Without a session we have to assume the column exists if
            # the cached probe says so.
            return _TSV_AVAILABLE is True
        return _tsv_available(db)
    with _lock:
        return _bm25 is not None and len(_chunk_ids) > 0


def build_index(db: Session) -> int:
    """Build / rebuild the active backend's index.

    For tsvector this is a no-op (Postgres maintains the column on its
    own). Returns 0 in that case.
    """
    backend = _backend()
    if backend == "tsvector":
        # Probe once so subsequent `is_ready()` calls without a session
        # can answer correctly.
        _tsv_available(db)
        logger.info("BM25 backend=tsvector — Postgres maintains the index; build_index() is a no-op")
        return 0
    return _build_rank_bm25(db)


def ensure_fresh(db: Session) -> bool:
    """Backend-specific freshness check.

    tsvector → no-op (Postgres maintains the index).
    rank_bm25 → poll Redis generation counter and rebuild on bump.
    """
    if _backend() == "tsvector":
        return False
    return _ensure_fresh_rank_bm25(db)


def search(query: str, top_k: int = 10, db: Session | None = None) -> list[tuple[str, float]]:
    """Return [(chunk_id, score)] sorted by descending score.

    The tsvector backend requires `db`; the rank_bm25 backend ignores it.
    Callers that don't have a session can pass None — the rank_bm25 path
    works without; the tsvector path returns [] with a warning.
    """
    backend = _backend()
    if backend == "tsvector":
        if db is None:
            logger.warning("tsvector backend requires a Session; returning []")
            return []
        return _search_tsvector(db, query, top_k)
    return _search_rank_bm25(query, top_k)
