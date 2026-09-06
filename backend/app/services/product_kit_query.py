# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Latest-version-per-doc_type queries for product kit documents.

ProductKitDocument keeps full version history (a new row per regeneration, like
BRD/TSD), so callers that want "the current kit" must select the highest version
per doc_type rather than reading an arbitrary row.

Portable across Postgres (prod) and SQLite (test harness) — no Postgres-only
`DISTINCT ON`; uses a `max(version) GROUP BY doc_type` subquery joined back to
the table. Writers guarantee a unique version per (change, doc_type), so the
join returns exactly one row per doc_type.
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.product_kit import (
    RETIRED_DOC_TYPES, ProductKitDocument, ProductKitDocType,
)


def latest_kit_docs(db: Session, change_request_id: str,
                    include_retired: bool = False) -> list[ProductKitDocument]:
    """All product kit docs for a change, one row per doc_type (highest version).

    Retired doc types (`RETIRED_DOC_TYPES`) are excluded by default: this query backs the
    dispatch envelope, so a legacy `product_doc` row on an old change would otherwise still
    be shipped to every bank alongside the `product_note` that replaced it. Pass
    `include_retired=True` for admin/audit views that need the full historical set.
    """
    PK = ProductKitDocument
    mx = (
        select(PK.doc_type, func.max(PK.version).label("mv"))
        .where(PK.change_request_id == change_request_id)
        .group_by(PK.doc_type)
        .subquery()
    )
    rows = list(
        db.scalars(
            select(PK)
            .join(mx, (PK.doc_type == mx.c.doc_type) & (PK.version == mx.c.mv))
            .where(PK.change_request_id == change_request_id)
        ).all()
    )
    if include_retired:
        return rows
    # Filter in Python, not SQL — doc_type is a native enum and the column may hold either
    # the enum or its value depending on the driver path.
    return [r for r in rows
            if getattr(r.doc_type, "value", r.doc_type) not in RETIRED_DOC_TYPES]


def latest_kit_doc(
    db: Session, change_request_id: str, doc_type: ProductKitDocType,
) -> ProductKitDocument | None:
    """The current (highest-version) row for one doc_type, or None."""
    PK = ProductKitDocument
    return db.scalars(
        select(PK)
        .where(PK.change_request_id == change_request_id, PK.doc_type == doc_type)
        .order_by(PK.version.desc())
    ).first()


def kit_doc_versions(
    db: Session, change_request_id: str, doc_type: ProductKitDocType,
) -> list[ProductKitDocument]:
    """All saved versions of one doc_type, newest first. Each generation/revision
    inserts a new row (never overwrites), so this is the full edit history."""
    PK = ProductKitDocument
    return list(
        db.scalars(
            select(PK)
            .where(PK.change_request_id == change_request_id, PK.doc_type == doc_type)
            .order_by(PK.version.desc())
        ).all()
    )


def kit_doc_by_version(
    db: Session, change_request_id: str, doc_type: ProductKitDocType, version: int,
) -> ProductKitDocument | None:
    """One specific saved version of a doc_type, or None."""
    PK = ProductKitDocument
    return db.scalars(
        select(PK).where(
            PK.change_request_id == change_request_id,
            PK.doc_type == doc_type,
            PK.version == version,
        )
    ).first()


def kit_versions(db: Session, change_request_id: str) -> list[int]:
    """Distinct kit snapshot versions (negotiation_version) for a change, ascending."""
    PK = ProductKitDocument
    rows = db.scalars(
        select(PK.negotiation_version)
        .where(PK.change_request_id == change_request_id)
        .distinct()
    ).all()
    return sorted(int(v) for v in rows if v is not None)


def kit_docs_at_version(
    db: Session, change_request_id: str, negotiation_version: int,
) -> list[ProductKitDocument]:
    """Docs for one kit snapshot, one row per doc_type (highest version within
    that negotiation_version). Mirrors latest_kit_docs but scoped to a snapshot."""
    PK = ProductKitDocument
    mx = (
        select(PK.doc_type, func.max(PK.version).label("mv"))
        .where(
            PK.change_request_id == change_request_id,
            PK.negotiation_version == negotiation_version,
        )
        .group_by(PK.doc_type)
        .subquery()
    )
    return list(
        db.scalars(
            select(PK)
            .join(mx, (PK.doc_type == mx.c.doc_type) & (PK.version == mx.c.mv))
            .where(
                PK.change_request_id == change_request_id,
                PK.negotiation_version == negotiation_version,
            )
        ).all()
    )
