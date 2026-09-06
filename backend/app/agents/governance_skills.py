# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Governance skill files (EA / InfoSec rulebooks) — pure parsing/injection helpers.

Accepts the industry-standard SKILL.md shape (Claude Code / grok-build / Codex:
optional YAML frontmatter + free markdown body) and derives the enforceable
units from whatever structure the document has — three modes, strongest first:

    rule_headings   ## RULE EA-01: <title>   → explicit per-rule enforcement
    sections        ## <any heading>          → every level-2 section is a unit
    whole_document  (no headings)             → the whole body is ONE unit

Everything before the first heading (after frontmatter) is the PREAMBLE
(definitions, glossary, scope notes) and is included with every review pass.

Loading is deterministic and complete or fails loud (ValueError) — governance
rules must never be silently dropped. Deliberately rejected behaviours from
the grok-build reference: filesystem discovery (DB row is the single source),
silent-empty-on-timeout, and budget truncation. Large skills scale by RULE
SHARDING (deterministic batches of whole rules, union == full rule set by
construction), never by similarity retrieval — an un-retrieved rule is a rule
that silently never gets checked.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.agents._prompt_safety import wrap_untrusted

SKILL_TYPES = ("ea", "infosec")

# Rules per reviewer pass. One directive per rule; ~25 keeps the per-pass verdict
# list comfortably inside the reviewer output cap even on AiNxt (no finish_reason).
_RULES_PER_BATCH = 25

_RULE_HEADING = re.compile(
    # Separator is ':', an em/en dash, or a SPACED '-': a bare hyphen would
    # backtrack into the id ('## RULE EA-01 Title' parsing as id 'EA'), and two
    # colon-less rules sharing a prefix then reject as duplicate id 'EA'.
    r"^##\s*RULE\s+([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:[:—–]|\s-)\s*(.+?)\s*$",
    re.MULTILINE,
)

# Anything that LOOKS like a rule heading (any depth/case) — used in
# rule_headings mode to reject near-misses loudly instead of silently folding
# them into the previous rule's body.
_RULE_ATTEMPT = re.compile(r"^#{1,6}\s*RULE\b.*$", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class SkillRule:
    id: str
    title: str
    body: str  # full markdown section including the heading line


def checksum(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Split optional ``---`` YAML frontmatter from the body.

    Tolerant scalar-only parser (``key: value`` lines) — governance authors are
    not engineers and a stray colon must not reject the upload; the rules, not
    the frontmatter, are the contract. Returns ({}, content) when absent.
    """
    if not content.startswith("---"):
        return {}, content
    end = re.search(r"^---\s*$", content[3:], re.MULTILINE)
    if not end:
        return {}, content
    raw = content[3:3 + end.start()]
    if _RULE_HEADING.search(raw) or _H2_HEADING.search(raw) or _RULE_ATTEMPT.search(raw):
        # A leading '---' followed by enforceable headings — OR a NEAR-MISS rule heading
        # (F9) — before the next '---' is a horizontal RULE, not frontmatter. Treating it
        # as frontmatter would silently swallow every rule/section (and hide the near-miss
        # from the loud-reject scan) inside the region.
        return {}, content
    body = content[3 + end.end():].lstrip("\n")
    meta: dict = {}
    for line in raw.splitlines():
        if ":" not in line or not line.strip() or line.lstrip().startswith("#"):
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower().replace("-", "_")
        if key:
            meta[key] = val.strip().strip("\"'")
    return meta, body


def parse_rules(content: str) -> tuple[str, list[SkillRule]]:
    """Extract (preamble, rules) from the frontmatter-stripped body.

    Compatibility wrapper over :func:`parse_skill` — see it for the three
    parsing modes. Raises ValueError on a genuinely broken skill (empty body,
    duplicate explicit rule ids)."""
    parsed = parse_skill(content)
    return parsed["preamble"], parsed["rules"]


# Generic level-2 section heading (the industry SKILL.md shape — Claude Code /
# grok-build / Codex skills are frontmatter + free markdown, usually organised
# under '## Section' headings). '## RULE …' lines are handled by the mode above.
_H2_HEADING = re.compile(r"^##\s+(?!RULE\b)(.+?)\s*$", re.MULTILINE)


def _slug(title: str, seen: set[str]) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "section"
    base, n = s, 2
    while s in seen:
        s = f"{base}-{n}"
        n += 1
    seen.add(s)
    return s


def parse_skill(content: str) -> dict:
    """Parse a skill in whichever of the three supported shapes it uses.

    Mode 1 — ``rule_headings``: explicit ``## RULE <id>: <title>`` lines (the
      platform's native convention; strongest per-rule enforcement). Duplicate
      ids (case-insensitive) reject the upload.
    Mode 2 — ``sections``: the industry-standard SKILL.md shape (Claude Code /
      grok-build / Codex): frontmatter + markdown organised under ``##``
      headings. EVERY level-2 section becomes one enforceable unit (id =
      slugified heading) — nothing is skipped, so a requirement hiding in an
      'Examples' section is still enforced.
    Mode 3 — ``whole_document``: no headings at all (pure prose) — the entire
      body is ONE binding unit ('apply this skill completely'). Weakest
      granularity, still verbatim-enforced.

    Only a genuinely broken skill rejects: empty body, or duplicate explicit
    rule ids. Returns {mode, preamble, rules}.
    """
    meta, body = parse_frontmatter(content)
    if not body.strip():
        raise ValueError("skill body is empty — nothing to enforce")

    rule_matches = list(_RULE_HEADING.finditer(body))
    rule_starts = {m.start() for m in rule_matches}
    # A heading that ALMOST parses (h3 depth, lowercase 'Rule', missing separator) would
    # silently fold into the previous rule's body — or, when NO rule parsed at all, degrade
    # the whole document into a single whole_document unit (F9). Fail loud in BOTH cases:
    # this scan runs before mode selection, not only when a valid rule already matched.
    near_miss = [am.group(0)[:80] for am in _RULE_ATTEMPT.finditer(body)
                 if am.start() not in rule_starts]
    if near_miss:
        raise ValueError("; ".join(
            f"heading looks like a rule but does not parse: {h!r} — use "
            "'## RULE <id>: <title>' (two #'s, uppercase RULE, ':' after the id)"
            for h in near_miss[:5]))
    if rule_matches:
        problems: list[str] = []
        seen: dict[str, str] = {}
        rules: list[SkillRule] = []
        for i, m in enumerate(rule_matches):
            rid, title = m.group(1), m.group(2)
            key = rid.lower()
            if key in seen:
                problems.append(f"duplicate rule id {rid!r} (first used as {seen[key]!r})")
            seen.setdefault(key, rid)
            end = rule_matches[i + 1].start() if i + 1 < len(rule_matches) else len(body)
            rules.append(SkillRule(id=rid, title=title, body=body[m.start():end].rstrip()))
        if problems:
            raise ValueError("; ".join(problems))
        return {"mode": "rule_headings", "preamble": body[: rule_matches[0].start()].strip(),
                "rules": rules}

    h2_matches = list(_H2_HEADING.finditer(body))
    if h2_matches:
        slugs: set[str] = set()
        rules = []
        for i, m in enumerate(h2_matches):
            title = m.group(1)
            end = h2_matches[i + 1].start() if i + 1 < len(h2_matches) else len(body)
            rules.append(SkillRule(id=_slug(title, slugs), title=title,
                                   body=body[m.start():end].rstrip()))
        return {"mode": "sections", "preamble": body[: h2_matches[0].start()].strip(),
                "rules": rules}

    title = (meta.get("name") or "").strip()
    if not title:
        first = next((ln.strip().lstrip("#").strip() for ln in body.splitlines() if ln.strip()), "")
        title = first[:80] or "entire skill document"
    return {"mode": "whole_document", "preamble": "",
            "rules": [SkillRule(id="SKILL", title=title, body=body.strip())]}


def validate_skill(content: str) -> dict:
    """Upload-time validation. Returns the parsed summary or raises ValueError."""
    meta, _ = parse_frontmatter(content)
    parsed = parse_skill(content)
    return {
        "name": meta.get("name", ""),
        "description": meta.get("description", ""),
        "mode": parsed["mode"],
        "rules": [{"id": r.id, "title": r.title} for r in parsed["rules"]],
        "checksum": checksum(content),
    }


def shard_rules(rules: list[SkillRule], per_batch: int = _RULES_PER_BATCH) -> list[list[SkillRule]]:
    """Deterministic whole-rule batches; union == input by construction."""
    return [rules[i:i + per_batch] for i in range(0, len(rules), per_batch)] or [[]]


def build_skill_block(content: str, stage: str) -> str:
    """Full skill, verbatim, in the untrusted-data envelope. Never truncated."""
    return wrap_untrusted(content, f"GOVERNANCE_SKILL_{stage.upper()}")


def build_batch_block(preamble: str, batch: list[SkillRule], stage: str,
                      batch_idx: int, total_batches: int) -> str:
    """One sharded pass's skill text: preamble + this batch's rules, verbatim.

    Single-batch skills should use build_skill_block (the whole document,
    including frontmatter, reaches the model untouched).
    """
    parts = []
    if preamble:
        parts.append(preamble)
    parts.extend(r.body for r in batch)
    label = f"GOVERNANCE_SKILL_{stage.upper()}_RULES_BATCH_{batch_idx + 1}_OF_{total_batches}"
    return wrap_untrusted("\n\n".join(parts), label)
