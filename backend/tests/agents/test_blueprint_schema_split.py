# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Two live document schemas — validation must not warn on the wrong one.

`app.agents.blueprints` drives the markdown/agents generator;
`app.docgen.document_guides` drives the LangGraph .docx pipeline. For BRD the
two share ZERO section headings. Before this was fixed, every docgen-generated
BRD reported 12 of 14 required sections missing — a permanent false warning,
which is worse than no warning because it trains reviewers to ignore the check.

These tests pin the containment fix. They will need revisiting when the pack
contract decides whether one document schema or two is authoritative
(docs/genericization/04-target-architecture.md §8.3) — that is the point.
"""
import pytest

from app.agents import blueprints as agents_bp
from app.agents.document_validator import _blueprint_section_issues
from app.docgen import document_guides as docgen_bp


def _render(blueprint) -> str:
    """A document that follows `blueprint` exactly."""
    return "\n\n".join(
        f"## {s['heading']}\n\nBody text for this section."
        for s in blueprint["sections"]
    )


def test_the_two_brd_schemas_really_do_diverge():
    """Guards the premise. If these ever converge, the fix below is dead weight
    and should be removed rather than left to rot."""
    a = {s["heading"].lower() for s in agents_bp.get("brd")["sections"]}
    d = {s["heading"].lower() for s in docgen_bp.get_document_blueprint("brd")["sections"]}
    assert a and d
    assert not (a & d), "schemas converged — revisit _candidate_blueprints()"


@pytest.mark.parametrize("doc_type", ["brd", "tech_spec", "canvas", "xsd"])
def test_agents_shaped_document_validates_clean(doc_type):
    bp = agents_bp.get(doc_type)
    assert bp, doc_type
    assert _blueprint_section_issues(_render(bp), doc_type) == []


def test_docgen_shaped_brd_validates_clean():
    """The regression this fix exists for."""
    bp = docgen_bp.get_document_blueprint("brd")
    assert bp
    assert _blueprint_section_issues(_render(bp), "brd") == []


def test_document_matching_neither_schema_still_warns():
    """The true positive must survive. A validator that never fires is not a
    validator — the fix must remove false positives only."""
    issues = _blueprint_section_issues("Prose with no headings at all. " * 20, "brd")
    assert len(issues) == 1
    assert issues[0]["rule"] == "missing_blueprint_sections"
    assert issues[0]["severity"] == "warning"


def test_unknown_doc_type_is_not_an_error():
    assert _blueprint_section_issues("anything", "not-a-doc-type") == []
