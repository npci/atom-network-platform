# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Auto-repair now fixes invented-surface WARNINGS, not only blockers.

UC2 surfaced a leftover invented field (`splitGroupId`) that the consistency gate
flagged as a non-blocking warning — so it was never repaired. Warnings that assert
invented technical surface (schema / wire_message / persistence / config) are now
repairable; pure advisory warnings (omission / endpoint) stay banner-only.
"""
from app.agents.doc_consistency import (
    _is_repairable, _has_repairable, divergent_items, repair_instruction,
)

_BLOCKER = {"severity": "blocker", "kind": "wire_message", "item": "ReqSplitPay", "detail": "new msg"}
_WARN_FIELD = {"severity": "warning", "kind": "persistence", "item": "splitGroupId", "detail": "invented field"}
_WARN_SCHEMA = {"severity": "warning", "kind": "schema", "item": "splitRef", "detail": "new field"}
_WARN_OMIT = {"severity": "warning", "kind": "omission", "item": "FR-3", "detail": "dropped"}
_WARN_EP = {"severity": "warning", "kind": "endpoint", "item": "/internal/x", "detail": "new endpoint"}


def test_blocker_and_invented_surface_warnings_are_repairable():
    assert _is_repairable(_BLOCKER)
    assert _is_repairable(_WARN_FIELD)
    assert _is_repairable(_WARN_SCHEMA)


def test_advisory_warnings_are_not_repairable():
    assert not _is_repairable(_WARN_OMIT)
    assert not _is_repairable(_WARN_EP)


def test_has_repairable_true_on_invented_surface_warning_alone():
    assert _has_repairable({"findings": [_WARN_FIELD, _WARN_OMIT]})
    assert not _has_repairable({"findings": [_WARN_OMIT, _WARN_EP]})


def test_divergent_items_and_instruction_target_the_warning():
    c = {"findings": [_WARN_FIELD, _WARN_OMIT]}
    assert "splitGroupId" in divergent_items(c)
    assert "FR-3" not in divergent_items(c)          # omission left alone
    assert "splitGroupId" in repair_instruction("BRD", c)


# ── Ratified-value fidelity: drift (wrong form) + missing (dropped) ──────────
_VALUE_DRIFT = {"severity": "blocker", "kind": "value_drift", "item": "BIOAUTH",
                "doc_form": "BioAuth",
                "detail": "doc uses BioAuth, plan requires BIOAUTH"}
_VALUE_MISSING = {"severity": "blocker", "kind": "value_missing",
                  "item": "₹5000 per-transaction cap", "detail": "plan requires it; doc omits it"}


def test_value_drift_and_missing_are_repairable():
    assert _is_repairable(_VALUE_DRIFT)
    assert _is_repairable(_VALUE_MISSING)
    assert _has_repairable({"findings": [_VALUE_DRIFT]})
    assert _has_repairable({"findings": [_VALUE_MISSING]})


def test_repair_instruction_corrects_and_adds():
    instr = repair_instruction("BRD", {"findings": [_VALUE_DRIFT, _VALUE_MISSING]})
    assert "CORRECT" in instr          # fix wrong-form values (casing/spelling)
    assert "ADD" in instr              # add ratified constraints the doc omitted
    assert "BIOAUTH" in instr          # carries the plan's exact required form


def test_divergent_items_locates_value_drift_by_doc_form():
    # The doc contains the WRONG form ("BioAuth"); the repair greps for THAT to find the
    # block, not the plan's correct form ("BIOAUTH"), which is absent from the doc. This is
    # the value_drift-locate bug: locating by `item` found nothing, so the block was never
    # targeted for the fix.
    assert divergent_items({"findings": [_VALUE_DRIFT]}) == ["BioAuth"]
    # value_missing has no doc_form (the value is absent from the doc) → falls back to `item`
    # so the writer adds it; the section-targeting then misses and full-doc edit takes over.
    assert divergent_items({"findings": [_VALUE_MISSING]}) == ["\u20b95000 per-transaction cap"]
