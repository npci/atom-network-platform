# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Read-through cold-storage rehydration — completes architecture Finding #8.

Finding #8 ("No Data Tiering for Large Payloads") was previously closed only
half-way, and `ARCHITECTURE_REVIEW_ACTIONS_CLOSURE.md` C4 says so plainly: the
compression stage writes a gzip COPY to disk and records a manifest row, but
never nulls the source column. So the database is no smaller — it now holds the
original content AND a compressed duplicate exists on disk. The finding's actual
goal, reducing DB size, was not met.

Closing it requires nulling `tech_specs.content` / `brds.content` /
`a2a_messages.payload` / `.response_body` after a verified compress. That is only
safe if every read of those columns transparently falls back to the coldstore
copy. This module provides that fallback.

Why an ORM load event, and not a property or 114 edits
------------------------------------------------------
An audit of this repository found:

  * 30 read sites for `TechSpec.content`
  * 25 for `BRD.content`
  * 59 for `A2AMessage.payload`

...114 in total. Editing each one would be the "materially larger and riskier
change" C4 warned about, and would silently miss any site added later.

Three mechanisms were considered:

1. **A Python `@property`** (rename the column, expose a property). Rejected: it
   breaks column-level SQL. `app/api/phase_c.py::cert_txns` does
   `select(A2AMessage.payload).where(...)` — a property is not a SQL expression,
   so that endpoint would raise. Verified by audit; this is a real call site, not
   a hypothetical.
2. **A `TypeDecorator`** (like `core/encrypted_type.py`). Rejected: a
   TypeDecorator's `process_result_value` receives only the column value, not the
   owning row. It cannot know the row's primary key, so it cannot find the
   manifest that says where the coldstore file is.
3. **A SQLAlchemy `'load'` event hook** — chosen. It fires after a row is
   populated into an object, has the full instance (so the PK is available), and
   sets the attribute in place. Every one of the 114 attribute reads then sees
   real content with no call-site change, because they are all just reading an
   attribute off a loaded ORM object.

The one thing an ORM event cannot cover is the column-level `select()` in
`cert_txns`, which never builds an ORM object. That is handled explicitly and
narrowly at that call site rather than papered over — see `rehydrate_payload_dict`.

Cost model
----------
The hook is a no-op for every row whose column is non-NULL, which is every row
until an operator enables nulling. The manifest lookup only happens when the
column IS NULL — i.e. only for rows that were actually tiered. There is no
per-row query on the hot path for untiered data.

Safety
------
Fails OPEN: if the coldstore file is missing or corrupt, the attribute is left as
NULL and the failure is logged loudly. A rehydration error must never raise out
of an ORM load, which would break unrelated queries in ways very hard to trace.
"""
from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# (source_table, {attribute: kind}) for every column this module can rehydrate.
# `kind` selects how the stored bytes map back onto the attribute:
#   "text"      — the file holds the column's text verbatim
#   "json_key"  — the file holds a JSON object; this attribute is one of its keys
_REHYDRATABLE: dict[str, dict[str, str]] = {
    "tech_specs":   {"content": "text"},
    "brds":         {"content": "text"},
    "a2a_messages": {"payload": "json_key", "response_body": "json_key"},
}


# Instance attribute recording which columns on a loaded object hold REHYDRATED
# (cold-storage) content rather than persisted content. Needed because the hook
# writes into `inspect(obj).dict`, which otherwise makes a rehydrated row look
# byte-for-byte like a row whose column was never nulled.
_REHYDRATED_MARKER = "_coldstore_rehydrated_attrs"


def is_rehydrated(obj, attribute: str) -> bool:
    """True if `attribute` on this loaded instance came from cold storage rather
    than from the database column.

    The tiering sweep uses this to skip rows it has already reclaimed. Any other
    caller that needs to distinguish "content is in the DB" from "content is in
    cold storage" should use this rather than inspecting instance state.
    """
    return attribute in (getattr(obj, _REHYDRATED_MARKER, ()) or ())


def _coldstore_root() -> Path:
    from app.core.config import settings
    return Path(settings.artifact_coldstore_dir)


def _read_coldstore_file(rel_path: str) -> bytes | None:
    path = _coldstore_root() / rel_path
    try:
        with gzip.open(path, "rb") as f:
            return f.read()
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        logger.error(
            "coldstore rehydrate: cannot read %s (%s) — the source column was "
            "nulled but its cold copy is unreadable; content is UNAVAILABLE",
            rel_path, exc,
        )
        return None


def _manifest_path_for(db, source_table: str, source_id: str) -> str | None:
    from app.models.artifact_cold_storage import ArtifactColdStorage
    row = (
        db.query(ArtifactColdStorage)
        .filter(ArtifactColdStorage.source_table == source_table,
                ArtifactColdStorage.source_id == source_id)
        .first()
    )
    return row.coldstore_path if row is not None else None


def fetch_from_coldstore(db, source_table: str, source_id: str,
                         attribute: str) -> object | None:
    """Return one tiered attribute's value from cold storage, or None.

    `None` means "unavailable" — either not tiered, or the cold copy could not be
    read. Callers must treat it exactly as they treat a NULL column today.
    """
    kinds = _REHYDRATABLE.get(source_table)
    if not kinds or attribute not in kinds:
        return None
    rel_path = _manifest_path_for(db, source_table, source_id)
    if not rel_path:
        return None
    raw = _read_coldstore_file(rel_path)
    if raw is None:
        return None
    if kinds[attribute] == "text":
        return raw.decode("utf-8", errors="replace")
    # json_key — the compressor wrote {"payload": ..., "response_body": ...}
    try:
        return (json.loads(raw.decode("utf-8", errors="replace")) or {}).get(attribute)
    except (ValueError, AttributeError) as exc:
        logger.error("coldstore rehydrate: %s/%s is not valid JSON (%s)",
                     source_table, source_id, exc)
        return None


def rehydrate_payload_dict(db, message_id: str, payload) -> object:
    """Explicit rehydration for the ONE place that reads a tiered column via a
    column-level `select()` rather than through the ORM
    (`app/api/phase_c.py::cert_txns`).

    An ORM `'load'` event cannot help there: no instance is ever constructed, so
    there is nothing to attach rehydrated state to. Rather than leave a silent
    hole, that call site calls this helper. Passing a non-NULL `payload` returns
    it untouched, so this is safe to call unconditionally.

    A `str` payload is decoded to a dict. A column-level `select()` on a `JSON`
    column does not always apply the type's result processor (and a raw
    `text()` query never does), so the caller can receive the raw JSON text
    rather than a dict — which then fails on `.get()`. Normalising here keeps
    that detail out of the call site.
    """
    if payload is None:
        payload = fetch_from_coldstore(db, "a2a_messages", message_id, "payload")
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return None
    return payload


# ── ORM load-event registration ──────────────────────────────────────────────

_registered = False


def _make_listener(source_table: str, attributes: dict[str, str]):
    def _on_load(target, _context):
        # Only pay any cost when a rehydratable column is actually NULL. For
        # untiered rows (all rows, until an operator enables nulling) this is a
        # couple of getattr calls and nothing else — no query, no file IO.
        missing = [a for a in attributes if getattr(target, a, None) is None]
        if not missing:
            return
        try:
            from app.core.config import settings
            if not getattr(settings, "artifact_coldstore_read_through", True):
                return
            from sqlalchemy import inspect as _sa_inspect
            state = _sa_inspect(target)
            db = state.session
            if db is None:
                return  # detached instance — no session to query the manifest with
            source_id = getattr(target, "id", None)
            if not source_id:
                return
            rel_path = _manifest_path_for(db, source_table, source_id)
            if not rel_path:
                return  # not tiered — a genuinely empty column
            raw = _read_coldstore_file(rel_path)
            if raw is None:
                return
            text = raw.decode("utf-8", errors="replace")
            parsed = None
            rehydrated: list[str] = []
            for attr in missing:
                if attributes[attr] == "text":
                    value = text
                else:
                    if parsed is None:
                        try:
                            parsed = json.loads(text) or {}
                        except ValueError:
                            logger.error(
                                "coldstore rehydrate: %s/%s is not valid JSON",
                                source_table, source_id)
                            return
                    value = parsed.get(attr)
                if value is None:
                    continue
                # Assign without marking the attribute dirty: this is a READ
                # path, and a flush must never write rehydrated content back
                # into the column the tiering job just nulled (that would
                # silently undo the space saving, and could resurrect content
                # for a row an operator intended to keep only in cold storage).
                state.dict[attr] = value
                rehydrated.append(attr)
            if rehydrated:
                # Record WHICH attributes are rehydrated rather than persisted.
                # Without this marker, `inspect(obj).dict` is indistinguishable
                # from a row whose column still holds content — which made the
                # nulling sweep treat already-reclaimed rows as still needing
                # work, re-nulling them on every pass (caught by
                # test_nulling_is_idempotent). `is_rehydrated()` below is the
                # supported way to ask.
                setattr(target, _REHYDRATED_MARKER, tuple(rehydrated))
            logger.debug("coldstore rehydrate: %s/%s restored %s",
                         source_table, source_id, rehydrated)
        except Exception:  # noqa: BLE001 — must never raise out of an ORM load
            logger.exception(
                "coldstore rehydrate failed for %s/%s — leaving column NULL",
                source_table, getattr(target, "id", "?"))

    return _on_load


def register_coldstore_read_through() -> bool:
    """Attach the `'load'` listeners. Idempotent; safe to call at import time.

    Returns True if listeners are now attached, False if the feature is disabled
    or registration failed (logged). Called from `app.main` at startup.
    """
    global _registered
    if _registered:
        return True
    try:
        from sqlalchemy import event

        from app.models.brd import BRD
        from app.models.phase_c import A2AMessage
        from app.models.tech_spec import TechSpec

        models = {
            "tech_specs": TechSpec,
            "brds": BRD,
            "a2a_messages": A2AMessage,
        }
        for table, attrs in _REHYDRATABLE.items():
            model = models.get(table)
            if model is None:
                continue
            event.listen(model, "load", _make_listener(table, attrs))
        _registered = True
        logger.info("coldstore read-through rehydration registered for %s",
                    sorted(_REHYDRATABLE))
        return True
    except Exception:  # noqa: BLE001 — never block startup over a read optimisation
        logger.exception("could not register coldstore read-through listeners")
        return False
