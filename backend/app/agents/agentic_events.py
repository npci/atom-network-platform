# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Event persistence + coding activity log for the agentic state machine.

Two sinks, one call (THE BOOK §3 / §21):

* ``agentic_events`` — the durable, append-only **source of truth**. The
  WebSocket layer (S13) is a *subscriber* that replays/streams from this table;
  it never owns workflow state, so a dropped socket loses nothing.
* ``CODING_LOG_DIR/<run_id>.jsonl`` — a human-readable per-run mirror for
  audit / post-mortem / replay, secret-redacted and size-capped.

``emit_event`` is the single entry point both the state machine (this slice) and
later phase bodies (tool calls, commands, edits, gates — S6+) call.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.agentic import AgenticEvent
from app.models.base import utcnow

logger = logging.getLogger("app.agentic")

# Bounded backup so a single run's log can't grow without limit. Retention is
# finalised by the workspace GC slice (§6/§21); this is the per-file ceiling.
_MAX_LOG_BYTES = 20 * 1024 * 1024  # 20 MB, then one rollover to <file>.1


# ── Secret redaction (§21, defense-in-depth) ───────────────────────────────────
# The clean-env execution (§6) already keeps most secrets out of child
# processes; this covers a misconfigured build that *echoes* a token to stdout.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # GitLab private tokens / bearer headers. Whole header value (`.+`, not
    # `\S+`): `Authorization: Bearer <jwt>` otherwise only loses "Bearer" and
    # leaks the token after it.
    (re.compile(r"(?i)\b(private-token|authorization)\s*[:=]\s*.+"), r"\1: [REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"), "bearer [REDACTED]"),
    # KEY=value / *_API_KEY / *_SECRET / *_TOKEN / *_PASSWORD env-style lines.
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD))\s*=\s*\S+"), r"\1=[REDACTED]"),
    # Credentials embedded in a URL (scheme://user:pass@host).
    #
    # These two are a MIRROR of `app.core.log_buffer._REDACTIONS` — keep them in
    # sync. This copy previously used `[^:/\s]+` (a plus) for the userinfo, which
    # does not match the empty-username form `scheme://:pass@host` and so leaked
    # the password verbatim. That is the exact shape this project deploys: all
    # three REDIS_URL values in docker-compose.yml are
    # `redis://:${REDIS_PASSWORD}@redis:6379/0`. log_buffer was fixed to a star;
    # this copy was not, and it is the only scrubber on AgenticEvent.payload,
    # which api/agentic.py returns to clients.
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^:/\s]*):[^@/\s]+@"), r"\1:[REDACTED]@"),
    # Same credentials in a URL with NO `//` after the scheme.
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*:/?)([^:/\s@]*):[^@/\s]+@"), r"\1\2:[REDACTED]@"),
    # SSH private keys (OpenSSH / PEM).
    (re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"), "[REDACTED SSH KEY]"),
    # AWS access key IDs (AKIA...).
    (re.compile(r"\b(AKIA[0-9A-Z]{16})\b"), "[REDACTED AWS KEY]"),
    # PEM-encoded certificates (defense-in-depth — usually not secret, but
    # may embed private keys in combined bundles).
    (re.compile(r"-----BEGIN CERTIFICATE-----[\s\S]*?-----END CERTIFICATE-----"), "[REDACTED CERTIFICATE]"),
)


def redact(text: str) -> str:
    """Mask common secret shapes in free text before it is persisted."""
    if not text:
        return text
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def _redact_obj(obj: Any) -> Any:
    """Recursively redact strings inside a JSON-able payload."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: _redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(v) for v in obj]
    return obj


# ── Per-run JSONL mirror ────────────────────────────────────────────────────────

_resolved_log_dir: Path | None = None


def _resolve_coding_log_dir() -> Path | None:
    """Pick the first WRITABLE coding-log dir, trying in order:
      1. settings.coding_log_dir (operator-configured, CODING_LOG_DIR)
      2. the running user's home (~/.a2a/coding_logs) — writable per-user even
         when the configured dir (e.g. /app/logs/coding) is read-only in the
         container. This is the "log in the running user's own folder" fallback.
      3. the OS temp dir (last resort; survives until reboot)
    Resolution is cached; returns None only if even /tmp is unwritable (then the
    caller skips logging — the durable agentic_events row remains the truth)."""
    global _resolved_log_dir
    if _resolved_log_dir is not None:
        return _resolved_log_dir
    import tempfile
    candidates = [
        settings.coding_log_dir,
        str(Path.home() / ".a2a" / "coding_logs"),
        str(Path(tempfile.gettempdir()) / "a2a_coding_logs"),
    ]
    for cand in candidates:
        if not cand:
            continue
        try:
            d = Path(cand)
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write_test"           # prove it's actually writable, not just present
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError:
            continue
        _resolved_log_dir = d
        if cand != settings.coding_log_dir:
            logger.warning("coding-log dir %r not writable; logging to %s instead",
                           settings.coding_log_dir, d)
        return d
    return None


def _write_coding_log(run_id: str, record: dict) -> None:
    """Append one redacted JSON line to ``<coding-log dir>/<run_id>.jsonl``.

    Best-effort: a log-write failure must never break the run, so all errors
    are swallowed with a warning (the durable ``agentic_events`` row is the
    real source of truth). The dir is auto-resolved to the first writable
    location (see ``_resolve_coding_log_dir``). One size-based rollover keeps a
    single file bounded.
    """
    try:
        log_dir = _resolve_coding_log_dir()
        if log_dir is None:
            return
        path = log_dir / f"{run_id}.jsonl"
        if path.exists() and path.stat().st_size >= _MAX_LOG_BYTES:
            path.replace(path.with_suffix(".jsonl.1"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError as exc:
        logger.warning("coding-log write failed for run=%s: %s", run_id, exc)


# ── The single emit entry point ──────────────────────────────────────────────────

def emit_event(
    db: Session,
    run_id: str,
    kind: str,
    payload: dict | None = None,
    *,
    flush: bool = True,
) -> AgenticEvent:
    """Append an event to ``agentic_events`` and mirror it to the coding log.

    ``seq`` is a per-run monotonic counter computed as ``max(seq)+1`` under the
    caller's transaction. The append-only ``(run_id, seq)`` uniqueness
    (migration M-D) guards against a concurrent double-append: with the run
    lease (§3) only one worker drives a run at a time, so contention is not
    expected, but the unique constraint makes a violation loud rather than
    silently re-using a seq.

    NOTE — serialization is LOCK-FREE by design. The run lease (§3) guarantees a
    single worker drives a run at a time, so concurrent appends to the same run are
    not expected; the ``(run_id, seq)`` unique constraint (migration M-D) is the
    backstop that makes a genuine double-drive LOUD (one appender hits an
    IntegrityError) instead of silently re-using a seq. The flush is wrapped in a
    SAVEPOINT (``begin_nested``) so a benign single-collision is absorbed (re-read +
    retry once) WITHOUT aborting the caller's whole transaction, and a persistent
    collision raises a clear "concurrent driver" error rather than a cryptic
    ``IntegrityError``/``PendingRollbackError`` cascade — see the append below.

    Do NOT add ``.with_for_update()`` here — it breaks prod two ways:
    1. On the ``max(seq)`` aggregate Postgres rejects it outright
       ("FeatureNotSupported: FOR UPDATE is not allowed with aggregate functions"),
       500-ing the first event of every run.
    2. As a row lock on agentic_runs it is held for the WHOLE phase — emit_event
       only ``flush``es; the orchestrator commits at phase boundaries — so it blocks
       the heartbeat thread's ``last_heartbeat_at``/``renew_lease`` UPDATE on that
       same row. The lease then lapses mid-build and the recovery beat double-drives
       the run on the same workspace (the exact failure the heartbeat prevents).
    """
    payload = _redact_obj(payload or {})

    def _alloc_and_add() -> "AgenticEvent":
        next_seq = (
            db.query(func.coalesce(func.max(AgenticEvent.seq), -1))
            .filter(AgenticEvent.run_id == run_id)
            .scalar()
        ) + 1
        ev = AgenticEvent(run_id=run_id, seq=next_seq, kind=kind, payload=payload)
        db.add(ev)
        return ev

    # Savepoint-wrapped append (§3, prod hardening): a genuine double-drive collides on the
    # (run_id, seq) unique constraint. Without the savepoint the IntegrityError aborts the
    # CALLER's whole transaction — every later statement then raises PendingRollbackError until
    # someone rolls back, so the collision (a benign, expected signal) cascades into unrelated
    # failures (the crash the recovery beat then re-drives). begin_nested() isolates the flush:
    # on collision we roll back ONLY the savepoint, re-read max(seq) (the other driver advanced
    # it), and retry ONCE. The outer transaction stays clean either way. A second collision means
    # a live concurrent driver — raise a CLEAR error (the zombie driver aborts on it, as designed)
    # rather than a cryptic IntegrityError. flush=False callers (rare) keep the lock-free path.
    if not flush or not hasattr(db, "begin_nested"):
        event = _alloc_and_add()
        if flush:
            db.flush()
    else:
        from sqlalchemy.exc import IntegrityError
        event = None
        for attempt in (1, 2):
            ev = None
            try:
                with db.begin_nested():          # SAVEPOINT — rolled back alone on failure
                    ev = _alloc_and_add()
                    db.flush()                    # surface the (run_id, seq) clash HERE
                event = ev
                break
            except IntegrityError as ie:
                # The savepoint rollback restores `ev` to session.new — expunge it so the
                # retry's fresh add doesn't leave a duplicate pending for the outer commit.
                if ev is not None:
                    try:
                        db.expunge(ev)
                    except Exception:            # noqa: BLE001 — already detached
                        pass
                if attempt == 2:
                    raise RuntimeError(
                        f"agentic_events: seq collision persisted for run={run_id} after retry — "
                        "a second worker is driving this run concurrently (double-drive). This "
                        "driver is superseded and must stop."
                    ) from ie
                logger.warning("emit_event: (run_id, seq) collision on run=%s — a concurrent "
                               "appender advanced the seq; re-reading and retrying once", run_id)

    _write_coding_log(
        run_id,
        {"ts": utcnow().isoformat(), "run_id": run_id, "seq": event.seq, "kind": kind, "payload": payload},
    )
    return event
