# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The shipped example governance skills must stay loadable.

backend/examples/governance_skills/ ships one generic EA and one generic
InfoSec rulebook so the pre-build review stages work out of the box (uploaded
via the admin UI or seeded by scripts/seed_governance_skills.py). If either
file drifts out of the parser contract, the failure would otherwise surface
only at upload/seed time on a demo host — pin it here instead.
"""
from __future__ import annotations

import pathlib

import pytest

from app.agents.governance_skills import parse_skill, validate_skill

EXAMPLES = pathlib.Path(__file__).resolve().parents[2] / "examples" / "governance_skills"
FILES = {
    "ea":      EXAMPLES / "ea_review_skill.md",
    "infosec": EXAMPLES / "infosec_review_skill.md",
}


@pytest.mark.parametrize("stype", sorted(FILES))
def test_example_skill_parses_with_explicit_rules(stype):
    content = FILES[stype].read_text(encoding="utf-8")
    summary = validate_skill(content)
    assert summary["mode"] == "rule_headings", \
        "the examples demonstrate the strongest (per-rule) enforcement mode"
    assert len(summary["rules"]) >= 10
    assert summary["name"], "frontmatter name drives the skill slot — must be set"
    ids = [r["id"] for r in summary["rules"]]
    assert len(ids) == len(set(ids))


def test_example_skills_use_distinct_slots():
    names = {validate_skill(p.read_text(encoding="utf-8"))["name"] for p in FILES.values()}
    assert len(names) == 2


@pytest.mark.parametrize("stype", sorted(FILES))
def test_example_rule_bodies_are_verdictable_units(stype):
    parsed = parse_skill(FILES[stype].read_text(encoding="utf-8"))
    for rule in parsed["rules"]:
        body = rule.body
        assert body.splitlines()[0].startswith("## RULE ")
        assert len(body) > 120, f"rule {rule.id} is too thin to judge a diff against"
        assert "FAIL" in body, f"rule {rule.id} never says what a FAIL looks like"


@pytest.mark.parametrize("stype", sorted(FILES))
def test_examples_stay_generic(stype):
    """The point of the examples: no application/domain vocabulary baked in."""
    text = FILES[stype].read_text(encoding="utf-8").lower()
    for word in ("npci", "upi", "nlln", "escrow", "vpa"):
        assert word not in text, f"example {stype} skill mentions {word!r} — keep it generic"
