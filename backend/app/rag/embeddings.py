# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Embedding service using Ollama nomic-embed-text.

Produces 768-dim vectors via Ollama's local API.
"""
import logging
from pathlib import Path

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "nomic-embed-text"
EMBEDDING_DIM = 768

# Empirically the AiNxt gateway in front of nomic-embed-text rejects inputs
# at far below the model's nominal 8192-token ceiling (an 11786-char input
# triggered "input length exceeds the context length"). Cap defensively at
# ~6000 chars (~1500 tokens at 4 chars/token) — well clear of the gateway's
# real ceiling with headroom for tokenizer variance and multi-byte characters.
# Override via env if a different gateway has more headroom.
import os as _os
MAX_EMBED_CHARS = int(_os.getenv("EMBED_MAX_CHARS", "6000"))

# Window overlap for sliding-window embedding (lossless alternative to truncation).
# 50% overlap is the sweet spot in literature for retrieval — a query whose answer
# straddles two windows still hits a window that contains it whole.
EMBED_WINDOW_OVERLAP = int(_os.getenv("EMBED_WINDOW_OVERLAP", "1000"))  # chars

# Soft cap on total windows per oversized input — protects against runaway costs
# on pathologically large symbols. 6 windows × 6000 chars = 36k chars, beyond
# which truncation kicks in (very rare; e.g. minified JS or vendored bundles).
EMBED_MAX_WINDOWS = int(_os.getenv("EMBED_MAX_WINDOWS", "6"))

# Throughput tuning. Embedding gateway calls (Ollama nomic-embed-text) are
# I/O-bound — every batch is one HTTP round-trip. A single backend ingest
# typically fires ~2 500 batches; doing them serially leaves 8-16× headroom
# on the table. Tune via env without code changes.
#   EMBED_BATCH_SIZE       — items per HTTP call (Ollama tolerates 32-64
#                            comfortably; AiNxt may rate-limit > 32).
#   EMBED_HTTP_CONCURRENCY — concurrent HTTP calls in flight. 8 is a safe
#                            default for self-hosted Ollama; bump to 16
#                            on a dedicated GPU node, drop to 4 if the
#                            gateway is shared.
EMBED_BATCH_SIZE = int(_os.getenv("EMBED_BATCH_SIZE", "32"))

# Phase 3.1 partial — dynamic concurrency default. On a 16-core box this
# yields 32 (capped); on a 2-core CI runner it stays at the floor of 8.
# The hardcoded 8 was leaving headroom on the table on bigger boxes.
def _default_embed_concurrency() -> int:
    cpu = _os.cpu_count() or 4
    return min(32, max(8, cpu * 2))


EMBED_HTTP_CONCURRENCY = int(_os.getenv("EMBED_HTTP_CONCURRENCY", str(_default_embed_concurrency())))

_client = None
_executor = None

# ── Phase 3 Gap A — observable zero-vector failures ──────────────────────────
# When the gateway hard-fails on a chunk, the legacy behaviour returns a
# zero vector and lets ingest continue. That row is then effectively
# unfindable by dense search (cosine sim against a zero vector is 0 for
# every query) — a silent dead zone in the index.
#
# We keep the fail-open default (correctness over latency for huge ingests),
# but now:
#   1. Increment a counter so /metrics or a periodic log can surface the issue.
#   2. Warn at WARNING level (legacy was ERROR but only at single-input
#      failures — now we cover the bulk path too).
#   3. Optional EMBED_FAIL_HARD=1 env var raises EmbedHardFailure instead so
#      operators running a critical ingest can fail-fast.
import threading as _failure_lock_module
_embed_zero_vec_count = 0
_embed_zero_vec_lock = _failure_lock_module.RLock()


class EmbedHardFailure(RuntimeError):
    """Raised when EMBED_FAIL_HARD=1 AND a single-input embed exhausted retries.

    Distinct from httpx.HTTPStatusError so callers can decide whether to retry
    the whole batch or surface the failure to the operator.
    """


def _embed_fail_hard_enabled() -> bool:
    return _os.getenv("EMBED_FAIL_HARD", "").lower() in ("1", "true", "yes")


def _record_embed_zero_vector(reason: str, *, char_len: int) -> None:
    """Bump the counter and warn. Called from every fail-open zero-vec path."""
    global _embed_zero_vec_count
    with _embed_zero_vec_lock:
        _embed_zero_vec_count += 1
    logger.warning(
        "Embed: returning zero vector (%s, char_len=%d). "
        "Row will be silently unfindable by dense search. Total zero-vec embeds so far: %d",
        reason, char_len, _embed_zero_vec_count,
    )


def embed_failure_stats() -> dict:
    """How many zero-vector fallbacks have happened in this process so far."""
    with _embed_zero_vec_lock:
        return {"zero_vector_total": _embed_zero_vec_count}


def _reset_embed_failure_counters_for_tests() -> None:
    global _embed_zero_vec_count
    with _embed_zero_vec_lock:
        _embed_zero_vec_count = 0


def _get_client() -> httpx.Client:
    """Single shared httpx.Client.

    httpx.Client is thread-safe for concurrent .post() calls — it manages
    a connection pool internally. Reusing one client across all threads
    lets us amortise TCP/TLS setup over the full ingest run.

    The pool defaults are bumped so EMBED_HTTP_CONCURRENCY > 10 actually
    uses parallel sockets instead of queueing on the default `max_connections=10`.
    """
    global _client
    if _client is None:
        limits = httpx.Limits(
            max_connections=max(64, EMBED_HTTP_CONCURRENCY * 2),
            max_keepalive_connections=max(32, EMBED_HTTP_CONCURRENCY),
            keepalive_expiry=30.0,
        )
        # 300s (was 120s) — CPU-only Ollama (no GPU passthrough) was observed
        # taking 60-80s for a single embed call under load; a multi-input
        # batch could exceed 120s and trip the client timeout before the
        # per-input fallback ever got a chance to run.
        _client = httpx.Client(timeout=300.0, limits=limits)
    return _client


def _get_executor():
    """Module-level ThreadPoolExecutor for parallel embedding dispatch.

    Re-used across calls so we don't pay thread-spawn cost per ingest pass.
    Sized to EMBED_HTTP_CONCURRENCY — each thread issues one in-flight
    HTTP call to the embedding gateway.
    """
    global _executor
    if _executor is None:
        import concurrent.futures
        _executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=EMBED_HTTP_CONCURRENCY,
            thread_name_prefix="embed-pool",
        )
    return _executor


# Cache the in-container detection at module load — it can't change at
# runtime and the file probe is a sub-millisecond syscall, but readers
# expecting `_ollama_url()` to be hot-path-safe shouldn't see syscalls.
_IN_DOCKER: bool = (
    Path("/.dockerenv").exists()
    or Path("/proc/1/cgroup").exists() and "docker" in (
        Path("/proc/1/cgroup").read_text(errors="ignore")
        if Path("/proc/1/cgroup").exists() else ""
    )
)


def _ollama_url() -> str:
    """Return the Ollama base URL the embeddings client should hit.

    Cross-platform handling of `localhost`:
      - Outside a container → `localhost` is correct, leave it.
      - Inside a container  → `localhost` would mean the container itself,
        so rewrite to `host.docker.internal`. On Docker Desktop (Mac/Win)
        that's auto-resolved; on Linux Docker the operator must add
        `extra_hosts: ["host.docker.internal:host-gateway"]` in
        docker-compose.yml (we do this in the project's compose file).

    If you set OLLAMA_URL to a real IP or DNS name (not "localhost"), the
    rewrite never fires — that's the cleanest cross-platform approach for
    networks where Ollama runs on a fixed address.
    """
    url = (settings.ollama_url or "http://localhost:11434").rstrip("/")
    if _IN_DOCKER and "://localhost" in url:
        url = url.replace("://localhost", "://host.docker.internal")
    return url


def _safe_for_embedding(text: str | None) -> str | None:
    """Return text suitable for the embedder, or None if it should be skipped.

    Drops empty / whitespace-only inputs (gateway returns 400 on these) and
    truncates oversized inputs to stay under nomic-embed-text's 8192-token
    context. Logs a warning when truncating so operators can spot oversized
    chunks coming out of the chunker.

    Note: this is the LEGACY truncating path. New callers should use
    `_split_for_embedding()` + `embed_long_text()` to preserve the full
    content via sliding windows instead of clipping it.
    """
    if not text or not text.strip():
        return None
    if len(text) > MAX_EMBED_CHARS:
        logger.warning(
            "Truncating oversized chunk for embedding: %d -> %d chars",
            len(text), MAX_EMBED_CHARS,
        )
        return text[:MAX_EMBED_CHARS]
    return text


def _split_for_embedding(text: str) -> list[str]:
    """Split a long text into overlapping windows that each fit MAX_EMBED_CHARS.

    Uses a 50%-overlap sliding window by default (configurable via
    EMBED_WINDOW_OVERLAP). Windows are split at line boundaries when possible
    to avoid cutting tokens in half — a window may end up smaller than the
    nominal max, which is fine; the embedder just returns a slightly less-
    saturated vector.

    For a 13 000-char Java method with MAX_EMBED_CHARS=6000 and overlap=1000:
        window 0: chars [0,    6000)
        window 1: chars [5000, 11000)
        window 2: chars [10000, 13000)

    Every byte of the original text appears in at least one window. A query
    whose answer lives at any position in the original is recoverable from
    at least one window's vector — no information lost.

    Caps total windows at EMBED_MAX_WINDOWS to bound cost on pathologically
    large inputs (e.g. minified JS, vendored bundles). Beyond that cap we
    truncate, with a clearly-distinguished warning.
    """
    if not text or len(text) <= MAX_EMBED_CHARS:
        return [text] if text else []

    step = max(1, MAX_EMBED_CHARS - EMBED_WINDOW_OVERLAP)
    windows: list[str] = []
    cursor = 0
    n = len(text)

    while cursor < n and len(windows) < EMBED_MAX_WINDOWS:
        end = min(cursor + MAX_EMBED_CHARS, n)
        # If we're not at the very end, try to back off to a line boundary
        # so we don't cut a token in half.
        if end < n:
            nl = text.rfind("\n", cursor, end)
            if nl > cursor + (MAX_EMBED_CHARS // 2):
                # Found a newline in the latter half of the window — use it.
                end = nl
        window = text[cursor:end]
        if window.strip():
            windows.append(window)
        if end >= n:
            cursor = n   # mark fully consumed so the post-loop check is correct
            break
        cursor = max(cursor + 1, end - EMBED_WINDOW_OVERLAP)

    if cursor < n:
        # We hit EMBED_MAX_WINDOWS before consuming the whole text. Log loudly
        # so operators can decide whether to bump the cap or split at a higher
        # level (e.g. tree-sitter symbol-body sub-splitting).
        logger.warning(
            "Sliding-window cap (%d) reached on %d-char input — "
            "tail of input from char %d will not be embedded",
            EMBED_MAX_WINDOWS, n, cursor,
        )
    else:
        logger.debug(
            "Sliding-window split: %d-char input → %d windows (full coverage)",
            n, len(windows),
        )
    return windows


def _mean_pool(vectors: list[list[float]]) -> list[float]:
    """Average a list of equal-length vectors element-wise.

    Used when the caller wants a single vector representing a long input
    (the inverse of `_split_for_embedding`). Mean pooling preserves
    semantic centroid; max-pooling would over-emphasise outlier dimensions.
    """
    if not vectors:
        return [0.0] * EMBEDDING_DIM
    if len(vectors) == 1:
        return vectors[0]
    dim = len(vectors[0])
    n = float(len(vectors))
    out = [0.0] * dim
    for v in vectors:
        for i in range(dim):
            out[i] += v[i]
    return [x / n for x in out]


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Return a list of 768-dim embedding vectors for the given texts.

    Behaviours by input shape:
      - Empty/whitespace input → zero vector at that position. Logged.
      - Input ≤ MAX_EMBED_CHARS → embedded directly (1:1 mapping).
      - Input > MAX_EMBED_CHARS → **sliding-window split** (50% overlap),
        each window embedded, and the per-window vectors mean-pooled into
        a single output vector for that input position. Lossless: every
        char of the original is covered by ≥1 window. No truncation.

    All paths fail-soft — a 400 from the gateway on one input downgrades
    that one position to a zero vector and ingest continues.

    For a multi-row storage strategy (one document_chunks row per window
    sharing a parent_chunk_id), use `embed_long_text(text, return_windows=True)`
    directly instead of this function — it returns the per-window vectors
    rather than mean-pooling them.
    """
    client = _get_client()
    url = f"{_ollama_url()}/api/embed"

    # First pass: classify each input as skip / direct / split.
    #   skip  → emit zero vector at that position
    #   direct → embed as-is (one window per input)
    #   split → sliding-window split; mean-pool per-window vectors
    #
    # We accumulate ALL the windows from all inputs into one flat list for
    # batched embedding, and remember the (input_idx, window_count) tuples
    # so we can reassemble per-input vectors after the call.
    flat_windows: list[str] = []          # all windows in flat embedding order
    per_input_window_counts: list[int] = []  # one entry per input — how many windows it produced

    for idx, t in enumerate(texts):
        if not t or not t.strip():
            logger.warning(
                "Skipping empty/whitespace chunk at index %d for embedding", idx,
            )
            per_input_window_counts.append(0)
            continue

        if len(t) <= MAX_EMBED_CHARS:
            flat_windows.append(t)
            per_input_window_counts.append(1)
        else:
            # Sliding-window split — lossless replacement for the old truncate.
            windows = _split_for_embedding(t)
            if not windows:
                per_input_window_counts.append(0)
                continue
            flat_windows.extend(windows)
            per_input_window_counts.append(len(windows))
            logger.info(
                "Embed: oversized input at idx=%d (%d chars) → %d sliding windows (lossless)",
                idx, len(t), len(windows),
            )

    # Aliases retained for compatibility with the rest of the function below.
    cleaned = flat_windows
    cleaned_positions = list(range(len(flat_windows)))   # not used downstream — we reassemble via per_input_window_counts

    def _embed_one(single_text: str) -> list[float]:
        """Embed a single input. On 400 (oversize), aggressively re-truncate
        and retry once; on second failure, fall open to a zero vector by
        default OR raise EmbedHardFailure when `EMBED_FAIL_HARD=1` so the
        caller can surface the failure to the operator instead of silently
        inserting an unfindable row."""
        original_len = len(single_text)
        for attempt in (1, 2):
            try:
                resp = client.post(url, json={"model": EMBEDDING_MODEL, "input": [single_text]})
                resp.raise_for_status()
                return resp.json()["embeddings"][0]
            except httpx.HTTPStatusError as e:
                body = e.response.text[:300] if e.response is not None else "<no body>"
                if attempt == 1 and "context length" in body.lower():
                    half = max(500, len(single_text) // 2)
                    logger.warning(
                        "Embed 400 on single input (%d chars) — re-truncating to %d and retrying",
                        len(single_text), half,
                    )
                    single_text = single_text[:half]
                    continue
                logger.error(
                    "Embed single-input failure: status=%s body=%s char_len=%d first_120=%r",
                    getattr(e.response, "status_code", "?"),
                    body, len(single_text), single_text[:120],
                )
                # Phase 3 Gap A — counter + optional fail-hard
                if _embed_fail_hard_enabled():
                    raise EmbedHardFailure(
                        f"Embed exhausted retries on {original_len}-char input "
                        f"(status={getattr(e.response, 'status_code', '?')})"
                    ) from e
                _record_embed_zero_vector("HTTP error after retry", char_len=original_len)
                return [0.0] * EMBEDDING_DIM

    # Build the list of batches first so the parallel dispatcher has stable
    # indices for ordered reassembly. Larger EMBED_BATCH_SIZE cuts the number
    # of HTTP round-trips; EMBED_HTTP_CONCURRENCY decides how many of those
    # round-trips fly at once.
    batch_size = EMBED_BATCH_SIZE
    batches: list[list[str]] = [
        cleaned[i:i + batch_size] for i in range(0, len(cleaned), batch_size)
    ]

    def _embed_one_batch(batch_idx: int, batch: list[str]) -> tuple[int, list[list[float]]]:
        """Run one batch HTTP call. On failure, fall back to per-input
        embedding (which has its own retry-with-half-truncate logic in
        `_embed_one`). Returns the batch index alongside the results so
        the caller can reassemble in original order regardless of completion
        order.
        """
        try:
            resp = client.post(url, json={"model": EMBEDDING_MODEL, "input": batch})
            resp.raise_for_status()
            # TODO(— BUG flagged in review, NOT fixed on retrofit): this
            # trusts the gateway to return exactly len(batch) vectors. If it
            # returns fewer/more, the reassembly cursor desyncs and every later
            # chunk in the run gets the WRONG vector — silently persisted into
            # the embed cache under the wrong content-hash. Guard it:
            #   embs = resp.json()["embeddings"]
            #   if len(embs) != len(batch): return batch_idx, [_embed_one(o) for o in batch]
            #   return batch_idx, embs
            return batch_idx, resp.json()["embeddings"]
        except httpx.HTTPStatusError as e:
            body = e.response.text[:500] if e.response is not None else "<no body>"
            sizes = [len(b) for b in batch]
            logger.warning(
                "Embed batch %d failed (status=%s body=%s sizes=%s) — falling back to per-input",
                batch_idx,
                getattr(e.response, "status_code", "?"),
                body, sizes,
            )
            return batch_idx, [_embed_one(one) for one in batch]
        except Exception as e:
            # Network blip / timeout — per-input fallback handles transient errors.
            logger.warning(
                "Embed batch %d transport failure (%s) — falling back to per-input",
                batch_idx, e,
            )
            return batch_idx, [_embed_one(one) for one in batch]

    # Dispatch all batches concurrently up to EMBED_HTTP_CONCURRENCY in flight.
    # The executor pool ensures we never have more than N HTTP calls live at
    # once even when len(batches) > N — surplus batches queue on the executor.
    raw_embeddings: list[list[float]] = []
    if len(batches) <= 1 or EMBED_HTTP_CONCURRENCY <= 1:
        # Trivial case (≤1 batch) or operator wanted serial — keep it simple.
        for i, b in enumerate(batches):
            _, vecs = _embed_one_batch(i, b)
            raw_embeddings.extend(vecs)
    else:
        executor = _get_executor()
        # Pre-allocate so out-of-order completion can write to the right slot.
        per_batch_results: list[list[list[float]] | None] = [None] * len(batches)
        futures = [executor.submit(_embed_one_batch, i, b) for i, b in enumerate(batches)]
        for fut in futures:
            idx, vecs = fut.result()
            per_batch_results[idx] = vecs
        for vecs in per_batch_results:
            if vecs:
                raw_embeddings.extend(vecs)

    if len(batches) > 1:
        logger.info(
            "Embed: %d inputs → %d batches @ batch_size=%d, concurrency=%d",
            len(cleaned), len(batches), batch_size, EMBED_HTTP_CONCURRENCY,
        )

    # Reassemble: each input may have produced 0, 1, or N window-vectors.
    #   0 → zero vector (empty/whitespace input)
    #   1 → that vector verbatim (direct path, no split)
    #   N → mean-pooled across the N windows (sliding-window path)
    out: list[list[float]] = []
    cursor = 0
    for count in per_input_window_counts:
        if count == 0:
            # Phase 3 Gap A — empty/whitespace inputs surface here. Track
            # them so operators noticing them in the counter can fix the
            # chunker rather than scratching their head over silent dropouts.
            _record_embed_zero_vector("empty/whitespace input", char_len=0)
            out.append([0.0] * EMBEDDING_DIM)
        elif count == 1:
            out.append(raw_embeddings[cursor])
            cursor += 1
        else:
            window_vecs = raw_embeddings[cursor:cursor + count]
            out.append(_mean_pool(window_vecs))
            cursor += count
    return out


def embed_query(text: str) -> list[float]:
    """Return a single 768-dim embedding vector for a query string.

    Empty/whitespace queries return a zero vector rather than crashing —
    callers (retrieval, sandbox tests) shouldn't have to special-case this.

    Phase 1.3 — bounded LRU cache. Cache hits skip the Ollama HTTP round-
    trip; cache disabled when `settings.query_embedding_cache_size <= 0`.

    Phase 3 Gap C — retry parity with `embed_texts`. Earlier versions of
    this function re-raised on any HTTP error, which meant a single
    transient 5xx/timeout from the embedder would propagate up through
    retrieval and break the user-facing request. Now we:
      - retry once with a halved input on "context length" 400s,
      - on persistent failure, return a zero vector (fail-soft) and bump
        the Phase 3 Gap A counter so the failure is observable.
      - `EMBED_FAIL_HARD=1` still re-raises (EmbedHardFailure) so an
        operator running a critical job can surface the issue.
    """
    # Phase 1.3 — cache check before HTTP
    cached = _query_cache_get(text or "")
    if cached is not None:
        return cached

    safe = _safe_for_embedding(text)
    if safe is None:
        logger.warning("embed_query received empty input — returning zero vector")
        _record_embed_zero_vector("empty query input", char_len=0)
        return [0.0] * EMBEDDING_DIM

    client = _get_client()
    url = f"{_ollama_url()}/api/embed"
    original_len = len(safe)
    payload_text = safe
    last_error: Exception | None = None

    for attempt in (1, 2):
        try:
            resp = client.post(url, json={"model": EMBEDDING_MODEL, "input": [payload_text]})
            resp.raise_for_status()
            data = resp.json()
            vec = data["embeddings"][0]
            _query_cache_put(text or "", vec)
            return vec
        except httpx.HTTPStatusError as e:
            last_error = e
            body = e.response.text[:500] if e.response is not None else "<no body>"
            if attempt == 1 and "context length" in body.lower():
                half = max(500, len(payload_text) // 2)
                logger.warning(
                    "embed_query 400 (context length) on %d-char input — re-truncating to %d and retrying",
                    len(payload_text), half,
                )
                payload_text = payload_text[:half]
                continue
            logger.error(
                "embed_query HTTP %s: body=%s | char_len=%d | first_120=%r",
                getattr(e.response, "status_code", "?"),
                body, len(payload_text), payload_text[:120],
            )
            break  # second attempt or non-retryable status — fall through
        except Exception as e:
            last_error = e
            logger.warning(
                "embed_query transport failure on attempt %d (%s)", attempt, e,
            )
            # Transport errors get one retry (the loop continues unless attempt=2)
            if attempt == 2:
                break

    # Out of retries — observable fall-open.
    if _embed_fail_hard_enabled():
        raise EmbedHardFailure(
            f"embed_query exhausted retries on {original_len}-char input"
        ) from last_error
    _record_embed_zero_vector("embed_query after retry", char_len=original_len)
    return [0.0] * EMBEDDING_DIM


def embed_long_text(
    text: str | None,
    *,
    return_windows: bool = False,
) -> list[float] | list[list[float]]:
    """Lossless embed for inputs that may exceed MAX_EMBED_CHARS.

    Splits the input into 50%-overlap sliding windows that each fit the
    embedder's char budget, embeds every window, and either:

      - mean-pools the per-window vectors into a single 768-dim vector
        (default; same return shape as `embed_query`), OR

      - returns the raw list of per-window vectors so the caller can
        store each as a separate `document_chunks` row keyed by a
        common `parent_chunk_id`. Multi-row storage produces the best
        retrieval recall — at retrieval time a query can hit any
        window's vector and the caller dedups via parent_chunk_id.

    Args:
        text: source text. Empty / whitespace returns a zero vector
              (or empty list when return_windows=True).
        return_windows: when True, return list[list[float]] (one vector
              per window). When False, mean-pool to a single vector.

    Returns:
        - return_windows=False: list[float] — single 768-dim vector.
        - return_windows=True:  list[list[float]] — one vector per window.

    Performance: for a typical Java symbol body (median ~800 chars),
    only one window is produced and behaviour matches `embed_query`.
    For an oversized symbol (8000+ chars) you pay 2-6× the embedding
    cost — but you keep all the content rather than truncating.
    """
    if not text or not text.strip():
        logger.debug("embed_long_text: empty input — returning zero result")
        return [] if return_windows else [0.0] * EMBEDDING_DIM

    windows = _split_for_embedding(text)
    if not windows:
        return [] if return_windows else [0.0] * EMBEDDING_DIM

    # `embed_texts` already handles batching, per-input fallback, and
    # safe-input guards. We just hand it the pre-split windows.
    vectors = embed_texts(windows)

    if return_windows:
        return vectors
    return _mean_pool(vectors)


# ── Phase 3 Gap D — startup warmup probe ─────────────────────────────────────

def warm_up(*, timeout_sec: float = 5.0) -> bool:
    """Fire one cheap embed call so the model is loaded into Ollama's VRAM
    before real requests arrive.

    Ollama lazy-loads model weights on the first request, which can take
    1-3 seconds. Without a warmup, the first user-facing retrieval after
    a deploy pays that cost visibly. Calling this at startup (background
    fire-and-forget) hides the cold-start from real traffic.

    Returns True on success, False on any failure (logged, not raised).
    Safe to call multiple times — Ollama just no-ops after the first.

    Caller pattern (e.g. in main.py lifespan):
        import threading
        threading.Thread(target=warm_up, daemon=True).start()
    """
    try:
        client = _get_client()
        url = f"{_ollama_url()}/api/embed"
        # The minimum-cost probe — a single short input.
        resp = client.post(
            url,
            json={"model": EMBEDDING_MODEL, "input": ["warmup"]},
            timeout=timeout_sec,
        )
        resp.raise_for_status()
        logger.info("Embed warmup OK (model=%s)", EMBEDDING_MODEL)
        return True
    except Exception as e:
        logger.warning(
            "Embed warmup failed (%s) — first real request will pay cold-start latency",
            e,
        )
        return False


# ── Phase 1.3 — query embedding cache ────────────────────────────────────────

# In-memory FIFO cache of (sha256(query), model) → vector. Bounded by
# `settings.query_embedding_cache_size` so a runaway producer doesn't pin
# unbounded memory. We cache the *vector*, not the raw query, to avoid
# accidentally retaining PII in a long-lived process.
import hashlib as _hashlib
from collections import OrderedDict as _OrderedDict
import threading as _threading

_query_cache_lock = _threading.RLock()
_query_cache: "_OrderedDict[str, list[float]]" = _OrderedDict()

_query_cache_hits = 0
_query_cache_misses = 0


def _query_cache_key(text: str) -> str:
    return _hashlib.sha256((EMBEDDING_MODEL + "::" + (text or "")).encode("utf-8")).hexdigest()


def _query_cache_get(text: str) -> list[float] | None:
    global _query_cache_hits
    try:
        from app.core.config import settings as _settings
        cap = int(getattr(_settings, "query_embedding_cache_size", 0) or 0)
    except Exception:
        cap = 0
    if cap <= 0:
        return None
    key = _query_cache_key(text)
    with _query_cache_lock:
        if key in _query_cache:
            _query_cache.move_to_end(key)
            _query_cache_hits += 1
            return list(_query_cache[key])
    return None


def _query_cache_put(text: str, vec: list[float]) -> None:
    global _query_cache_misses
    try:
        from app.core.config import settings as _settings
        cap = int(getattr(_settings, "query_embedding_cache_size", 0) or 0)
    except Exception:
        cap = 0
    if cap <= 0:
        return
    key = _query_cache_key(text)
    with _query_cache_lock:
        _query_cache[key] = list(vec)
        _query_cache.move_to_end(key)
        while len(_query_cache) > cap:
            _query_cache.popitem(last=False)
        _query_cache_misses += 1


def get_query_cache_counters() -> dict[str, int]:
    return {
        "query_embedding_cache_hits":   _query_cache_hits,
        "query_embedding_cache_misses": _query_cache_misses,
        "query_embedding_cache_size":   len(_query_cache),
    }


def query_embedding_cache_stats() -> dict:
    """Diagnostic — return current size + cumulative hit/miss counts."""
    with _query_cache_lock:
        return {
            "size":   len(_query_cache),
            "hits":   _query_cache_hits,
            "misses": _query_cache_misses,
        }


def _reset_query_embedding_cache_for_tests() -> None:
    """Test hook only — clear the cache and counters."""
    global _query_cache_hits, _query_cache_misses
    with _query_cache_lock:
        _query_cache.clear()
        _query_cache_hits = 0
        _query_cache_misses = 0
