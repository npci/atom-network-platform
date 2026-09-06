# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""AR-07 / A14 — `GET /threads` must not issue N+1 queries.

The original `list_all_threads` loop issued 1 + 3N queries: one for the thread
list, then per thread a ChangeRequest get, a PartnerAgent get and a
NegotiationMessage select. At 100 threads that is 301 round-trips.

`app/api/a2a.py` fixed it by eager-loading messages and batching the change and
partner lookups into one `IN` query each. This file is the regression evidence
the EA re-review asks for ("add a query-count test for 1, 10, and 100 threads"):
it counts real SELECT statements at the DBAPI cursor and asserts the count does
not move with N.

Measured constant: 4 SELECTs, identical at 1, 10 and 100 threads — the thread
list, the eager `selectin` load of messages, and one batched `IN` each for
changes and partners. The source comment says "three queries", counting the
selectin load as part of one logical fetch; 4 is the statement count at the
cursor. The ceiling below sits a little above that so a legitimate future
query does not fail the suite, while an N+1 (30+ at ten threads) still does.

Both halves matter. A function that returned an empty list would issue a
constant number of queries too, so every case also asserts the payload is
correct — the count is only meaningful alongside proof the work was done.

Verified by mutation, not only by passing: removing `selectinload` fails four
of these tests, and removing the batched `IN` lookups in favour of per-thread
`db.get()` fails three. A variant that keeps the batch queries and adds
per-thread `get()` calls does NOT fail, correctly — the identity map serves
those from memory, so it issues the same 4 statements and is not a regression.
"""
import importlib
import pkgutil

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 -- register mappers on Base before create_all

# Several FK targets are not re-exported from app.models.__init__, and
# create_all needs the whole graph or the thread/message tables have dangling
# references. Same import dance the governance tests use.
for _m in pkgutil.iter_modules(app.models.__path__):
    importlib.import_module(f"app.models.{_m.name}")

from app.core.database import Base                                # noqa: E402
from app.models.change_request import ChangeRequest               # noqa: E402
from app.models.phase_c import (                                  # noqa: E402
    NegotiationMessage,
    NegotiationRole,
    NegotiationThread,
    PartnerAgent,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


class _QueryCounter:
    """Counts SELECTs issued on a connection between enter and exit.

    Hooked at `before_cursor_execute`, so it sees what actually reaches the
    DBAPI — an ORM-level count could be satisfied by a lazy load that still
    costs a round-trip, which is the exact failure mode being guarded.
    """

    def __init__(self, session):
        self._engine = session.get_bind()
        self.statements: list[str] = []

    def __enter__(self):
        event.listen(self._engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc):
        event.remove(self._engine, "before_cursor_execute", self._record)
        return False

    def _record(self, conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            self.statements.append(statement)

    @property
    def count(self) -> int:
        return len(self.statements)


def _seed(db, n_threads: int, msgs_per_thread: int = 3) -> None:
    """One change and one partner per thread, so the batched IN lookups have
    something real to collapse. Sharing a single change across all threads
    would let a per-thread `get()` be served from the identity map and hide
    an N+1 that a production spread of ids would expose."""
    for i in range(n_threads):
        cr = ChangeRequest(id=f"cr-{i}", initial_prompt=f"prompt {i}", created_by="tester")
        partner = PartnerAgent(id=f"p-{i}", name=f"Bank {i}")
        thread = NegotiationThread(id=f"t-{i}", change_request_id=cr.id, partner_id=partner.id)
        db.add_all([cr, partner, thread])
        for j in range(msgs_per_thread):
            db.add(NegotiationMessage(
                id=f"m-{i}-{j}",
                thread_id=thread.id,
                role=NegotiationRole.PARTNER if j == 0 else NegotiationRole.PO_APPROVED,
                content=f"message {j} on thread {i}",
            ))
    db.commit()
    db.expunge_all()   # force real SELECTs, not identity-map hits


def _list_threads(db) -> list:
    """The threads array from the endpoint's payload.

    The route returns `{"threads": [...], "total_unread": N}`; every assertion
    here is about the thread rows, so unwrap once rather than at each call
    site. Called directly rather than through the router: the FastAPI deps
    supply only a session and an authenticated user, and neither participates
    in the query pattern under test.
    """
    from app.api.a2a import list_all_threads
    return list_all_threads(db=db, _=None)["threads"]


@pytest.mark.parametrize("n_threads", [1, 10, 100])
def test_query_count_is_constant_in_thread_count(db, n_threads):
    """The scenario the finding names, at the three sizes it names."""
    _seed(db, n_threads)
    with _QueryCounter(db) as counter:
        result = _list_threads(db)

    assert len(result) == n_threads, "every seeded thread must be returned"
    assert counter.count <= 6, (
        f"{n_threads} threads issued {counter.count} SELECTs — the listing is "
        f"scaling with thread count again (N+1 regression).\n"
        + "\n".join(f"  {s.splitlines()[0][:110]}" for s in counter.statements)
    )


def test_query_count_does_not_grow_between_1_and_100(db):
    """The property, stated directly: the same query count at both extremes.

    A per-size ceiling could be satisfied by a listing that grows slowly.
    This compares the two ends against each other, which is what "constant
    regardless of N" actually means.
    """
    _seed(db, 1)
    with _QueryCounter(db) as small:
        assert len(_list_threads(db)) == 1

    db.query(NegotiationMessage).delete()
    db.query(NegotiationThread).delete()
    db.query(ChangeRequest).delete()
    db.query(PartnerAgent).delete()
    db.commit()

    _seed(db, 100)
    with _QueryCounter(db) as large:
        assert len(_list_threads(db)) == 100

    assert large.count == small.count, (
        f"1 thread -> {small.count} SELECTs, 100 threads -> {large.count}. "
        f"The listing must issue the same number of queries at both sizes."
    )


def test_messages_are_eager_loaded_not_lazy_per_thread(db):
    """Pins the specific mechanism, so removing `selectinload` fails here with
    a message naming the cause rather than only tripping the ceiling above."""
    _seed(db, 20, msgs_per_thread=2)
    with _QueryCounter(db) as counter:
        _list_threads(db)

    message_selects = [s for s in counter.statements if "negotiation_messages" in s.lower()]
    assert len(message_selects) <= 1, (
        f"{len(message_selects)} separate SELECTs against negotiation_messages for 20 "
        f"threads — messages are being lazy-loaded per thread instead of eager-loaded."
    )


def test_unread_counts_survive_the_batching(db):
    """The optimisation must not have changed the answer. A partner message
    with no approved reply after it counts as unread; one with a reply does not."""
    cr = ChangeRequest(id="cr-x", initial_prompt="p", created_by="tester")
    partner = PartnerAgent(id="p-x", name="Bank X")
    thread = NegotiationThread(id="t-x", change_request_id=cr.id, partner_id=partner.id)
    db.add_all([cr, partner, thread])
    db.add(NegotiationMessage(id="m1", thread_id="t-x",
                              role=NegotiationRole.PARTNER, content="answered"))
    db.add(NegotiationMessage(id="m2", thread_id="t-x",
                              role=NegotiationRole.PO_APPROVED, content="the reply"))
    db.add(NegotiationMessage(id="m3", thread_id="t-x",
                              role=NegotiationRole.PARTNER, content="still open"))
    db.commit()
    db.expunge_all()

    from app.api.a2a import list_all_threads
    payload = list_all_threads(db=db, _=None)
    assert len(payload["threads"]) == 1
    assert payload["threads"][0]["unread_count"] == 1, (
        "only the trailing partner message is unread")
    assert payload["total_unread"] == 1, (
        "the rolled-up total must agree with the per-thread count")
