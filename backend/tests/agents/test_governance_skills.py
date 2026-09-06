# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Governance skill parsing / sharding / injection — pure, no DB.

The load-bearing properties: rules parse deterministically or the upload is
REJECTED (never reinterpreted at review time); sharding is complete by
construction (union == rule set, no overlap); injection is verbatim and never
truncated (the grok budget-truncation failure mode, deliberately rejected).
"""
import pytest

from app.agents.governance_skills import (
    SkillRule, build_batch_block, build_skill_block, checksum,
    parse_frontmatter, parse_rules, shard_rules, validate_skill,
)

DOC = """---
name: EA Governance
description: Enterprise architecture rules
---
# Preamble
Shared definitions.

## RULE EA-01: All external calls go through the ESB adapter
Rationale one.

## RULE EA-02 — Naming follows the platform standard
Rationale two.
"""


def test_frontmatter_parses_scalars_and_strips_body():
    meta, body = parse_frontmatter(DOC)
    assert meta["name"] == "EA Governance"
    assert meta["description"].startswith("Enterprise")
    assert body.startswith("# Preamble")


def test_frontmatter_absent_is_not_an_error():
    meta, body = parse_frontmatter("## RULE X-1: t\nb")
    assert meta == {} and body.startswith("## RULE")


def test_rules_parse_with_bodies_and_preamble():
    pre, rules = parse_rules(DOC)
    assert pre.startswith("# Preamble")
    assert [r.id for r in rules] == ["EA-01", "EA-02"]
    assert rules[0].body.startswith("## RULE EA-01")
    assert "Rationale one" in rules[0].body and "EA-02" not in rules[0].body


def test_empty_body_rejected_loudly():
    with pytest.raises(ValueError, match="empty"):
        parse_rules("---\nname: x\n---\n   \n")


def test_duplicate_rule_ids_rejected_case_insensitively():
    with pytest.raises(ValueError, match="duplicate rule id"):
        parse_rules("## RULE X-1: a\nb\n## RULE x-1: c\nd")


# ── standard SKILL.md compatibility (Claude Code / grok-build / Codex shape) ──

STD = """---
name: secure-code-review
description: Review code for security issues
---
Use when reviewing auth or payment changes.

## When to use
Any diff touching auth or payments.

## Instructions
1. Validate every input.

## Examples
A hardcoded key is a blocker.
"""


def test_standard_skill_md_parses_in_sections_mode():
    from app.agents.governance_skills import parse_skill
    p = parse_skill(STD)
    assert p["mode"] == "sections"
    assert [r.id for r in p["rules"]] == ["when-to-use", "instructions", "examples"]
    assert p["rules"][1].body.startswith("## Instructions")
    assert p["preamble"].startswith("Use when reviewing")


def test_pure_prose_parses_as_whole_document():
    from app.agents.governance_skills import parse_skill
    p = parse_skill("---\nname: Data Policy\n---\nNever log full account numbers.")
    assert p["mode"] == "whole_document"
    assert p["rules"][0].id == "SKILL" and p["rules"][0].title == "Data Policy"
    assert "Never log" in p["rules"][0].body


def test_rule_headings_take_precedence_in_mixed_docs():
    from app.agents.governance_skills import parse_skill
    p = parse_skill("## Overview\nprose\n## RULE R-1: the real rule\nbody")
    assert p["mode"] == "rule_headings" and [r.id for r in p["rules"]] == ["R-1"]


def test_section_slugs_deduplicate():
    from app.agents.governance_skills import parse_skill
    p = parse_skill("## Checks\na\n## Checks\nb\n## Checks!\nc")
    assert [r.id for r in p["rules"]] == ["checks", "checks-2", "checks-3"]


def test_validate_skill_reports_mode():
    assert validate_skill(STD)["mode"] == "sections"
    assert validate_skill(DOC)["mode"] == "rule_headings"


def test_sharding_is_complete_and_disjoint():
    doc = "\n".join(f"## RULE R-{i:02d}: t{i}\nbody{i}" for i in range(63))
    _, rules = parse_rules(doc)
    batches = shard_rules(rules)
    assert [len(b) for b in batches] == [25, 25, 13]
    flat = [r.id for b in batches for r in b]
    assert flat == [r.id for r in rules]          # union == input, order kept, no overlap
    assert len(set(flat)) == len(flat)


def test_small_skill_is_one_batch():
    _, rules = parse_rules(DOC)
    assert len(shard_rules(rules)) == 1


def test_skill_block_is_verbatim_and_enveloped():
    blk = build_skill_block(DOC, "ea")
    assert blk.startswith("----- BEGIN GOVERNANCE_SKILL_EA")
    assert DOC in blk                              # verbatim — nothing dropped or trimmed
    assert blk.rstrip().endswith("----- END GOVERNANCE_SKILL_EA -----")


def test_batch_block_carries_preamble_and_only_its_rules():
    pre, rules = parse_rules(DOC)
    blk = build_batch_block(pre, rules[:1], "infosec", 0, 2)
    assert "BATCH_1_OF_2" in blk
    assert "# Preamble" in blk and "EA-01" in blk and "EA-02" not in blk


def test_validate_skill_summary_and_checksum_stability():
    v = validate_skill(DOC)
    assert v["name"] == "EA Governance"
    assert [r["id"] for r in v["rules"]] == ["EA-01", "EA-02"]
    assert v["checksum"] == checksum(DOC) and len(v["checksum"]) == 64
    assert checksum(DOC) != checksum(DOC + " ")    # content-bound


def test_giant_rule_body_never_truncated():
    big = "## RULE B-1: big\n" + ("x" * 200_000)
    _, rules = parse_rules(big)
    blk = build_batch_block("", rules, "ea", 0, 1)
    assert "x" * 200_000 in blk


def test_shard_rules_empty_input_yields_one_empty_batch():
    assert shard_rules([]) == [[]]


def test_rule_dataclass_shape():
    r = SkillRule(id="A-1", title="t", body="## RULE A-1: t\nb")
    assert (r.id, r.title) == ("A-1", "t")


# ── F9: all-near-miss RULE documents reject (not silent whole_document) ────────

def test_all_near_miss_rule_headings_reject():
    with pytest.raises(ValueError, match="looks like a rule"):
        parse_rules("## RULE EA-01 Missing separator\nbody\n## RULE EA-02 Also broken\nmore")


def test_near_miss_inside_frontmatter_region_rejects():
    doc = "---\n## RULE EA-01 Missing separator\n---\nbody text"
    with pytest.raises(ValueError, match="looks like a rule"):
        parse_rules(doc)


def test_valid_rules_still_parse_and_lowercase_rule_still_rejected():
    _, rules = parse_rules("## RULE EA-01: ok\nbody")
    assert [r.id for r in rules] == ["EA-01"]
    with pytest.raises(ValueError, match="looks like a rule"):
        parse_rules("## rule ea-01: lowercase keyword\nbody")   # near-miss even with a colon
