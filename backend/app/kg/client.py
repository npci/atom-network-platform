# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Cypher-over-SQL client for Apache AGE.

Apache AGE exposes Cypher via SQL wrappers:

    SELECT * FROM cypher('graph_name', $$
        MATCH (n:Class) RETURN n.name, n.kind
    $$) AS (name agtype, kind agtype);

Every call must (a) have `LOAD 'age'` in the current session and (b) have
`ag_catalog` on the search path so AGE functions resolve. We do both on
every `run_cypher` call so the client is stateless and safe from any caller
(FastAPI request, Celery task, REPL).

Key design choices:
  - `build_cypher_sql` is pure — unit-testable without AGE. It builds the
    SELECT string given a Cypher body + return column specs. Returns the
    SQL string plus a typed-row decoder callable.
  - `run_cypher` is the side-effecting wrapper that executes against a
    real session. Marked tests skip when AGE is unavailable.
  - Parameters: AGE Cypher parameter binding is version-dependent (older
    releases embed literals). For safety and portability, `run_cypher`
    accepts a `cypher_params` dict that the caller pre-interpolates via
    `escape_cypher_literal` (provided). This keeps SQL injection surface
    narrow and explicit.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Pure helpers
# ──────────────────────────────────────────────────────────────────────────────

# AGE column type is always "agtype" for Cypher-returned values.
_DEFAULT_RET_TYPE = "agtype"

# Cypher body must NOT contain its own `$$` delimiter (would break the dollar-
# quoted SQL literal). Caller errors out early if we spot one.
_DOLLAR_QUOTE_RE = re.compile(r"\$\$")


def build_cypher_sql(
    cypher: str,
    *,
    graph_name: str,
    return_cols: list[tuple[str, str]] | list[str] | None = None,
) -> str:
    """Build the `SELECT * FROM cypher(...) AS (...)` wrapper SQL.

    Args:
        cypher: The Cypher query body (NO surrounding `$$`).
        graph_name: The AGE named graph.
        return_cols: List of (col_name, col_type) tuples for the AS clause.
                     Strings shorthand for `(name, "agtype")`. None → single
                     implicit column `result agtype`.

    Returns:
        SQL string ready to pass to `session.execute(text(...))`.
    """
    if not cypher or not cypher.strip():
        raise ValueError("cypher body must be non-empty")
    if _DOLLAR_QUOTE_RE.search(cypher):
        raise ValueError("cypher body must not contain '$$' (would break dollar-quoted literal)")
    if not graph_name or not graph_name.replace("_", "").isalnum():
        # Reject graph names that could break the quoted wrapper.
        raise ValueError(f"invalid graph_name: {graph_name!r}")

    if not return_cols:
        as_clause = "(result agtype)"
    else:
        parts: list[str] = []
        for c in return_cols:
            if isinstance(c, tuple):
                name, typ = c
            else:
                name, typ = c, _DEFAULT_RET_TYPE
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"invalid return_col name: {name!r}")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", typ):
                raise ValueError(f"invalid return_col type: {typ!r}")
            parts.append(f"{name} {typ}")
        as_clause = "(" + ", ".join(parts) + ")"

    return (
        f"SELECT * FROM cypher('{graph_name}', $$\n{cypher}\n$$) AS {as_clause};"
    )


def escape_cypher_literal(value: Any) -> str:
    """Format a Python value as a Cypher literal for safe string interpolation.

    - None         → null
    - bool         → true / false
    - int / float  → numeric literal
    - str          → 'escaped'  (single quotes inside are doubled)
    - list / dict  → JSON (Cypher maps/arrays share JSON syntax closely)

    Caller is responsible for choosing when to use this vs. a parameter
    passed through SQL bind vars. Both patterns have trade-offs in AGE.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    if isinstance(value, (list, dict)):
        # JSON is sufficient for our common cases (lists of strings, flat maps).
        return json.dumps(value, ensure_ascii=False)
    raise TypeError(f"unsupported Cypher literal type: {type(value).__name__}")


# ──────────────────────────────────────────────────────────────────────────────
# Row decoding
# ──────────────────────────────────────────────────────────────────────────────

# AGE returns agtype-wrapped values. For primitives, agtype stringifies as
# JSON — we use json.loads as a best-effort decoder.

def _decode_agtype(raw: Any) -> Any:
    """Best-effort decode of an agtype value to a Python primitive / dict."""
    if raw is None:
        return None
    if isinstance(raw, (int, float, bool, dict, list)):
        return raw
    s = str(raw)
    # Newer AGE returns values like `"hello"::agtype` — strip the ::agtype
    # suffix if present.
    s = re.sub(r"::agtype$", "", s)
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        # Fall back to the raw string if it isn't JSON-parseable
        # (e.g. agtype nodes include structural headers).
        return s


# ──────────────────────────────────────────────────────────────────────────────
# Session bootstrap
# ──────────────────────────────────────────────────────────────────────────────

def _ensure_age_session(db: Session) -> None:
    """Run `LOAD 'age'` and set the search path on the current session.

    Cheap and idempotent — AGE handles repeat LOADs. Call before any Cypher.
    """
    db.execute(text("LOAD 'age';"))
    db.execute(text('SET search_path = ag_catalog, "$user", public;'))


# ──────────────────────────────────────────────────────────────────────────────
# Main entry
# ──────────────────────────────────────────────────────────────────────────────

def run_cypher(
    db: Session,
    cypher: str,
    *,
    graph_name: str | None = None,
    return_cols: list[tuple[str, str]] | list[str] | None = None,
    decode: Callable[[Any], Any] | None = None,
) -> list[dict]:
    """Execute a Cypher query against the configured AGE graph.

    Args:
        db: SQLAlchemy session.
        cypher: The Cypher query body (no `$$`).
        graph_name: Override for the default `settings.kg_graph_name`.
        return_cols: List of (col_name, col_type) specs. Defaults to a single
                     `result agtype` column.
        decode: Optional value decoder per cell. Defaults to `_decode_agtype`.

    Returns:
        List of dicts, one per row. Keys match `return_cols` names (or
        `result` when omitted). Values are decoded best-effort.

    Raises:
        Any SQLAlchemy / psycopg2 error on malformed Cypher or connection
        failure — caller decides how to handle. (Use `is_age_available` to
        probe upfront.)
    """
    graph_name = graph_name or settings.kg_graph_name
    decode = decode or _decode_agtype

    sql = build_cypher_sql(cypher, graph_name=graph_name, return_cols=return_cols)

    _ensure_age_session(db)
    result = db.execute(text(sql))

    col_names: list[str]
    if not return_cols:
        col_names = ["result"]
    else:
        col_names = [c[0] if isinstance(c, tuple) else c for c in return_cols]

    out: list[dict] = []
    for row in result.fetchall():
        mapped = {}
        for name, value in zip(col_names, row):
            mapped[name] = decode(value)
        out.append(mapped)
    return out


def is_age_available(db: Session) -> bool:
    """Probe: returns True iff AGE + the configured graph are reachable.

    Useful for tests (skip `@pytest.mark.age` when False) and for
    conditional wiring (features that depend on KG degrade gracefully).
    """
    try:
        rows = run_cypher(db, "RETURN 1", return_cols=[("v", "agtype")])
        return len(rows) == 1
    except Exception as e:
        logger.debug("is_age_available: %s", e)
        # A failed `LOAD 'age'` / Cypher aborts the Postgres transaction; roll
        # back so the caller's connection is usable again. Without this the
        # poisoned transaction surfaces later as InFailedSqlTransaction on the
        # next unrelated statement (e.g. emit_event's max(seq) query).
        _safe_rollback(db)
        _warn_if_graph_missing(db)
        return False


def _safe_rollback(db: Session) -> None:
    """Undo a poisoned transaction, never raising in the process.

    Shared by the probe and its diagnostic: both run on a failure path where a
    rollback that raises would replace the real error with a less useful one.
    """
    try:
        db.rollback()
    except Exception:  # noqa: BLE001 — cleanup on a failure path
        pass


def _warn_if_graph_missing(db: Session) -> None:
    """Separate "AGE is absent" from "the configured graph is absent".

    AGE distinguishes these perfectly well — a query against an unknown graph
    raises `graph "x" does not exist`, it does NOT create one — but the probe
    above collapses every cause into a DEBUG line and a False. An operator whose
    only problem was a renamed `KG_GRAPH_NAME` was then told by the callers to
    "check the Postgres container + migration 0020", found both healthy, and had
    nothing else to go on while every graph-hop answer came back empty.

    Asking `ag_catalog.ag_graph` directly is what makes the two distinguishable:
    if that table is readable, AGE is installed and the graph simply is not
    there. Best-effort — this runs on a failure path and must not raise.
    """
    graph = settings.kg_graph_name
    try:
        names = [r[0] for r in db.execute(
            text("SELECT name::text FROM ag_catalog.ag_graph")).fetchall()]
    except Exception:  # noqa: BLE001 — best-effort diagnostic, must not raise
        # ag_catalog unreadable => AGE really is absent, which the DEBUG line in
        # the caller already covers. The failed SELECT poisons the transaction
        # again, so undo it before handing the connection back.
        _safe_rollback(db)
        return
    if graph not in names:
        logger.warning(
            "kg: Apache AGE is installed but graph %r does not exist (present: %s). "
            "Graph retrieval will return nothing. An AGE graph cannot be renamed "
            "in place: either set KG_GRAPH_NAME to an existing graph, or re-ingest "
            "under this name.", graph, names or "none",
        )
