# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""LLM-call observability shim (Slice 28).

When `settings.use_observability_traces` is True, every LLM call emits a
single structured JSON log line tagged with the agent name, purpose
bucket (Slice 27), provider, model, latency, prompt/response sizes, and
success/failure. Operators forward those to whichever backend they use
(Langfuse, OpenTelemetry collector, Datadog, plain log search).

This v0 deliberately ships NO hard Langfuse / OTEL SDK dependency. The
gain from a tagged structured log is sufficient for the immediate need
(slicing cost-per-purpose now that Slice 27 routes 7 workloads). A
follow-up sub-slice (28a) wires a real backend SDK once operators pick
one.

The shim is fail-open: any error inside `record_llm_call` is caught +
logged at WARN level and never propagates back to the caller. We do not
want "telemetry broke" to mean "agents broke".
"""
from __future__ import annotations

import dataclasses
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings

# Dedicated logger so operators can route observability output separately
# from app logs (e.g. ship "app.observability" to Langfuse, "app" to stderr).
logger = logging.getLogger("app.observability")


# ── Slice 24+ — dedicated rotating file for LLM call traces ──────────────────
#
# Every LlmCallTrace lands as one JSONL row in `LLM_CALL_LOG_PATH`
# (default /app/logs/llm_calls.jsonl). RotatingFileHandler caps size:
# 50 MB per file, last 10 retained → ~500 MB hard ceiling. Operators
# can `jq`/`duckdb` against the file directly. The handler is in
# addition to the standard "app.observability" emit so existing
# Langfuse / Datadog wiring keeps working.
#
# The dedicated logger doesn't propagate (else every LLM call would
# also land in the main backend log, doubling volume). Default level
# INFO so debug-level diagnostics in this file stay quiet.
from logging.handlers import RotatingFileHandler as _RotatingFileHandler
from pathlib import Path as _Path

LLM_CALL_LOGGER_NAME = "app.llm_calls"
_llm_logger = logging.getLogger(LLM_CALL_LOGGER_NAME)
_llm_logger.propagate = False
_llm_logger.setLevel(logging.INFO)
_llm_logger_configured = False


def _setup_llm_call_logger() -> None:
    """Attach a RotatingFileHandler to `app.llm_calls` once.

    Idempotent — checks `_llm_logger_configured` so re-imports / test
    sessions don't double-attach. Falls back silently to "no file
    handler" if the directory is unwritable; the trace then only goes
    to the main observability logger.
    """
    global _llm_logger_configured
    if _llm_logger_configured:
        return
    _llm_logger_configured = True

    # Read from settings (not os.getenv) so the value in .env is honoured —
    # pydantic parses .env, but the raw process env usually doesn't carry it.
    # Blank → co-locate with the other diagnostics files (mounted + writable),
    # not the old unmounted /tmp default nobody could find.
    path_str = settings.llm_call_log_path
    if not path_str:
        try:
            from app.core import diag
            d = diag.diag_dir()
            path_str = str(d / "llm_calls.jsonl") if d else "/tmp/cm-platform-logs/llm_calls.jsonl"
        except Exception:
            path_str = "/tmp/cm-platform-logs/llm_calls.jsonl"
    try:
        _Path(path_str).parent.mkdir(parents=True, exist_ok=True)
        handler = _RotatingFileHandler(
            path_str,
            maxBytes=50 * 1024 * 1024,   # 50 MB
            backupCount=10,              # 10 rotated files → ~500 MB cap
            encoding="utf-8",
        )
        # Pure JSON line per record; no formatter prefix so jq + duckdb
        # can consume the file as JSONL without preprocessing.
        handler.setFormatter(logging.Formatter("%(message)s"))
        _llm_logger.addHandler(handler)
        logger.info("LLM call log → %s (50MB×10 rotation)", path_str)
    except OSError as exc:
        logger.warning(
            "Could not open LLM call log at %s (%s) — traces will only "
            "appear in the main observability logger.", path_str, exc,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Trace dataclass
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class LlmCallTrace:
    """One observation of a single LLM call.

    Schema is explicit so log queries / dashboards have stable field
    names. New fields are append-only — never rename or remove without
    a deprecation pass.
    """
    agent_name:        str
    purpose:           str
    provider:          str
    model:             str
    streaming:         bool
    prompt_chars:      int
    response_chars:    int
    response_chunks:   int
    elapsed_ms:        int
    success:           bool
    error_class:       str | None = None
    error_message:     str | None = None
    # R-9 — agent_jobs.id of the parent durable job (when invoked from
    # a tracked WS handler / REST endpoint / docgen pipeline). Optional
    # because direct call_llm() consumers (tests, ad-hoc scripts) don't
    # have a job context. Populated via the new `job_id` kwarg on
    # `record_llm_call_kwargs` and threaded through the call_llm /
    # stream_llm signatures so every LLM round-trip can be linked back
    # to the user-visible job in the agent_jobs table.
    job_id:            str | None = None
    # ── Slice 24+ extension — size/usage/cost ───────────────────────
    # Captured on every successful call (where the provider returns
    # usage). All optional so legacy callers and provider failures
    # don't break the schema.
    input_tokens:       int  | None = None    # provider-reported, NOT char/4 estimate
    output_tokens:      int  | None = None    # provider-reported
    cache_read_tokens:  int  | None = None    # Anthropic prompt-cache hit
    cache_write_tokens: int  | None = None    # Anthropic prompt-cache write
    cost_usd:           float | None = None   # via app.core.llm_costs.compute_cost_usd
    # Why the model stopped. Provider-native value; not normalised:
    #   Anthropic: end_turn / max_tokens / stop_sequence / tool_use
    #   OpenAI:    stop / length / content_filter / tool_calls
    # `length` (OpenAI) and `max_tokens` (Anthropic) both mean "hit cap";
    # operators interpret per provider. Useful when JSON-recovery sees
    # truncated output and we want to know whether to bump max_tokens.
    stop_reason:        str | None = None
    extra:             dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        # Drop None / empty fields to keep log lines tight. `extra` only
        # serialised when non-empty.
        if not d["extra"]:
            d.pop("extra")
        if d["error_class"] is None:
            d.pop("error_class")
            d.pop("error_message")
        # Drop optional Slice-24 extension fields when None — keeps the
        # JSONL clean for legacy callsites that don't yet thread the
        # usage info through.
        for k in (
            "job_id",
            "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_write_tokens", "cost_usd",
            "stop_reason",
        ):
            if d.get(k) is None:
                d.pop(k, None)
        return d


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def agent_purpose_label(agent_name: str) -> str:
    """Resolve an agent name → purpose string for trace tagging.

    Wraps `app.core.llm_router.purpose_for_agent` but tolerates the
    router being absent (e.g. import error during a partial deploy).
    Returns `"unknown"` rather than raising.
    """
    if not agent_name:
        return "unknown"
    try:
        from app.core.llm_router import purpose_for_agent
        return purpose_for_agent(agent_name).value
    except Exception:
        return "unknown"


def now_monotonic_seconds() -> float:
    """Indirection for tests — easier to monkeypatch this single name
    than to monkeypatch `time.monotonic` globally."""
    return time.monotonic()


# ──────────────────────────────────────────────────────────────────────────────
# Public emit API
# ──────────────────────────────────────────────────────────────────────────────

# ── Usage ledger (Usage dashboard) ──────────────────────────────────────────────
# A contextvar carries the change/run/kind a burst of LLM calls belongs to. The agentic
# orchestrator sets it around a drive; non-flow callers (docgen/eval) may set it too, else
# their calls persist with these null (rolled up as 'other (non-flow) usage by section').
import contextvars as _contextvars  # noqa: E402

_usage_ctx: "_contextvars.ContextVar[dict | None]" = _contextvars.ContextVar("llm_usage_ctx", default=None)


def set_usage_context(*, change_request_id: str | None = None, run_id: str | None = None,
                      kind: str | None = None, section_default: str | None = None):
    """Tag subsequent LLM calls (in this async context) with the owning change/run/kind.

    ``section_default`` overrides the per-row section for callers that share one generic
    ``agent_name`` (the docgen bridge tags every BRD/TSD/… call ``docgen_bridge``; the runner
    sets a per-document section here so they split correctly). Calls that carry their own
    specific agent_name keep it. Returns the contextvar token so the caller can reset() it."""
    return _usage_ctx.set({"change_request_id": change_request_id, "run_id": run_id,
                           "kind": kind, "section_default": section_default})


def reset_usage_context(token) -> None:
    try:
        _usage_ctx.reset(token)
    except Exception:  # noqa: BLE001 — best-effort
        _usage_ctx.set(None)


def current_usage_context() -> dict | None:
    """Read the current usage context (change_request_id / run_id / kind / section_default),
    or None if unset. Public accessor so the transcript writer can name its per-change folder
    from the same contextvar the Usage ledger uses — no signature threading through stages."""
    return _usage_ctx.get()


def log_cache_probe(system, agent_name: str | None) -> None:
    """Prompt-cache viability probe (flag-gated, log-only, never raises).

    Emits one CACHE_PROBE line per LLM call with a hash of the system prompt
    (the cache-prefix candidate), its length, the owning run, and the agent.
    Grep `CACHE_PROBE` across a real run and group by (run_id, sys_hash): if a
    hash repeats within a run — and the repeats fall inside the 5-min TTL, and
    the block clears ~4096 chars — that agent is a real caching candidate for
    AiNxt's /ask/cached endpoint. If hashes never repeat, caching cannot help.
    Log-only: no behaviour change, no dependency on the /ask/cached endpoint.
    """
    try:
        from app.core.config import settings
        if not getattr(settings, "cache_probe_logging", False):
            return

        # `system` may be a plain string or a segmented list of Anthropic
        # cache_control blocks — normalise both to the same text so the hash is
        # stable regardless of how the caller shaped the prompt.
        if isinstance(system, list):
            parts = []
            for seg in system:
                if isinstance(seg, dict):
                    parts.append(str(seg.get("text") or seg.get("content") or ""))
                else:
                    parts.append(str(seg))
            text = "".join(parts)
        else:
            text = system or ""

        import hashlib
        sys_hash = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]

        ctx = _usage_ctx.get() or {}
        logger.info(
            "CACHE_PROBE agent=%s run_id=%s change_request_id=%s sys_hash=%s sys_len=%d cacheable=%s",
            agent_name or "unknown",
            ctx.get("run_id") or "-",
            ctx.get("change_request_id") or "-",
            sys_hash, len(text), len(text) >= 4096,
        )
    except Exception:  # noqa: BLE001 — a probe must never affect the call path
        pass


import re as _re  # noqa: E402
_CHANGE_ID_RE = _re.compile(r"/changes/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


class UsageContextMiddleware:
    """ASGI middleware that tags every LLM call made while serving a ``/changes/{id}`` request
    (REST or WebSocket) with that change. Path-based, so the ENTIRE change lifecycle — from the
    FIRST phase (prompt enhancement) through research / canvas / BRD / TSD / XSD / code — is
    attributed to the change in the Usage dashboard, with no per-endpoint wiring. (Codegen runs
    in a celery worker, not an HTTP request, so the orchestrator sets the context there directly.)
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket"):
            m = _CHANGE_ID_RE.search(scope.get("path") or "")
            if m:
                tok = set_usage_context(change_request_id=m.group(1))
                try:
                    await self.app(scope, receive, send)
                finally:
                    reset_usage_context(tok)
                return
        await self.app(scope, receive, send)


def _persist_usage_row(trace: "LlmCallTrace") -> None:
    """Best-effort: append one row to `llm_usage_records`. NEVER raises — a usage-ledger write
    must not affect the LLM call that produced it. Skips calls without token data (failures,
    providers that strip usage) so the ledger only holds real spend."""
    try:
        from app.core.config import settings
        if not getattr(settings, "persist_llm_usage", True):
            return
        if not trace.success:
            return
        if not (trace.input_tokens or trace.output_tokens):
            return
        ctx = _usage_ctx.get() or {}
        # A generic shared agent_name (the docgen bridge's "docgen_bridge") is replaced by the
        # context's per-document section when one is set; specific agent names are kept as-is.
        section = trace.agent_name
        if (not section or section == "docgen_bridge") and ctx.get("section_default"):
            section = ctx["section_default"]
        from app.core.database import SessionLocal
        from app.models.agentic import LlmUsageRecord
        from app.models.base import generate_uuid, utcnow
        db = SessionLocal()
        try:
            db.add(LlmUsageRecord(
                id=generate_uuid(), ts=utcnow(),
                change_request_id=ctx.get("change_request_id"),
                run_id=ctx.get("run_id"), kind=ctx.get("kind"),
                section=section, model=trace.model,
                input_tokens=trace.input_tokens or 0, output_tokens=trace.output_tokens or 0,
                cache_read_tokens=trace.cache_read_tokens or 0,
                cache_write_tokens=trace.cache_write_tokens or 0,
                cost_usd=trace.cost_usd))
            db.commit()
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 — telemetry must never break a call
        logger.debug("usage ledger write skipped: %s", e)


def get_cumulative_run_tokens(run_id: str) -> int:
    """Sum of input+output+cache tokens persisted so far for one agentic run.

    Backs the A6 (architecture review High #5) per-run token budget guard in
    `agents/agentic_runtime.py`. Queries the durable `llm_usage_records`
    ledger rather than an in-process counter, so the budget is enforced
    across ALL phases/drives of a run (analysis, code, review, verification
    fix loops) — not just the current loop invocation's in-memory total,
    which resets every time `run_agent_loop` is re-entered. Best-effort:
    returns 0 (never raises) on any DB error so a ledger hiccup cannot
    itself abort a run — the caller should treat 0 as "unknown, do not
    enforce this cycle" rather than "definitely zero spend"."""
    try:
        from sqlalchemy import func, select
        from app.core.database import SessionLocal
        from app.models.agentic import LlmUsageRecord
        db = SessionLocal()
        try:
            total = db.scalar(
                select(
                    func.coalesce(func.sum(
                        LlmUsageRecord.input_tokens + LlmUsageRecord.output_tokens
                        + LlmUsageRecord.cache_read_tokens + LlmUsageRecord.cache_write_tokens
                    ), 0)
                ).where(LlmUsageRecord.run_id == run_id)
            )
            return int(total or 0)
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 — telemetry read must never break the loop
        logger.debug("cumulative run-token read skipped: %s", e)
        return 0


def _schedule_usage_persist(trace: "LlmCallTrace") -> None:
    """Run the (blocking) usage-ledger write OFF the event loop.

    `record_llm_call` runs inline on the asyncio loop at the tail of every
    `call_llm`/`stream_llm`; doing a synchronous SessionLocal()+commit() there
    stalls the loop under DB latency × hundreds of calls/run. When a loop is
    running we offload to its default executor; otherwise (sync/worker-thread
    callers such as the docgen bridge) we run inline — already off the loop.

    `copy_context()` is required so the executor thread sees `_usage_ctx`
    (change_request_id / run_id / section); a bare run_in_executor would lose it.
    """
    import asyncio
    import contextvars
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _persist_usage_row(trace)
        return
    ctx = contextvars.copy_context()
    # Fire-and-forget — _persist_usage_row swallows its own errors, so the
    # discarded Future never carries an unretrieved exception.
    loop.run_in_executor(None, ctx.run, _persist_usage_row, trace)


def record_llm_call(trace: LlmCallTrace) -> None:
    """Emit one trace as a structured INFO log line.

    Short-circuits when `settings.use_observability_traces` is False so
    flag-off has zero overhead beyond a settings read. Fail-open: any
    serialisation or logging error is swallowed.

    Sub-slice 28a — when `settings.use_langfuse` is also True AND the
    `langfuse` SDK is importable AND host/keys are configured, the
    trace is ALSO pushed as a Langfuse generation. Log emission still
    happens regardless so operators always have a fallback record.
    """
    _schedule_usage_persist(trace)   # usage ledger is independent of the trace-LOG flag below
    if not settings.use_observability_traces:
        return
    try:
        payload = json.dumps(trace.to_dict(), default=str)
        # Standard observability logger keeps emitting (back-compat).
        logger.info("llm_call %s", payload)
        # Slice 24+ — also tee into the dedicated rotating file. Lazy
        # init so module import doesn't touch the filesystem; the
        # first record_llm_call attaches the handler.
        _setup_llm_call_logger()
        _llm_logger.info(payload)
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("record_llm_call: emit failed: %s", e)
    # 28a — Langfuse forward (best-effort).
    # Phase 0.4 — gated by `langfuse_sample_rate`. Local INFO logs above
    # ALWAYS emit; only the upstream Langfuse push is sampled. Failure
    # error traces (success=False) bypass sampling so we never miss them.
    if settings.use_langfuse and _should_sample_for_langfuse(trace):
        try:
            _forward_to_langfuse(trace)
        except Exception as e:
            logger.debug("record_llm_call: langfuse forward failed: %s", e)


def _should_sample_for_langfuse(trace: "LlmCallTrace") -> bool:
    """Phase 0.4 — return True if this trace should be forwarded.

    Errors are never sampled out (sample_rate ignored when success=False).
    A `langfuse_sample_rate` >= 1.0 forwards everything; <= 0 forwards nothing.
    In between, a uniform random draw decides per call.
    """
    if not getattr(trace, "success", True):
        return True
    rate = float(getattr(settings, "langfuse_sample_rate", 1.0) or 0.0)
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    import secrets
    return secrets.randbelow(1_000_000) / 1_000_000 < rate


def _forward_to_langfuse(trace: LlmCallTrace) -> None:
    """Best-effort Langfuse generation push. Never raises out — caller
    swallows. Lazy-imports + caches the client so the absence of the
    `langfuse` package + creds is a clean no-op.
    """
    client = _get_langfuse_client()
    if client is None:
        return
    # Langfuse SDK API: client.generation(name, model, input, output, metadata, ...)
    # We pass a coarse `name` matching the agent + purpose so dashboards
    # can group by both. Field shape stays small to avoid surprise costs.
    try:
        client.generation(
            name=f"{trace.agent_name}.{trace.purpose}",
            model=trace.model,
            metadata={
                "provider":         trace.provider,
                "streaming":        trace.streaming,
                "prompt_chars":     trace.prompt_chars,
                "response_chars":   trace.response_chars,
                "response_chunks":  trace.response_chunks,
                "elapsed_ms":       trace.elapsed_ms,
                "success":          trace.success,
                "error_class":      trace.error_class,
            },
            level="ERROR" if not trace.success else "DEFAULT",
            status_message=trace.error_message,
        )
    except Exception as e:
        # SDK signature drift — log + drop. Keep observability resilient
        # to upstream API changes.
        logger.debug("langfuse generation() failed: %s", e)


_LANGFUSE_CLIENT: object | None = None
_LANGFUSE_CLIENT_FAILED: bool = False


def _get_langfuse_client():
    """Lazy-init the Langfuse client. Returns None when:
      - SDK package isn't installed
      - host / public_key / secret_key empty
      - prior init attempt failed (cached)
    Returns the client otherwise. Idempotent."""
    global _LANGFUSE_CLIENT, _LANGFUSE_CLIENT_FAILED
    if _LANGFUSE_CLIENT is not None:
        return _LANGFUSE_CLIENT
    if _LANGFUSE_CLIENT_FAILED:
        return None
    if not (settings.langfuse_host and settings.langfuse_public_key and settings.langfuse_secret_key):
        _LANGFUSE_CLIENT_FAILED = True
        return None
    try:
        from langfuse import Langfuse
        _LANGFUSE_CLIENT = Langfuse(
            host=settings.langfuse_host,
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
        )
        return _LANGFUSE_CLIENT
    except Exception as e:
        logger.warning("Langfuse client init failed (will use log-only): %s", e)
        _LANGFUSE_CLIENT_FAILED = True
        return None


def _reset_langfuse_client_for_tests() -> None:
    """Test hook — clear the cached client + failure flag so each test
    can independently exercise the lazy-init paths."""
    global _LANGFUSE_CLIENT, _LANGFUSE_CLIENT_FAILED
    _LANGFUSE_CLIENT = None
    _LANGFUSE_CLIENT_FAILED = False


def record_llm_call_kwargs(
    *,
    agent_name: str,
    provider: str,
    model: str,
    streaming: bool,
    prompt_chars: int,
    response_chars: int,
    response_chunks: int,
    elapsed_ms: int,
    success: bool,
    error: BaseException | None = None,
    extra: dict[str, Any] | None = None,
    job_id: str | None = None,
    # Slice 24+ — size/usage/cost (all optional; legacy callers
    # that don't thread these stay back-compat)
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    stop_reason: str | None = None,
) -> None:
    """Convenience: build + emit in one call.

    `purpose` is resolved automatically from `agent_name` via
    `agent_purpose_label`. Override by passing `extra={"purpose": "..."}`
    if a caller knows better than the static map.

    R-9 — `job_id` (optional) links this LLM call back to its parent
    `agent_jobs` row. When present, dashboards can group every LLM call
    that belongs to one user-visible job (e.g. all 14 section-writes of
    a single BRD docgen run). Threaded through `call_llm` /
    `stream_llm` signatures via a contextvar so individual call sites
    don't need to plumb it explicitly.
    """
    # S6 (ARCHITECTURE_REVIEW_ACTIONS.md — "validate outbound dependency
    # responses before use... applies to LLM providers"). Sanity-checks the
    # provider-reported usage BEFORE it drives cost accounting or (via the
    # agentic loop's cumulative_tokens/context-window anchor) compaction
    # decisions. A malformed/absurd value is logged as a security-relevant
    # "malformed dependency response" telemetry event and excluded from
    # cost math (falls through as None, same semantics as "no usage
    # reported") rather than silently poisoning a downstream budget/
    # compaction calculation.
    from app.core.outbound_validation import validate_llm_usage, log_validation_warnings
    _usage_check = validate_llm_usage({
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens, "cache_write_tokens": cache_write_tokens,
    })
    if not _usage_check.ok:
        logger.warning(
            "SECURITY_EVENT event=malformed_dependency_response severity=low "
            "context=llm_usage provider=%s model=%s reason=%s",
            provider, model, _usage_check.reason,
        )
        input_tokens = output_tokens = cache_read_tokens = cache_write_tokens = None
    else:
        log_validation_warnings(_usage_check, context=f"llm_usage:{provider}:{model}")

    # Compute cost only when we actually have provider-reported tokens.
    # `cost_usd=None` is the right semantics when tokens are unknown
    # (e.g. provider didn't return usage, or the model isn't priced).
    cost_usd: float | None = None
    if input_tokens is not None or output_tokens is not None:
        from app.core.llm_costs import compute_cost_usd
        cost_usd = compute_cost_usd(
            model or "",
            input_tokens=input_tokens or 0,
            output_tokens=output_tokens or 0,
            cache_read_tokens=cache_read_tokens or 0,
            cache_write_tokens=cache_write_tokens or 0,
        )

    trace = LlmCallTrace(
        agent_name=agent_name or "unknown",
        purpose=(extra or {}).pop("purpose", None) if extra else None,  # see below
        provider=provider or "unknown",
        model=model or "unknown",
        streaming=streaming,
        prompt_chars=int(prompt_chars or 0),
        response_chars=int(response_chars or 0),
        response_chunks=int(response_chunks or 0),
        elapsed_ms=int(elapsed_ms or 0),
        success=bool(success),
        error_class=type(error).__name__ if error is not None else None,
        error_message=str(error)[:500] if error is not None else None,
        job_id=job_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        cost_usd=cost_usd,
        stop_reason=stop_reason,
        extra=extra or {},
    )
    if not trace.purpose:
        trace.purpose = agent_purpose_label(agent_name)
    record_llm_call(trace)




# ──────────────────────────────────────────────────────────────────────────────
# Char counting (no tokeniser — chars are a stable proxy)
# ──────────────────────────────────────────────────────────────────────────────

def estimate_prompt_chars(system: str, messages: list[dict]) -> int:
    """Total chars across system + every message body. Cheap, deterministic,
    backend-agnostic. Operators wanting real token counts can post-process
    via the provider's tokeniser; the trace stores `prompt_chars` so the
    field name doesn't lie."""
    total = len(system or "")
    for m in messages or []:
        body = m.get("content") if isinstance(m, dict) else None
        if isinstance(body, str):
            total += len(body)
    return total
