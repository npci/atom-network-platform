# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Persistence for the certification flow state (CERT-0) — owns `cert_flow_states`.

`FlowState` stays pure; this module is the only thing that knows the phase now
lives in a table. The orchestrator hands `persister(...)`'s callable to
`FlowState(persist=...)`, and every legal transition lands here.

THE WATERMARK, and why it lives on the FlowState instance. The orchestrator
builds a FRESH `FlowState` per dispatch, so an instance's in-memory history
starts empty against a stored row that already carries earlier rounds' entries.
Appending `history[len(stored):]` therefore silently drops every round-2
transition — the bug the first pass's tests caught. Instead `flow.flushed`
counts what THIS instance has successfully flushed: `save` appends
`flow.history[flow.flushed:]` and advances the watermark only after the commit
succeeds, so a failed write leaves the missed entries queued for the next save.

DELIBERATE NON-BEHAVIOUR: no mid-phase resume. Every dispatch starts at
NOT_STARTED and overwrites the stored `phase`; `history` accumulates across
rounds. Resuming semantics belong to the loop (CERT-6), not here.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.phase_c import CertFlowState

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.services.cert_agent.flow import FlowState

logger = logging.getLogger(__name__)

__all__ = ["load", "save", "persister"]


def load(db: "Session", cflow_id: str) -> CertFlowState | None:
    """The stored row, or None when this cflow has never persisted a phase."""
    return db.get(CertFlowState, cflow_id)


def _locked_row(db: "Session", cflow_id: str) -> CertFlowState | None:
    """The row, locked for update until this transaction ends.

    `history` is a JSON column appended by read-modify-write, which two
    concurrent savers will interleave into a lost update — both read the same
    list, both append their own entry, the second commit overwrites the first
    and a legitimate transition VANISHES from the certification audit trail.
    Locking the row serialises the read and the write.

    `with_for_update()` compiles to nothing on SQLite (its dialect has no
    FOR UPDATE and drops the clause), which is correct there: the unit harness
    is single-connection and SQLite serialises writers with a database-level
    lock anyway.
    """
    return db.execute(
        select(CertFlowState)
        .where(CertFlowState.cflow_id == cflow_id)
        .with_for_update()
    ).scalars().first()


def save(db: "Session", flow: "FlowState", *, change_request_id: str,
         partner_id: str, current_round: int) -> None:
    """Upsert the row for `flow.cflow_id`: overwrite phase/round, APPEND the
    instance's unflushed history entries.

    Serialised against concurrent savers by a row lock (see `_locked_row`),
    with one retry for the insert race two savers hit when the row does not
    exist yet — there the loser gets an IntegrityError on the primary key and
    must re-read the winner's row rather than clobber it.

    Raises on a failed write (after rolling the session back) — the caller is
    `FlowState._persist_safely`, which catches and logs, so a broken store
    never aborts a partner's run; the un-advanced watermark retries the missed
    entries on the next save.
    """
    for attempt in (1, 2):
        committed = False
        try:
            row = _locked_row(db, flow.cflow_id)
            if row is None:
                row = CertFlowState(
                    cflow_id=flow.cflow_id,
                    change_request_id=change_request_id,
                    partner_id=partner_id,
                    history=[],
                )
                db.add(row)
                # Flush inside the try: a concurrent insert of the same
                # cflow_id surfaces HERE as an IntegrityError, while the retry
                # below can still recover it.
                db.flush()
            row.change_request_id = change_request_id
            row.partner_id = partner_id
            row.phase = flow.phase.value
            row.current_round = current_round
            pending = flow.history[flow.flushed:]
            if pending:
                # Reassign rather than mutate: plain JSON columns do not track
                # in-place mutation, so `row.history.append(...)` would never
                # reach the database.
                row.history = list(row.history or []) + [list(e) for e in pending]
            db.commit()
            committed = True
            break
        except IntegrityError:
            if attempt == 2:
                raise
            logger.info(
                "cert_flow %s: row created concurrently — retrying the append",
                flow.cflow_id,
            )
        finally:
            # Rollback on ANY failure, including ones this function does not
            # name, without needing a catch-all clause to do it.
            if not committed:
                db.rollback()
    # Only after a COMMITTED append: a failed write leaves the watermark where
    # it was so the missed entries ride along on the next save.
    flow.flushed = len(flow.history)


def persister(db: "Session", *, change_request_id: str, partner_id: str,
              current_round: int) -> Callable[["FlowState"], None]:
    """The callable the orchestrator hands to `FlowState(persist=...)` —
    one dispatch, one round, one session."""

    def _persist(flow: "FlowState") -> None:
        save(db, flow, change_request_id=change_request_id,
             partner_id=partner_id, current_round=current_round)

    return _persist
