# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Apache AGE graph schema for the Authority knowledge graph (Slice 19).

Defines the full set of node (vertex) and edge labels called out in plan
§5.1 and provides `initialise_graph()` to create any that are missing.
Label creation is idempotent: we check `ag_catalog.ag_label` first and
skip if the label already exists.

Node labels cover the same taxonomy the plan describes:
  - Document layer:   Document, DocChunk
  - Code layer:       Repo, File, Module, Class, Function, Endpoint, Schema
  - Product layer:    Service, Feature, Capability
  - Design layer:     Requirement, ADR, Ticket
  - People layer:     Person, Team

Edge labels span structural / doc-code / product / history edge types.
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.error_taxonomy import client_safe_detail
from app.kg import client as kg_client

logger = logging.getLogger(__name__)


# Plan §5.1 — 17 vertex labels
NODE_LABELS: tuple[str, ...] = (
    "Document", "DocChunk",
    "Repo", "File", "Module", "Class", "Function", "Endpoint", "Schema",
    "Service", "Feature", "Capability",
    "Requirement", "ADR", "Ticket",
    "Person", "Team",
)

# Plan §5.1 — 15 edge labels
EDGE_LABELS: tuple[str, ...] = (
    # Code structural
    "CALLS", "IMPLEMENTS", "INHERITS", "IMPORTS", "EXPOSES", "CONSUMES",
    # Doc-code
    "DESCRIBES", "EXAMPLE_OF",
    # Product
    "PART_OF", "OWNED_BY", "DEPENDS_ON", "DEPRECATES",
    # History
    "CHANGED_IN", "INTRODUCED_IN", "BROKEN_BY",
)


# ──────────────────────────────────────────────────────────────────────────────
# Existence probe
# ──────────────────────────────────────────────────────────────────────────────

_LABEL_KIND_VERTEX = "v"
_LABEL_KIND_EDGE   = "e"


def _label_exists(db: Session, graph_name: str, label_name: str, kind: str) -> bool:
    """Check ag_catalog.ag_label for (graph, label, kind). Pure SQL, no Cypher."""
    sql = text("""
        SELECT 1
        FROM ag_catalog.ag_label l
        JOIN ag_catalog.ag_graph g ON l.graph = g.graphid
        WHERE g.name = :graph_name
          AND l.name = :label_name
          AND l.kind = :kind
        LIMIT 1
    """)
    kg_client._ensure_age_session(db)
    row = db.execute(sql, {
        "graph_name": graph_name, "label_name": label_name, "kind": kind,
    }).first()
    return row is not None


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def initialise_graph(
    db: Session,
    *,
    graph_name: str | None = None,
) -> dict:
    """Idempotently create the full label set.

    Returns a report dict:
      {
        "graph_name": "...",
        "vlabels_created": [...],
        "elabels_created": [...],
        "vlabels_skipped":  [...],   # already existed
        "elabels_skipped":  [...],
      }

    Raises only on connection / authorisation failures. Per-label failures
    are logged and included in a `failures` field so callers can diagnose.
    """
    graph_name = graph_name or settings.kg_graph_name

    vlabels_created: list[str] = []
    elabels_created: list[str] = []
    vlabels_skipped: list[str] = []
    elabels_skipped: list[str] = []
    failures: list[dict] = []

    kg_client._ensure_age_session(db)

    for label in NODE_LABELS:
        if _label_exists(db, graph_name, label, _LABEL_KIND_VERTEX):
            vlabels_skipped.append(label)
            continue
        try:
            db.execute(text(
                f"SELECT create_vlabel('{graph_name}', '{label}');"
            ))
            vlabels_created.append(label)
        except Exception as e:
            # SCR #6: `failures` is a declared field on KgInitResponse and is
            # returned by POST /api/kg/initialise. The failing statement is
            # `SELECT create_vlabel(...)`, so str(e) renders the psycopg2 class
            # name, the AGE catalog detail and the full SQL. Operators get all
            # of it from the log line below.
            failures.append({"label": label, "kind": "vertex",
                             "error": client_safe_detail(e)})
            logger.warning("create_vlabel(%s) failed: %s", label, e)

    for label in EDGE_LABELS:
        if _label_exists(db, graph_name, label, _LABEL_KIND_EDGE):
            elabels_skipped.append(label)
            continue
        try:
            db.execute(text(
                f"SELECT create_elabel('{graph_name}', '{label}');"
            ))
            elabels_created.append(label)
        except Exception as e:
            # SCR #6: see the sibling handler above — same response field,
            # same SQL disclosure.
            failures.append({"label": label, "kind": "edge",
                             "error": client_safe_detail(e)})
            logger.warning("create_elabel(%s) failed: %s", label, e)

    db.commit()

    logger.info(
        "KG schema init: graph=%s v_created=%d v_skipped=%d e_created=%d e_skipped=%d failures=%d",
        graph_name, len(vlabels_created), len(vlabels_skipped),
        len(elabels_created), len(elabels_skipped), len(failures),
    )

    return {
        "graph_name": graph_name,
        "vlabels_created": vlabels_created,
        "elabels_created": elabels_created,
        "vlabels_skipped": vlabels_skipped,
        "elabels_skipped": elabels_skipped,
        "failures": failures,
    }
