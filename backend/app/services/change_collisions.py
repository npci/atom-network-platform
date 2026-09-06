# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Cross-change collision detection (accuracy S8).

Two change requests that touch the same schema file / flow concurrently are
invisible to each other until (at best) a git merge conflict — and semantic
conflicts (both extend the same complexType differently) may never surface.

This intersects the queryable ``change_impacted_paths`` rows (persisted by the
Change-Analysis stage) across all NON-completed changes, so the UI can warn
"CR-123 also touches pain.001.xsd" at plan approval / Phase A start. Advisory
only — nothing is blocked.
"""
from __future__ import annotations

from sqlalchemy.orm import Session


def cross_change_collisions(db: Session, change_request_id: str) -> list[dict]:
    """Other non-completed changes that touch a (repo_id, path) this change also
    touches. Empty when this change has no impacted paths recorded yet."""
    from sqlalchemy import tuple_
    from app.models.change_analysis import ChangeImpactedPath
    from app.models.change_request import ChangeRequest, ChangeStatus

    my_pairs = [(r.repo_id, r.path) for r in db.query(
        ChangeImpactedPath.repo_id, ChangeImpactedPath.path
    ).filter(ChangeImpactedPath.change_request_id == change_request_id)]
    if not my_pairs:
        return []

    # Intersect in SQL: join the OTHER changes' impacted paths against mine and their
    # status, so the DB returns only the overlapping rows on non-completed changes —
    # instead of loading every ChangeImpactedPath row for every change into memory and
    # intersecting in Python (the inner join also drops orphaned rows for free).
    rows = (db.query(ChangeImpactedPath)
            .join(ChangeRequest, ChangeRequest.id == ChangeImpactedPath.change_request_id)
            .filter(ChangeImpactedPath.change_request_id != change_request_id)
            .filter(ChangeRequest.status != ChangeStatus.COMPLETED)
            .filter(tuple_(ChangeImpactedPath.repo_id, ChangeImpactedPath.path).in_(my_pairs))
            .all())

    out: list[dict] = []
    seen: set[tuple] = set()
    for o in rows:
        key = (o.change_request_id, o.repo_id, o.path)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "change_request_id": o.change_request_id,
            "repo_id": o.repo_id,
            "path": o.path,
            "namespace": o.namespace,
        })
    return out
