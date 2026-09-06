# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for ambiguity_detector's required_fields floor.

The detector's floor logic (line ~127 of ambiguity_detector.py) adds any
required_field NOT present in proposals as a critical gap.

The 5 v1 scope-signal keys used to ride that mechanism on purpose, so the PM
was asked to confirm parties/operations/risk/compliance before cert test cases
were generated. That consumer is gone (the cert engine is BRD/TSD-only and
`clarification_loader.get_scope_signals` has no production caller), so the keys
were removed from every taxonomy bucket and the tests below now pin their
ABSENCE — a re-add would silently reintroduce a pack-sized batch of closed-set
questions that nothing reads.
"""
from __future__ import annotations

import pytest

from app.agents.ambiguity_detector import _field_present_in_proposals
from app.agents.taxonomy import get_required_fields, get_taxonomy


_V1_SIGNAL_KEYS = {
    "certifying_parties",
    "feature_operations",
    "risk_profile",
    "compliance_sensitivity",
    "scope_error_codes",
}


@pytest.fixture
def upi_pack(monkeypatch):
    """Pin the UPI pack — the taxonomy is pack data now, and these tests pin
    what the UPI pack's buckets must (not) declare."""
    from app.core.domain import registry

    monkeypatch.setenv("DOMAIN_PACK", registry.DEFAULT_PACK)
    registry._load.cache_clear()
    try:
        yield
    finally:
        registry._load.cache_clear()


class TestTaxonomyCarriesNoScopeSignals:
    """No bucket may list a scope-signal key: they generate PM questions whose
    answers have no consumer. See the module docstring."""

    def test_no_bucket_lists_a_scope_signal_key(self, upi_pack):
        offenders = {
            bucket: sorted(_V1_SIGNAL_KEYS & set(spec["required_fields"]))
            for bucket, spec in get_taxonomy().items()
            if _V1_SIGNAL_KEYS & set(spec["required_fields"])
        }
        assert offenders == {}, f"scope-signal keys back in required_fields: {offenders}"


class TestFloorMechanismStillWorks:
    """The floor itself is unchanged — only its scope-signal payload is gone.
    ambiguity_detector.py:_field_present_in_proposals is a heuristic that checks
    whether the field name appears in the proposals JSON blob; an empty proposals
    blob still drops every required field through to a critical gap.
    """

    def test_absent_field_is_not_present_in_empty_proposals(self):
        assert _field_present_in_proposals("transaction_limit", {}) is False

    def test_get_required_fields_excludes_signals(self, upi_pack):
        """The helper the detector calls no longer returns any signal key, so the
        floor cannot manufacture a scope-signal question."""
        rf = get_required_fields({"primary": "payment_initiation"})
        assert rf, "payment_initiation should still declare real required fields"
        assert not (_V1_SIGNAL_KEYS & set(rf))
