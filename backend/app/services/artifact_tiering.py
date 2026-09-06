# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Non-destructive data tiering — compress old artifacts, then flag for archive.

Closes architecture review Finding #8 ("No Data Tiering for Large
Payloads"). Design rationale and the full data-classification table live
in `docs/ARCHITECTURE_REVIEW_REMEDIATION.md` §7 and
`docs/SECURITY_ARCHITECTURE.md` §8.

SAFETY MODEL — read this before changing anything in this file
-----------------------------------------------------------------
This module NEVER modifies or deletes a source row's own columns.
`tech_specs.content`, `brds.content`, and `a2a_messages.payload`/
`response_body` are read-only inputs here. Every existing code path that
reads those columns directly (24+ sites for TechSpec/BRD content, 43+ for
A2AMessage.payload, per this repo's own audit) keeps working completely
unchanged, whether or not a row has been tiered. Tiering only:

1. Writes a GZIP-compressed COPY of the content to a file under
   `settings.artifact_coldstore_dir` (which lives on the SAME `./artifacts`
   volume every service already mounts — no new infrastructure to
   provision, addressing the operator's "put it somewhere in the
   workspace first" request directly).
2. Records one `ArtifactColdStorage` manifest row pointing at that file.
3. Later (a separate, independent step), flips `ready_for_archive=True`
   on manifest rows old enough to be moved off-platform — a SIGNAL for a
   human/ops process to act on, never an automatic move or deletion.

Eligibility is deliberately conservative: only artifacts belonging to a
change request in a TERMINAL status (default: "completed") are ever
touched, and only once older than `artifact_coldstore_after_days`. An
in-flight change's documents are never compressed, regardless of age,
because they can still be legitimately re-read/re-edited as part of an
active workflow.

This is disabled by default (`artifact_tiering_enabled=False`) — an
operator opts in explicitly after reviewing the disk-space trade-off
(cold storage still lives on local disk; it trades DB table size for
filesystem usage, not for external storage cost, until the archive-move
step is executed by ops).
"""
from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.base import generate_uuid, utcnow

logger = logging.getLogger(__name__)


def coldstore_root() -> Path:
    """Root directory for compressed artifact copies. Lives on the same
    volume as `settings.artifacts_dir` (the `./artifacts` bind mount every
    service already has) — deliberately NOT the agentic workspace volume,
    since workspace clones are ephemeral/GC'd on a much shorter cycle and
    this data should outlive any single agentic run."""
    return Path(settings.artifact_coldstore_dir)


def _write_compressed(path: Path, content: str) -> tuple[int, int]:
    """Write `content` gzip-compressed to `path`, creating parent dirs.
    Returns (original_size_bytes, compressed_size_bytes). Raises on
    failure — callers should catch and log rather than let one bad row
    abort a whole sweep."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = content.encode("utf-8", errors="ignore")
    with gzip.open(path, "wb", compresslevel=6) as f:
        f.write(raw)
    return len(raw), path.stat().st_size


def _is_change_terminal(db: Session, change_request_id: str | None) -> bool:
    """True iff the owning ChangeRequest is in the configured terminal
    status (default 'completed'). None/missing change_request_id is
    treated as NOT terminal (fail-safe — never tier something we can't
    positively confirm is safe to tier)."""
    if not change_request_id:
        return False
    try:
        from app.models.change_request import ChangeRequest
        cr = db.get(ChangeRequest, change_request_id)
        if cr is None:
            return False
        target = (getattr(settings, "artifact_tiering_min_change_status", "completed") or "completed")
        return str(getattr(cr.status, "value", cr.status)) == target
    except Exception as e:  # noqa: BLE001 — fail-safe: uncertain -> not eligible
        logger.debug("artifact_tiering: terminal-status check failed for change=%s: %s",
                    change_request_id, e)
        return False


def _already_tiered(db: Session, source_table: str, source_id: str) -> bool:
    from app.models.artifact_cold_storage import ArtifactColdStorage
    return db.query(ArtifactColdStorage).filter(
        ArtifactColdStorage.source_table == source_table,
        ArtifactColdStorage.source_id == source_id,
    ).first() is not None


def _record_manifest(db: Session, *, source_table: str, source_id: str,
                     change_request_id: str | None, rel_path: str,
                     original_size: int, compressed_size: int) -> None:
    from app.models.artifact_cold_storage import ArtifactColdStorage
    db.add(ArtifactColdStorage(
        id=generate_uuid(),
        source_table=source_table,
        source_id=source_id,
        change_request_id=change_request_id,
        coldstore_path=rel_path,
        original_size_bytes=original_size,
        compressed_size_bytes=compressed_size,
        compressed_at=utcnow(),
        ready_for_archive=False,
    ))


def compress_old_tech_specs(db: Session) -> dict:
    """Compress TechSpec.content for rows older than
    artifact_coldstore_after_days, belonging to a terminal change,
    not already tiered. Returns a summary dict. Never raises — a
    per-row failure is logged and skipped so one bad row cannot abort
    the whole batch."""
    return _compress_text_column_rows(
        db, table="tech_specs", model_import=("app.models.tech_spec", "TechSpec"),
        content_attr="content", subdir="tech_specs",
    )


def compress_old_brds(db: Session) -> dict:
    """Same as `compress_old_tech_specs`, for BRD.content."""
    return _compress_text_column_rows(
        db, table="brds", model_import=("app.models.brd", "BRD"),
        content_attr="content", subdir="brds",
    )


def compress_old_a2a_messages(db: Session) -> dict:
    """Compress A2AMessage.payload + response_body (JSON columns) for
    rows older than artifact_coldstore_after_days. Unlike TSD/BRD,
    A2AMessage doesn't have a single "is this change done" gate as clean
    as ChangeStatus — eligibility here uses `created_at` age alone plus
    a terminal message status (delivered or delivery_failed with no
    pending retry), since a message mid-retry must stay fully readable
    for the retry sweeper and the admin resend UI."""
    from datetime import timedelta
    from app.models.phase_c import A2AMessage

    days = int(getattr(settings, "artifact_coldstore_after_days", 30) or 30)
    cutoff = utcnow() - timedelta(days=days)
    batch = int(getattr(settings, "artifact_tiering_batch_size", 200) or 200)

    candidates = (
        db.query(A2AMessage)
        .filter(A2AMessage.created_at < cutoff)
        .filter(A2AMessage.status.in_(("delivered", "delivery_failed")))
        .filter(A2AMessage.next_retry_at.is_(None))  # no pending retry — safe to tier
        .limit(batch)
        .all()
    )
    compressed = 0
    skipped = 0
    errors = 0
    for row in candidates:
        try:
            if _already_tiered(db, "a2a_messages", row.id):
                skipped += 1
                continue
            body = {"payload": row.payload, "response_body": row.response_body}
            content = json.dumps(body, default=str)
            if not content or content in ("null", "{}"):
                skipped += 1
                continue
            rel_path = f"a2a_messages/{row.created_at.strftime('%Y/%m')}/{row.id}.json.gz"
            abs_path = coldstore_root() / rel_path
            orig_size, comp_size = _write_compressed(abs_path, content)
            _record_manifest(db, source_table="a2a_messages", source_id=row.id,
                             change_request_id=row.change_request_id,
                             rel_path=rel_path, original_size=orig_size,
                             compressed_size=comp_size)
            compressed += 1
        except Exception as e:  # noqa: BLE001 — one bad row must not abort the batch
            errors += 1
            logger.warning("artifact_tiering: failed to compress a2a_messages/%s: %s", row.id, e)
    db.commit()
    return {"table": "a2a_messages", "compressed": compressed, "skipped": skipped, "errors": errors}


def _compress_text_column_rows(db: Session, *, table: str, model_import: tuple[str, str],
                               content_attr: str, subdir: str) -> dict:
    from datetime import timedelta
    mod_name, cls_name = model_import
    import importlib
    Model = getattr(importlib.import_module(mod_name), cls_name)

    days = int(getattr(settings, "artifact_coldstore_after_days", 30) or 30)
    cutoff = utcnow() - timedelta(days=days)
    batch = int(getattr(settings, "artifact_tiering_batch_size", 200) or 200)

    candidates = (
        db.query(Model)
        .filter(Model.created_at < cutoff)
        .limit(batch)
        .all()
    )
    compressed = 0
    skipped = 0
    errors = 0
    for row in candidates:
        try:
            content = getattr(row, content_attr, None)
            if not content:
                skipped += 1
                continue
            if _already_tiered(db, table, row.id):
                skipped += 1
                continue
            change_id = getattr(row, "change_request_id", None)
            if not _is_change_terminal(db, change_id):
                skipped += 1
                continue
            rel_path = f"{subdir}/{row.created_at.strftime('%Y/%m')}/{row.id}.txt.gz"
            abs_path = coldstore_root() / rel_path
            orig_size, comp_size = _write_compressed(abs_path, content)
            _record_manifest(db, source_table=table, source_id=row.id,
                             change_request_id=change_id, rel_path=rel_path,
                             original_size=orig_size, compressed_size=comp_size)
            compressed += 1
        except Exception as e:  # noqa: BLE001 — one bad row must not abort the batch
            errors += 1
            logger.warning("artifact_tiering: failed to compress %s/%s: %s", table, row.id, e)
    db.commit()
    return {"table": table, "compressed": compressed, "skipped": skipped, "errors": errors}


def mark_ready_for_archive(db: Session) -> dict:
    """Stage 2 — flip `ready_for_archive=True` on coldstore manifest rows
    older than `artifact_archive_after_days`. This ONLY sets a flag; it
    does not move, copy, or delete anything. An operator/ops process
    (outside this codebase, per the phased plan in
    docs/ARCHITECTURE_REVIEW_REMEDIATION.md §7) should periodically query
    `WHERE ready_for_archive = true AND archived_at IS NULL`, move each
    file at `coldstore_path` to real archive storage (S3 Glacier, tape,
    etc.), and then call `mark_archived()` below to record completion."""
    from datetime import timedelta
    from app.models.artifact_cold_storage import ArtifactColdStorage

    days = int(getattr(settings, "artifact_archive_after_days", 90) or 90)
    cutoff = utcnow() - timedelta(days=days)
    rows = (
        db.query(ArtifactColdStorage)
        .filter(ArtifactColdStorage.compressed_at < cutoff)
        .filter(ArtifactColdStorage.ready_for_archive.is_(False))
        .all()
    )
    for row in rows:
        row.ready_for_archive = True
    db.commit()
    return {"flagged_for_archive": len(rows)}


def mark_archived(db: Session, manifest_id: str) -> bool:
    """Called by an external ops process AFTER it has actually moved
    `coldstore_path` to real archive storage, to record completion. This
    function itself performs no file movement — it only updates the
    manifest row. Returns False if the manifest row doesn't exist."""
    from app.models.artifact_cold_storage import ArtifactColdStorage
    row = db.get(ArtifactColdStorage, manifest_id)
    if row is None:
        return False
    row.archived_at = utcnow()
    db.commit()
    return True


# ── Stage 1b — reclaim DB space by nulling verified-compressed columns ───────
#
# This is the half of Finding #8 that was previously left open (see
# ARCHITECTURE_REVIEW_ACTIONS_CLOSURE.md C4: "the original content/payload
# columns are never nulled after compression — the DATABASE SIZE problem is not
# yet reduced, only a redundant compressed copy now also exists").
#
# It is safe ONLY because `services/artifact_coldstore_read.py` registers a
# read-through fallback that transparently rehydrates a nulled column from its
# cold copy, so all 114 audited read sites keep returning content. Do not enable
# `artifact_coldstore_null_source` without that hook registered — hence the
# explicit guard in `null_verified_source_columns` below.

_NULLABLE_TARGETS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    # source_table: (module, class, attributes to null)
    "tech_specs":   ("app.models.tech_spec", "TechSpec", ("content",)),
    "brds":         ("app.models.brd", "BRD", ("content",)),
    "a2a_messages": ("app.models.phase_c", "A2AMessage", ("payload", "response_body")),
}


def _sql_null_for(Model, attribute: str):
    """The right "empty" value to assign so the column becomes a real SQL NULL.

    For a plain text column, Python `None` is correct. For a SQLAlchemy `JSON`
    column it is NOT: assigning `None` serialises to the JSON *string* `'null'`
    (verified — `typeof(payload)` reports `text`, and the row still occupies
    storage). That would make this whole feature a no-op for
    `a2a_messages.payload`/`response_body` — the two largest tiering targets —
    while still reporting success. `sqlalchemy.null()` emits SQL NULL, which is
    what actually frees the space and what the read-through hook's `is None`
    check looks for.
    """
    from sqlalchemy import JSON, null
    try:
        col = Model.__table__.c[attribute]
    except (AttributeError, KeyError):
        return None
    return null() if isinstance(col.type, JSON) else None


def _verify_coldstore_copy(source_table: str, source_id: str, rel_path: str,
                           expected: dict) -> bool:
    """Re-read the cold copy and confirm it round-trips to the SAME content
    currently in the DB, BEFORE that content is destroyed.

    This is the whole safety argument for nulling. A manifest row proving "we
    wrote a file once" is not sufficient evidence to delete the original — the
    file may have been truncated, corrupted, or already archived away by an ops
    process. So the bytes are read back and compared field by field, and nulling
    only proceeds on an exact match.
    """
    import gzip
    try:
        with gzip.open(coldstore_root() / rel_path, "rb") as f:
            raw = f.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        logger.warning("artifact_tiering: cannot verify cold copy %s (%s) — not nulling",
                       rel_path, exc)
        return False

    if source_table in ("tech_specs", "brds"):
        return raw == (expected.get("content") or "")

    # a2a_messages — the compressor stored a JSON envelope of both columns.
    try:
        stored = json.loads(raw) or {}
    except ValueError:
        logger.warning("artifact_tiering: cold copy %s is not valid JSON — not nulling", rel_path)
        return False
    # Compare through the SAME json round-trip the compressor used, so a
    # non-string-keyed dict or a datetime that `default=str` stringified does not
    # register as a spurious mismatch and block nulling forever.
    for attr in ("payload", "response_body"):
        want = json.loads(json.dumps(expected.get(attr), default=str))
        if stored.get(attr) != want:
            logger.warning(
                "artifact_tiering: cold copy %s does not match current %s.%s — not nulling",
                rel_path, source_table, attr)
            return False
    return True


def null_verified_source_columns(db: Session) -> dict:
    """Null the source columns of rows whose cold copy verifies byte-identical.

    This is what actually reduces database size — the goal of Finding #8. Gated
    on `artifact_coldstore_null_source` (default False) AND on the read-through
    hook being registered, because nulling without the fallback would make
    tiered content unreadable to all 114 call sites.

    Returns a summary dict. Never raises; a per-row problem is logged and that
    row is left untouched (content intact) rather than risking data loss.
    """
    if not getattr(settings, "artifact_coldstore_null_source", False):
        return {"enabled": False, "nulled": 0}

    from app.services.artifact_coldstore_read import register_coldstore_read_through
    if not register_coldstore_read_through():
        # Refuse rather than proceed: without rehydration this would turn a
        # space optimisation into silent data loss for every reader.
        logger.error(
            "artifact_tiering: refusing to null source columns — the coldstore "
            "read-through hook is not registered, so nulled content would be "
            "unreadable. See services/artifact_coldstore_read.py.")
        return {"enabled": True, "nulled": 0, "refused": "read_through_unavailable"}

    import importlib

    from app.models.artifact_cold_storage import ArtifactColdStorage

    batch = int(getattr(settings, "artifact_tiering_batch_size", 200) or 200)
    nulled = 0
    verified_failed = 0
    skipped = 0

    manifests = (
        db.query(ArtifactColdStorage)
        .filter(ArtifactColdStorage.source_table.in_(tuple(_NULLABLE_TARGETS)))
        .limit(batch)
        .all()
    )
    for man in manifests:
        try:
            mod_name, cls_name, attrs = _NULLABLE_TARGETS[man.source_table]
            Model = getattr(importlib.import_module(mod_name), cls_name)
            row = db.get(Model, man.source_id)
            if row is None:
                skipped += 1
                continue
            # A row that is ALREADY nulled has been rehydrated by the
            # read-through hook, so BOTH `getattr` and the instance's state dict
            # report content — neither can tell "still in the DB" from "already
            # in cold storage". Ask the hook which attributes it filled in.
            # Without this the sweep re-nulls the same rows on every pass.
            from app.services.artifact_coldstore_read import is_rehydrated
            from sqlalchemy import inspect as _sa_inspect
            persisted = _sa_inspect(row).dict
            if all(is_rehydrated(row, a) or persisted.get(a) is None for a in attrs):
                skipped += 1   # already reclaimed
                continue
            expected = {a: persisted.get(a) for a in attrs}
            if not _verify_coldstore_copy(man.source_table, man.source_id,
                                          man.coldstore_path, expected):
                verified_failed += 1
                continue
            for a in attrs:
                setattr(row, a, _sql_null_for(Model, a))
            nulled += 1
        except Exception as exc:  # noqa: BLE001 — never let one row abort the sweep
            verified_failed += 1
            logger.warning("artifact_tiering: nulling failed for %s/%s: %s",
                           man.source_table, man.source_id, exc)
    db.commit()
    result = {"enabled": True, "nulled": nulled,
              "verify_failed": verified_failed, "skipped": skipped}
    if nulled:
        logger.info("artifact_tiering: reclaimed DB space for %d row(s): %s", nulled, result)
    return result


def run_tiering_sweep(db: Session) -> dict:
    """Entry point for the Celery periodic task. Runs Stage 1 (compress) across
    all three tiering targets, Stage 1b (null verified-compressed columns — the
    step that actually reduces DB size), then Stage 2 (flag for archive).

    No-ops entirely (returns {"enabled": False}) unless
    settings.artifact_tiering_enabled is True — this is an opt-in feature, not
    something that starts touching data the moment the code ships. Stage 1b has
    its OWN additional flag (`artifact_coldstore_null_source`, default False) so
    an operator can run compression for a while and inspect the cold copies
    before authorising anything destructive."""
    if not getattr(settings, "artifact_tiering_enabled", False):
        return {"enabled": False}
    results = {
        "enabled": True,
        "tech_specs": compress_old_tech_specs(db),
        "brds": compress_old_brds(db),
        "a2a_messages": compress_old_a2a_messages(db),
        # Ordered AFTER the compress stages on purpose: a row compressed in this
        # same sweep is then immediately eligible for nulling, but only via the
        # verify step above, never on the strength of "we just wrote it".
        "null_source": null_verified_source_columns(db),
        "archive_flagging": mark_ready_for_archive(db),
    }
    logger.info("artifact_tiering sweep complete: %s", results)
    return results
