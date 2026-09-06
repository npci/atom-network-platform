# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The feature taxonomy is pack data, not engine content.

`app.agents.taxonomy` used to hardcode UPI's ten payment buckets; now every
bucket (keywords, seed queries, required fields) comes from the active pack's
`feature_taxonomy:` section via `feature_taxonomy_of`. These tests pin the
seam itself:

  * a pack's buckets — not another domain's — drive classification,
  * a pack that declares NO taxonomy degrades to one generic bucket rather
    than inheriting UPI's,
  * no UPI vocabulary survives in the neutral classifier code.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.agents import taxonomy
from app.core.domain.contract import FeatureBucket, feature_taxonomy_of
from app.core.domain.registry import DEFAULT_PACK


NLLN_PACK = str(Path(DEFAULT_PACK).resolve().parents[2] / "packs" / "nlln" / "nlln.yaml")


@pytest.fixture
def pin_pack(monkeypatch):
    """Pin DOMAIN_PACK to a given YAML and restore the registry cache after."""
    from app.core.domain import registry

    def _pin(path: str):
        monkeypatch.setenv("DOMAIN_PACK", path)
        registry._load.cache_clear()

    yield _pin
    registry._load.cache_clear()


# ── The accessor ─────────────────────────────────────────────────────────────

class _NoTaxonomyPack:
    key = "bare"
    version = "0.1"

    def change_types(self):
        return []

    def artifacts(self):
        return []

    def prompt_blocks(self):
        return {}


def test_feature_taxonomy_of_absence_is_empty():
    """Omitting the method means the domain declares no taxonomy — empty, not
    an error, and never a fallback to another domain's buckets."""
    assert feature_taxonomy_of(_NoTaxonomyPack()) == ()


def test_feature_taxonomy_of_returns_declared_buckets():
    class _Pack(_NoTaxonomyPack):
        def feature_taxonomy(self):
            return [FeatureBucket(key="reservations", label="Reservations",
                                  keywords=["hold"], seed_queries=["hold flow"])]

    buckets = feature_taxonomy_of(_Pack())
    assert [b.key for b in buckets] == ["reservations"]
    assert buckets[0].required_fields == []  # optional, defaults empty


# ── get_taxonomy is pack-driven ──────────────────────────────────────────────

def test_upi_pack_taxonomy_and_fallback_default(pin_pack):
    pin_pack(DEFAULT_PACK)
    tax = taxonomy.get_taxonomy()
    assert "payment_initiation" in tax and "mandate_recurring" in tax
    # Keyword fallback with no matches defaults to the FIRST declared bucket.
    result = taxonomy._keyword_fallback("completely unrelated gibberish qqq", tax)
    assert result["primary"] == "payment_initiation"


def test_nlln_pack_taxonomy_has_no_upi_leakage(pin_pack):
    pin_pack(NLLN_PACK)
    tax = taxonomy.get_taxonomy()
    assert "loan_lifecycle" in tax and "reservation_holds" in tax
    serialized = repr(tax).lower()
    for payments_term in ("upi", "npci", "psp", "reqtransfer", "mandate", "rupay"):
        assert payments_term not in serialized, (
            f"UPI vocabulary {payments_term!r} leaked into the NLLN taxonomy"
        )
    # And the library keywords steer the fallback classifier, not UPI's.
    result = taxonomy._keyword_fallback("patron wants to renew a loan", tax)
    assert result["primary"] == "loan_lifecycle"


async def test_pack_without_taxonomy_gets_one_generic_bucket(pin_pack, tmp_path, monkeypatch):
    pack = tmp_path / "bare.yaml"
    pack.write_text("key: bare\n", encoding="utf-8")
    pin_pack(str(pack))

    tax = taxonomy.get_taxonomy()
    assert list(tax) == [taxonomy.GENERAL_BUCKET_KEY]
    assert tax["general"]["analogue_queries"] == []
    assert tax["general"]["required_fields"] == []

    # classify() short-circuits — a single bucket needs no LLM call.
    def _boom(*a, **k):
        raise AssertionError("classify() must not call the LLM for a 1-bucket taxonomy")

    monkeypatch.setattr(taxonomy, "call_llm", _boom)
    result = await taxonomy.classify("add a shiny new feature")
    assert result["primary"] == "general"
    assert result["source"] == "domain_pack"
    assert result["bucket"]["analogue_queries"] == []
    assert taxonomy.get_analogue_queries(result) == []
    assert taxonomy.get_required_fields(result) == []


# ── Neutrality of the classifier code itself ─────────────────────────────────

def test_classifier_prompt_is_domain_neutral():
    """The system prompt template must carry no domain vocabulary — the domain
    arrives via the pack's bucket labels and the domain_name prompt block."""
    for term in ("UPI", "NPCI", "payment"):
        assert term not in taxonomy._CLASSIFY_SYSTEM
