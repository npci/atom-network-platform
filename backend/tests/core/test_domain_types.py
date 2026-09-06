# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The core/pack seam: document STRUCTURE lives in core, CONTENT does not.

These tests exist to catch the two ways this seam degrades in review:
  1. someone re-adds the TypedDicts to `agents.blueprints`, quietly forking them;
  2. someone moves blueprint CONTENT into core "while they're in there".
"""
import inspect

from app.agents import blueprints
from app.core.domain import types as domain_types


def test_types_are_defined_in_core_not_in_the_agent_module():
    # Re-exported, not redefined — a fork would make these different objects.
    assert blueprints.Blueprint is domain_types.Blueprint
    assert blueprints.Section is domain_types.Section
    assert inspect.getmodule(domain_types.Blueprint).__name__ == "app.core.domain.types"


def test_core_types_module_carries_no_domain_content():
    """core/domain must stay domain-neutral — that is the entire point of it.

    The docstring legitimately names the network-specific modules it points at, so
    check the executable source rather than the prose.
    """
    src = inspect.getsource(domain_types)
    code = src.split('"""', 2)[-1]  # strip the module docstring
    lowered = code.lower()
    for term in ("network", "npci", "psp", "reqtransfer", "issuer bank"):
        assert term not in lowered, f"domain term {term!r} leaked into core/domain/types.py"


def test_blueprints_still_expose_every_document_type():
    for doc_type in ("brd", "tech_spec", "canvas", "xsd"):
        bp = blueprints.get(doc_type)
        assert bp is not None, doc_type
        assert bp["sections"], f"{doc_type} has no sections"
        # Every section must carry the two fields the prompt and validator use.
        for section in bp["sections"]:
            assert section.get("heading"), f"{doc_type} section missing heading"
            assert section.get("key"), f"{doc_type} section missing key"


def test_unknown_document_type_returns_none():
    assert blueprints.get("not-a-doc-type") is None


def test_format_for_prompt_lists_required_headings():
    """The prompt block is what reaches the model; if it silently empties, every
    generated document loses its section scaffold and nothing else fails."""
    rendered = blueprints.format_for_prompt("brd")
    assert rendered.strip()
    for heading in blueprints.required_section_headings("brd"):
        assert heading in rendered
