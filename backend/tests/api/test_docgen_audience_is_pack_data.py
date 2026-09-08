# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""A generated circular's addressees come from the pack, never from this repo.

`_docgen_audience` fed the document agent a hardcoded "Member Banks, PSPs,
TPAPs" for every circular, on every pack, while `docgen/agents/pipeline.py` read
the pack's own `docgen_circular_addressees` for the same purpose. The two
disagreed and the hardcoded payments list is what arrived as `audience`.

It surfaced exactly where the ADCN aviation fixture says to look. A CAAB
airworthiness circular was generated addressed to "All Member Banks, Payment
Service Providers (PSPs) and Third-Party Application Providers (TPAPs)
operating under or interfacing with the ADCN airworthiness compliance
framework." That fixture's README states the acceptance criterion plainly: if a
payments term shows up in a generated artifact, it came from the platform.

This is a leak a green test suite cannot catch, because the suite runs the
payments pack — where the hardcoded string happens to be right. So these tests
assert on a NON-payments pack specifically.
"""
from __future__ import annotations

import pytest

from app.api.agents import _docgen_audience

# Tokens that must never reach a non-payments deployment's generated document.
PAYMENTS_TERMS = ("bank", "psp", "tpap", "payment service provider", "upi", "npci")


@pytest.fixture
def aviation_pack(tmp_path, monkeypatch):
    """A pack whose addressees share no vocabulary with the payments one."""
    from app.core.domain import registry

    pack = tmp_path / "aviation.yaml"
    pack.write_text(
        "key: aviation_probe\n"
        "prompt_blocks:\n"
        "  docgen_circular_addressees: 'All certificated operators on the network.'\n"
    )
    monkeypatch.setenv("DOMAIN_PACK", str(pack))
    registry._load.cache_clear()
    try:
        yield
    finally:
        registry._load.cache_clear()


def test_circular_audience_comes_from_the_pack(aviation_pack):
    audience = _docgen_audience("circular")
    assert "certificated operators" in audience, (
        f"the pack's addressees were ignored; got {audience!r}")


def test_circular_audience_carries_no_payments_vocabulary(aviation_pack):
    """The regression. Fails pre-fix on every one of these tokens."""
    lowered = _docgen_audience("circular").lower()
    leaked = [t for t in PAYMENTS_TERMS if t in lowered]
    assert not leaked, (
        f"payments vocabulary {leaked} reached a non-payments deployment's "
        "circular — this is the platform imposing its own domain on a "
        "generated artifact")


def test_non_circular_audience_is_domain_free(aviation_pack):
    """Other documents address internal readers and need no pack lookup.

    Pinned so nobody 'helpfully' routes these through the pack too: their
    audience carries no domain vocabulary to leak in the first place.
    """
    audience = _docgen_audience("BRD").lower()
    assert not [t for t in PAYMENTS_TERMS if t in audience]
    assert "product managers" in audience


def test_a_pack_declaring_no_addressees_gets_a_neutral_default(tmp_path, monkeypatch):
    """Absent the key, the fallback must not reintroduce a domain."""
    from app.core.domain import registry

    pack = tmp_path / "bare.yaml"
    pack.write_text("key: bare_probe\n")
    monkeypatch.setenv("DOMAIN_PACK", str(pack))
    registry._load.cache_clear()
    try:
        lowered = _docgen_audience("circular").lower()
        assert not [t for t in PAYMENTS_TERMS if t in lowered], (
            "the fallback smuggles payments vocabulary back in")
    finally:
        registry._load.cache_clear()
