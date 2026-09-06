# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Unit tests for deterministic XSD namespace canonicalization (§7.4).

The equivalence groups are pack data (`schema_namespaces`), so the UPI-split
assertions pin DOMAIN_PACK to the UPI pack explicitly instead of assuming it;
the no-groups behaviour is asserted against the NLLN pack, which declares none.
"""
from pathlib import Path

import pytest

from app.agents.xsd_namespace import (
    canonicalize_namespace,
    namespace_variant_note,
    same_namespace,
    sibling_namespace_spellings,
)

_PACKS = Path(__file__).resolve().parents[2] / "app" / "packs"

CANON = "http://example.org/network/schema/"          # majority / de-facto production
VARIANT = "http://www.example.net/network/schema/"  # ApiName.xsd, TransactionResult.xsd


@pytest.fixture
def upi_pack(monkeypatch):
    monkeypatch.setenv("DOMAIN_PACK", str(_PACKS / "network" / "network.yaml"))
    from app.core.domain import registry
    registry._load.cache_clear()
    yield
    registry._load.cache_clear()


@pytest.fixture
def nlln_pack(monkeypatch):
    monkeypatch.setenv("DOMAIN_PACK", str(_PACKS / "nlln" / "nlln.yaml"))
    from app.core.domain import registry
    registry._load.cache_clear()
    yield
    registry._load.cache_clear()


def test_two_real_spellings_are_equivalent(upi_pack):
    assert same_namespace(CANON, VARIANT)
    assert canonicalize_namespace(CANON) == canonicalize_namespace(VARIANT)


def test_trailing_slash_and_whitespace_normalized(upi_pack):
    assert same_namespace("http://example.org/network/schema", "  http://example.org/network/schema/  ")


def test_only_minority_spelling_is_flagged(upi_pack):
    assert namespace_variant_note(CANON) is None
    assert namespace_variant_note(VARIANT) is not None
    assert "do NOT auto-rewrite" in namespace_variant_note(VARIANT)


def test_unknown_namespace_untouched_no_false_equivalence(upi_pack):
    assert canonicalize_namespace("http://example.com/x") == "http://example.com/x"
    assert namespace_variant_note("http://example.com/x") is None
    # a different NPCI product is NOT merged with UPI
    assert not same_namespace(CANON, "http://example.org/netc/schema/")


def test_sibling_spellings(upi_pack):
    sibs = set(sibling_namespace_spellings(VARIANT))
    assert sibs == {"http://example.org/network/schema", "http://www.example.net/network/schema"}
    assert sibling_namespace_spellings("http://example.com/x") == ["http://example.com/x"]
    assert sibling_namespace_spellings(None) == []


def test_none_and_empty_safe(upi_pack):
    assert canonicalize_namespace(None) == ""
    assert same_namespace(None, "")
    assert namespace_variant_note(None) is None


def test_pack_without_groups_matches_raw_strings(nlln_pack):
    """A pack that declares no `schema_namespaces` must NOT inherit another
    ecosystem's equivalences — the UPI split is two different namespaces here."""
    assert not same_namespace(CANON, VARIANT)
    assert namespace_variant_note(VARIANT) is None
    assert sibling_namespace_spellings(VARIANT) == [VARIANT.rstrip("/")]
