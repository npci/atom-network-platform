# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Grounding helpers that connect the pipeline (planning → BRD/XSD/TSD) — accuracy E1/E2.

Uses the project's fake-DB pattern (call helpers directly, no TestClient/auth stack) so we
cover the contract that keeps the chain consistent: the realized XSD change and the ratified
plan scope are threaded into the docs as BINDING constraints."""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
from tests._optional_stubs import stub_jwt, stub_pgvector

stub_jwt(sub="x", token="t")
stub_pgvector()

from app.api.agents import _format_xsd_change_summary, _ratified_scope_block  # noqa: E402


# ── E2: the realized-change summary is compact, names the change, and is scope-binding ──

def test_xsd_change_summary_names_change_and_binds_scope():
    rec = {"network-core:network-common.xsd": {"new": ["recurring", {"name": "recurringMarker"}],
                                       "modified": [], "deprecated": []}}
    s = _format_xsd_change_summary(rec)
    assert "REALIZED SCHEMA CHANGE" in s
    assert "recurring" in s and "recurringMarker" in s
    assert "must not describe schema beyond this" in s.lower()


def test_xsd_change_summary_empty_is_noop():
    assert _format_xsd_change_summary({}) == ""
    assert _format_xsd_change_summary(None) == ""


# ── E1: the ratified plan scope (approach + functional plan) becomes a binding block ──

class _Q:
    def __init__(self, result): self._r = result
    def filter(self, *a, **k): return self
    def order_by(self, *a, **k): return self
    def first(self): return self._r


class _FakeDB:
    def __init__(self, ca, run): self._ca, self._run = ca, run
    def query(self, model):
        from app.models.change_analysis import ChangeAnalysis
        return _Q(self._ca if model is ChangeAnalysis else self._run)


def test_ratified_scope_block_binds_approach_and_plan():
    # technical_analysis is a real ChangeAnalysis column the block reads for the
    # wire/API surface; empty here exercises the PSP-internal "NONE" branch.
    ca = SimpleNamespace(functional_plan={"overview": "Add a recurring marker only.",
                                          "steps": ["Mark ReqTransfer as mandate-executed"]},
                         technical_analysis={})
    run = SimpleNamespace(handoff_json={"approach_decision": {"option": {
        "title": "extend-marker", "target_api": "ReqTransfer", "how_it_fits": "minimal, backward compatible"}}})
    block = _ratified_scope_block("cr1", _FakeDB(ca, run))
    assert "RATIFIED CHANGE SCOPE (BINDING" in block
    assert "extend-marker" in block and "ReqTransfer" in block
    assert "Add a recurring marker only." in block


def test_ratified_scope_block_empty_on_legacy_flow():
    assert _ratified_scope_block("cr1", _FakeDB(None, None)) == ""
