# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Domain nouns in the shared prompt blocks are substitution points.

Two things are pinned here:

1. **Byte-identity of the defaults.** `prompt_blocks` docstring: the blocks are
   marked `cache_control={"type": "ephemeral"}` for Anthropic prompt caching,
   and a cached segment must be the SAME BYTES across requests or the cache
   never hits. A whitespace change here is a silent cost regression, not a
   cosmetic one — so the expected text is frozen literally below rather than
   derived from the module (deriving it from the thing under test would assert
   nothing).

2. **That an alternate domain can actually render.** The API-deprecation
   reference domain has no regulator, no circulars and no PSPs; if the blocks
   cannot express that, they are not parameterised, only decorated.
"""
from app.core.prompt_blocks import (
    CITATION_RULES,
    DEFAULT_TERMS,
    MARKDOWN_RULES,
    PROD_OUTPUT_RULES,
    active_pack_terms,
    citation_rules,
    prod_output_rules,
)

# Frozen 2026-09-03 (domain-term neutralization pass), copied from the rendered
# default. Do not regenerate from the module — update deliberately, and expect a
# prompt-cache miss when you do.
FROZEN_CITATION_RULES = (
    "Source citation rules (when RAG evidence is provided):\n"
    "- Cite supporting Authority / Regulator evidence inline using the [S#] markers from the "
    "\"Retrieved authority corpus evidence\" section (e.g. \"PSPs must revoke within "
    "24h [S2].\").\n"
    "- Every regulatory obligation, error-code, API name, or dispute-SLA claim "
    "that has corpus support MUST carry a [S#] citation.\n"
    "- Reproduce the \"Source index\" block verbatim at the END of the document "
    "under a \"## References\" section, using only the [S#] tags actually cited "
    "in your output."
)

FROZEN_PROD_OUTPUT_FIRST_LINE = (
    "Production-grade output rules (STRICT — this is a regulated authority document):"
)


def test_citation_rules_default_is_byte_identical_to_the_frozen_text():
    assert citation_rules() == FROZEN_CITATION_RULES


def test_prod_output_rules_default_first_line_unchanged():
    assert prod_output_rules().splitlines()[0] == FROZEN_PROD_OUTPUT_FIRST_LINE


def test_constants_equal_their_active_pack_rendering():
    """The module constants are the ACTIVE pack's rendering — that is what
    agents bake into their system prompts at import."""
    assert PROD_OUTPUT_RULES == prod_output_rules(active_pack_terms())
    assert CITATION_RULES == citation_rules(active_pack_terms())


def test_upi_pack_terms_are_byte_identical_to_the_defaults():
    """The prompt-cache guarantee: the default deployment (UPI pack) must keep
    rendering EXACTLY the bytes it always has, so wiring the pack override
    cannot cost a cache miss. Loaded explicitly so this holds regardless of
    which pack the test run activates."""
    from pathlib import Path

    from app.core.domain.config_pack import load as load_config_pack

    upi_yaml = (Path(__file__).resolve().parents[2]
                / "app" / "packs" / "network" / "network.yaml")
    blocks = load_config_pack(str(upi_yaml)).prompt_blocks()
    upi_terms = {k: blocks.get(k, v) for k, v in DEFAULT_TERMS.items()}
    assert upi_terms == DEFAULT_TERMS
    assert citation_rules(upi_terms) == FROZEN_CITATION_RULES


def test_markdown_rules_is_not_parameterised():
    """It is 100% generic — no domain nouns, so no substitution points. If a
    brace ever appears here it is a bug, not a feature."""
    assert "{" not in MARKDOWN_RULES and "}" not in MARKDOWN_RULES
    for term in ("npci", "rbi", "psp", "network", "circular"):
        assert term not in MARKDOWN_RULES.lower()


def test_an_alternate_domain_renders_without_default_domain_vocabulary():
    """The API-deprecation reference domain: no regulator, no circulars, no PSPs."""
    terms = {
        "authority": "Platform Engineering",
        "evidence_sources": "internal API docs",
        "evidence_heading": "Retrieved API documentation",
        "citation_example": "the v1 endpoint sunsets on 2027-01-31 [S2].",
        "reference_kind": "changelog",
        "claim_kinds": "sunset date, replacement endpoint, or migration step",
        "document_register": "internal engineering notice",
    }
    rendered = prod_output_rules(terms) + "\n" + citation_rules(terms)
    lowered = rendered.lower()
    for leaked in ("npci", "rbi", "psp", "circular", "dispute-sla"):
        assert leaked not in lowered, f"{leaked!r} survived substitution"
    assert "Platform Engineering-convention default" in rendered
    assert "internal engineering notice" in rendered
    assert "sunsets on 2027-01-31" in rendered


def test_partial_override_falls_back_to_defaults():
    """A pack overriding one noun must not have to restate all of them."""
    rendered = citation_rules({"evidence_sources": "OCPP specification"})
    assert "OCPP specification evidence" in rendered
    assert "Retrieved authority corpus evidence" in rendered  # untouched default


def test_unknown_terms_are_ignored_rather_than_raising():
    assert citation_rules({"not_a_real_term": "x"}) == citation_rules()


def test_every_substitution_point_has_a_default():
    """A template referencing a term absent from DEFAULT_TERMS would KeyError at
    import time for every caller that does not pass it."""
    import string

    from app.core import prompt_blocks as pb

    for tmpl_name in ("_PROD_OUTPUT_RULES_T", "_CITATION_RULES_T"):
        tmpl = getattr(pb, tmpl_name)
        fields = {f for _, f, _, _ in string.Formatter().parse(tmpl) if f}
        missing = fields - set(DEFAULT_TERMS)
        assert not missing, f"{tmpl_name} uses undefined term(s): {missing}"


def test_evidence_heading_matches_what_the_rag_layer_emits():
    """The prompt tells the model to look for a section by name. If the RAG
    bridge renames that header, this block silently points at nothing."""
    from pathlib import Path

    bridge = Path(__file__).resolve().parents[2] / "app" / "docgen" / "rag_bridge.py"
    assert DEFAULT_TERMS["evidence_heading"] in bridge.read_text(encoding="utf-8")
