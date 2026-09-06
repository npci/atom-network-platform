# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Backfill `negotiation_messages` rows for existing CounterProposals
and Blockers so the unified-conversation cutover (Step 4) has every
historical structured event represented as a real NM row.

For each existing `counter_proposals` row missing an NM with that
`counter_proposal_id`, insert one (`event_kind='proposal'`). If the
row is resolved AND no NM with that link+`event_kind='resolution'`
exists, insert that one too.

Same shape for `blockers` rows (`event_kind='blocker'` for the
creation, `'blocker_resolution'` when resolved).

Threads are created on demand for (change, partner, 'general') pairs
that don't have one yet — CPs/Blockers can predate any Q&A on the
same change.

Idempotent: re-running the migration is a no-op since each insert is
gated on "row with this link + event_kind not already present".

Revision ID: 0055
Revises:    0054
Create Date: 2026-05-18
"""
from alembic import op
import sqlalchemy as sa
import uuid


revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def _new_uuid() -> str:
    return str(uuid.uuid4())


def upgrade() -> None:
    bind = op.get_bind()

    # Cache thread ids keyed by (change_request_id, partner_id, kind)
    # so we don't lookup-or-create per row when many CPs share a thread.
    thread_cache: dict[tuple[str, str, str], str] = {}

    def _get_or_create_thread(change_id: str, partner_id: str, kind: str = "general") -> str:
        key = (change_id, partner_id, kind)
        cached = thread_cache.get(key)
        if cached is not None:
            return cached
        row = bind.execute(
            sa.text("""
                SELECT id FROM negotiation_threads
                 WHERE change_request_id = :c AND partner_id = :p AND kind = :k
                 LIMIT 1
            """),
            {"c": change_id, "p": partner_id, "k": kind},
        ).first()
        if row is not None:
            thread_cache[key] = row[0]
            return row[0]
        tid = _new_uuid()
        bind.execute(
            sa.text("""
                INSERT INTO negotiation_threads
                    (id, change_request_id, partner_id, kind, status, created_at)
                VALUES (:id, :c, :p, :k, 'OPEN', NOW())
            """),
            {"id": tid, "c": change_id, "p": partner_id, "k": kind},
        )
        thread_cache[key] = tid
        return tid

    # ── CounterProposals ──────────────────────────────────────────────────
    cps = bind.execute(
        sa.text("""
            SELECT id, change_request_id, partner_id, originator,
                   justification, resolution_text, resolved_by,
                   created_at, resolved_at
              FROM counter_proposals
             ORDER BY created_at
        """)
    ).fetchall()

    for cp in cps:
        cp_id, change_id, partner_id, originator, justification, \
            resolution_text, resolved_by, created_at, resolved_at = cp

        # Skip proposal NM if one already exists for this CP+proposal kind
        exists = bind.execute(
            sa.text("""
                SELECT 1 FROM negotiation_messages
                 WHERE counter_proposal_id = :cp AND event_kind = 'proposal'
                 LIMIT 1
            """),
            {"cp": cp_id},
        ).first()
        if exists is None:
            tid = _get_or_create_thread(change_id, partner_id, "general")
            role = "partner" if originator == "partner" else "po_approved"
            bind.execute(
                sa.text("""
                    INSERT INTO negotiation_messages
                        (id, thread_id, role, content, approved_by,
                         counter_proposal_id, event_kind, created_at)
                    VALUES (:id, :tid, :role, :content, :approved_by,
                            :cp, 'proposal', :created_at)
                """),
                {
                    "id": _new_uuid(),
                    "tid": tid,
                    "role": role,
                    "content": justification or "(no justification)",
                    "approved_by": None,
                    "cp": cp_id,
                    "created_at": created_at,
                },
            )

        if resolved_at is None:
            continue

        exists = bind.execute(
            sa.text("""
                SELECT 1 FROM negotiation_messages
                 WHERE counter_proposal_id = :cp AND event_kind = 'resolution'
                 LIMIT 1
            """),
            {"cp": cp_id},
        ).first()
        if exists is None:
            tid = _get_or_create_thread(change_id, partner_id, "general")
            # Resolution is always authored on the OPPOSITE side from
            # the original proposer — matches the synthetic event
            # emitter's `originator` flip at phase_c.py:515.
            role = "po_approved" if originator == "partner" else "partner"
            bind.execute(
                sa.text("""
                    INSERT INTO negotiation_messages
                        (id, thread_id, role, content, approved_by,
                         counter_proposal_id, event_kind, created_at)
                    VALUES (:id, :tid, :role, :content, :approved_by,
                            :cp, 'resolution', :created_at)
                """),
                {
                    "id": _new_uuid(),
                    "tid": tid,
                    "role": role,
                    "content": resolution_text or "(resolved)",
                    "approved_by": resolved_by,
                    "cp": cp_id,
                    "created_at": resolved_at,
                },
            )

    # ── Blockers ──────────────────────────────────────────────────────────
    blocks = bind.execute(
        sa.text("""
            SELECT id, change_request_id, partner_id,
                   description, resolution_text, resolved_by,
                   created_at, resolved_at
              FROM blockers
             ORDER BY created_at
        """)
    ).fetchall()

    for b in blocks:
        bid, change_id, partner_id, description, \
            resolution_text, resolved_by, created_at, resolved_at = b

        exists = bind.execute(
            sa.text("""
                SELECT 1 FROM negotiation_messages
                 WHERE blocker_id = :b AND event_kind = 'blocker'
                 LIMIT 1
            """),
            {"b": bid},
        ).first()
        if exists is None:
            tid = _get_or_create_thread(change_id, partner_id, "general")
            bind.execute(
                sa.text("""
                    INSERT INTO negotiation_messages
                        (id, thread_id, role, content,
                         blocker_id, event_kind, created_at)
                    VALUES (:id, :tid, 'partner', :content,
                            :b, 'blocker', :created_at)
                """),
                {
                    "id": _new_uuid(),
                    "tid": tid,
                    "content": description or "(no description)",
                    "b": bid,
                    "created_at": created_at,
                },
            )

        if resolved_at is None:
            continue

        exists = bind.execute(
            sa.text("""
                SELECT 1 FROM negotiation_messages
                 WHERE blocker_id = :b AND event_kind = 'blocker_resolution'
                 LIMIT 1
            """),
            {"b": bid},
        ).first()
        if exists is None:
            tid = _get_or_create_thread(change_id, partner_id, "general")
            bind.execute(
                sa.text("""
                    INSERT INTO negotiation_messages
                        (id, thread_id, role, content, approved_by,
                         blocker_id, event_kind, created_at)
                    VALUES (:id, :tid, 'po_approved', :content, :approved_by,
                            :b, 'blocker_resolution', :created_at)
                """),
                {
                    "id": _new_uuid(),
                    "tid": tid,
                    "content": resolution_text or "(resolved)",
                    "approved_by": resolved_by,
                    "b": bid,
                    "created_at": resolved_at,
                },
            )


def downgrade() -> None:
    # Surgical reverse: remove only the rows we created (those with
    # a link FK set + a backfilled event_kind). User-authored chat
    # rows have both FKs null and stay untouched.
    op.execute(
        sa.text("""
            DELETE FROM negotiation_messages
             WHERE (counter_proposal_id IS NOT NULL OR blocker_id IS NOT NULL)
               AND event_kind IS NOT NULL
        """)
    )
