# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 2 (increment 2): cert_query → query+phase fold channel classification.

`_query_row_in_channel` decides whether an inbound query A2A row belongs to the
cert or general negotiation channel after the fold. It must treat:
  - legacy task_type='cert_query'            → cert
  - task_type='query' + payload phase='cert' → cert
  - task_type='query' (no/other phase)       → general
  - anything else                            → neither
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

from app.api.phase_c import _query_row_in_channel  # noqa: E402


def _row(task_type: str, phase: str | None = None):
    payload = {"task_type": task_type, "payload": {}}
    if phase is not None:
        payload["payload"]["phase"] = phase
    return SimpleNamespace(task_type=task_type, payload=payload)


def test_legacy_cert_query_is_cert_channel():
    r = _row("cert_query")
    assert _query_row_in_channel(r, "cert") is True
    assert _query_row_in_channel(r, "general") is False


def test_query_with_phase_cert_is_cert_channel():
    r = _row("query", phase="cert")
    assert _query_row_in_channel(r, "cert") is True
    assert _query_row_in_channel(r, "general") is False


def test_plain_query_is_general_channel():
    r = _row("query")
    assert _query_row_in_channel(r, "general") is True
    assert _query_row_in_channel(r, "cert") is False


def test_query_with_phase_general_is_general_channel():
    r = _row("query", phase="general")
    assert _query_row_in_channel(r, "general") is True
    assert _query_row_in_channel(r, "cert") is False


def test_non_query_task_type_is_neither():
    r = _row("blocker")
    assert _query_row_in_channel(r, "cert") is False
    assert _query_row_in_channel(r, "general") is False
