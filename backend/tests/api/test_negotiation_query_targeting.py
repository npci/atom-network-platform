# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Which partner query a PM reply answers.

`approve_and_respond` echoes a correlation_id back to the partner as `query_id`,
and the partner marks THAT OutgoingQuery answered. It used to always pick the
newest correlated partner message, so with two queries in flight, answering the
first went out stamped with the second's id — the partner settled the wrong
query, and (when the newest already had a response) filed the reply as a
follow-up on it instead.

`_pick_correlation_id` is the choice, split out so it can be exercised without
a database.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

from app.api.phase_c import _pick_correlation_id  # noqa: E402


def _q(corr):
    return SimpleNamespace(correlation_id=corr)


# Newest first, the order the endpoint queries them in.
NEWEST_FIRST = [_q("corr-2"), _q("corr-1")]


def test_explicit_target_wins_over_the_newest_query():
    # The bug: answering query 1 while query 2 is open went out as query 2.
    assert _pick_correlation_id(NEWEST_FIRST, "corr-1") == "corr-1"


def test_no_target_falls_back_to_the_newest():
    # Legacy clients send nothing; newest-wins is right when only one is open.
    assert _pick_correlation_id(NEWEST_FIRST, None) == "corr-2"


def test_unknown_target_is_ignored_not_echoed():
    # A stale/foreign id must not reach the partner — fall back rather than
    # address a query that isn't in this thread.
    assert _pick_correlation_id(NEWEST_FIRST, "corr-from-another-thread") == "corr-2"


def test_uncorrelated_rows_are_skipped():
    rows = [_q(None), _q("corr-2"), _q(None), _q("corr-1")]
    assert _pick_correlation_id(rows, None) == "corr-2"
    assert _pick_correlation_id(rows, "corr-1") == "corr-1"


def test_no_correlated_query_yields_none():
    assert _pick_correlation_id([], "corr-1") is None
    assert _pick_correlation_id([_q(None)], None) is None


def test_single_open_query_is_unaffected():
    # The common case must behave exactly as before, with or without a target.
    one = [_q("corr-1")]
    assert _pick_correlation_id(one, None) == "corr-1"
    assert _pick_correlation_id(one, "corr-1") == "corr-1"
