# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Scoring for the golden-output regression suite.

Two layers, deliberately separated by whether they cost money:

  structural — deterministic, offline, free. Blueprint section coverage plus
               the existing checks in app.services.evaluation.deterministic.
               Runs anywhere, including CI.
  semantic   — LLM judge against the golden. Costs an API call per artifact,
               needs a pinned model, and is skipped when no provider is
               configured. Never let this run silently in per-PR CI.

WHY THIS EXISTS: docs/genericization/06-migration-plan.md §1. The prompts were
tuned by pushing AWAY from generality — `agents/canvas.py` literally instructs
"Be specific to the network / the Authority — avoid generic product management boilerplate."
Genericization therefore removes quality by construction, and the loss is
invisible in code review because the diff looks like an improvement (fewer
hardcoded nouns, more parameterisation). Without a measurement harness the
regression surfaces months later as a reviewer rejecting a document, with no
baseline to say how much was lost or when.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Score:
    """A structural score plus the findings that produced it."""

    value: float                       # 0.0 – 1.0
    findings: list[str] = field(default_factory=list)
    detail: dict[str, float] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.findings

    def __str__(self) -> str:  # pragma: no cover - display only
        head = f"score={self.value:.3f}"
        if not self.findings:
            return head + " (clean)"
        return head + "\n  - " + "\n  - ".join(self.findings)


def _headings(text: str) -> set[str]:
    """Markdown headings, normalised: numbering and case stripped.

    Handles "## 1. Executive Summary", "## i. Current State" and
    "## A. The Authority — Changes" alike, because the two live document schemas number
    their sections differently (see 04-target-architecture §8.3).
    """
    out: set[str] = set()
    for raw in re.findall(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", text, flags=re.MULTILINE):
        cleaned = re.sub(r"^([0-9]+|[ivxlcIVXLC]+|[A-Za-z])[.)]\s*", "", raw.strip())
        out.add(cleaned.strip().lower())
    return out


def section_coverage(candidate: str, golden: str) -> Score:
    """Fraction of the golden's headings that appear in the candidate.

    Compared against the GOLDEN rather than a blueprint on purpose: the golden
    is what a human accepted, and it is the thing we are defending. A blueprint
    says what should have been produced; the golden says what actually was.
    """
    want = _headings(golden)
    if not want:
        return Score(1.0, ["golden has no headings — fixture is malformed"])
    have = _headings(candidate)
    missing = sorted(want - have)
    value = (len(want) - len(missing)) / len(want)
    findings = [f"missing section: {m}" for m in missing]
    return Score(value, findings, {"sections_expected": float(len(want)),
                                   "sections_missing": float(len(missing))})


def deterministic_findings(candidate: str, doc_type: str) -> list[str]:
    """Reuse the platform's own gate rather than re-implementing it.

    These are the checks production already applies, so a golden run and a real
    run disagree only about the model output, never about the rules.
    """
    from app.services.evaluation import deterministic as det

    # NOT included: det.check_mandatory_sections_present.
    #
    # It requires the literal headings "## Background", "## Functional
    # Requirements" and "## Compliance", which NEITHER BRD generator produces —
    # agents/blueprints numbers them ("2. Background & Context"), docgen uses a
    # different ontology entirely ("i. Current State"). Measured: a document
    # rendered from agents/blueprints' own BRD blueprint yields 3 findings; from
    # docgen's, 2. Those findings classify as MISSING_MANDATORY_SECTION, which
    # is listed in hard_fail_codes for the BRD contract (contracts.py:63,527).
    #
    # The eval gate is invoked advisorily (runner.run_advisory /
    # fire_advisory_eval), so it does not block — but every BRD carries three
    # permanent false hard-fails, which is the same "always wrong, therefore
    # ignored" failure PR #6 fixed for the section validator.
    #
    # Including it here would make every golden fail for a reason unrelated to
    # what the suite measures. Excluded deliberately, not silently — see
    # docs/GOLDEN_OUTPUTS.md and OQ-10.
    artifact = {"content": candidate, "type": doc_type}
    findings: list[str] = []
    for check in (det.check_no_placeholders,
                  det.check_no_internal_markers,
                  det.check_fr_numbering_pattern):
        try:
            findings.extend(check(artifact))
        except Exception as exc:  # noqa: BLE001 — one broken check must not
            findings.append(f"check {check.__name__} errored: {exc}")  # hide the rest
    return findings


def specificity(candidate: str, terms: list[str]) -> Score:
    """Fraction of domain terms from the golden that survive in the candidate.

    This is the layer that catches the failure mode genericization actually
    causes. A document can keep every heading and still degrade into generic
    product-management prose once the "be specific" instruction is diluted —
    section coverage would score that 1.0. Measuring domain-term density is a
    crude but honest proxy for "does this still read as a document about THIS
    ecosystem".
    """
    if not terms:
        return Score(1.0, [], {"terms_expected": 0.0})
    # Collapse whitespace before matching. Markdown bodies are hard-wrapped, so
    # a multi-word term routinely straddles a newline ("a technical\ndecline").
    # The first draft of this function matched raw substrings and scored its own
    # golden 0.857 for exactly that reason — the meta-test caught it.
    lc = re.sub(r"\s+", " ", candidate.lower())

    def present(entry) -> bool:
        """A term counts if ANY accepted form appears.

        An entry may be a plain string or a list whose first element is the
        canonical form and the rest are accepted alternates. This exists because
        the first real capture (2026-08-10) scored 0.429 while genuinely
        covering the concepts: the agent wrote the network's own abbreviation, "classified
        as BD", where the fixture demanded the spelled-out "business decline".
        Literal matching under-reports specificity exactly when the model is MOST
        fluent in the domain, which inverts what this score is for.
        """
        forms = [entry] if isinstance(entry, str) else list(entry)
        for f in forms:
            f = re.sub(r"\s+", " ", str(f).lower())
            # Short forms are abbreviations; require word boundaries so "bd"
            # cannot match inside an unrelated word.
            if len(f) <= 3:
                if re.search(rf"\b{re.escape(f)}\b", lc):
                    return True
            elif f in lc:
                return True
        return False

    def canonical(entry) -> str:
        return entry if isinstance(entry, str) else str(entry[0])

    missing = sorted({canonical(t) for t in terms if not present(t)})
    value = (len(terms) - len(missing)) / len(terms)
    findings = [f"domain term absent: {m}" for m in missing]
    return Score(value, findings, {"terms_expected": float(len(terms)),
                                   "terms_missing": float(len(missing))})


def structural_score(candidate: str, golden: str, doc_type: str,
                     terms: list[str] | None = None) -> Score:
    """Combined offline score. Free, deterministic, safe for CI."""
    cov = section_coverage(candidate, golden)
    spec = specificity(candidate, terms or [])
    det_findings = deterministic_findings(candidate, doc_type)

    # Equal weight on structure and specificity; deterministic findings are a
    # penalty rather than a component, because one placeholder is a defect
    # regardless of how well the rest scores.
    penalty = min(0.25, 0.05 * len(det_findings))
    value = max(0.0, (cov.value + spec.value) / 2 - penalty)

    return Score(
        value=value,
        findings=cov.findings + spec.findings + det_findings,
        detail={"section_coverage": cov.value,
                "specificity": spec.value,
                "deterministic_penalty": penalty,
                **cov.detail, **spec.detail},
    )


JUDGE_PROMPT = """You are grading a generated document against an accepted reference.

Score 0.0-1.0 on how well the CANDIDATE preserves what makes the REFERENCE
useful. Judge substance, not wording — the candidate may phrase things
differently and still be equivalent.

Penalise heavily:
- specific commitments in the reference (limits, codes, obligations, named
  actors, concrete steps) that the candidate has replaced with vague or generic
  statements
- requirements that lost their actor ("the system shall X" -> "X will be done")
- sections that survived as headings but lost their content

Do NOT penalise: different ordering, different phrasing, extra detail that does
not contradict the reference.

Reply with ONLY a JSON object:
{"score": <0.0-1.0>, "reasons": ["<short finding>", ...]}

REFERENCE:
---
%(golden)s
---

CANDIDATE:
---
%(candidate)s
---"""


def semantic_score(candidate: str, golden: str, *, model: str) -> Score | None:
    """LLM-judge comparison against the golden. None when unavailable.

    Returns None rather than raising or scoring 0 when no provider is
    configured: an absent judge is "not measured", and recording that as
    "measured and bad" — or as "measured and fine" — hides regressions in
    opposite directions. The caller must distinguish the three.

    `model` MUST be pinned by the caller. A judge-model upgrade mid-migration
    silently reindexes every baseline, and the shift reads as a quality change
    in your own code.

    Note this is NOT app.services.evaluation.judge.judge_advisory — that
    combines deterministic findings into a verdict and says so itself ("critic
    model not yet enabled"). It never compares two documents, which is the only
    question a golden suite asks.
    """
    import asyncio
    import json
    import re

    from app.core.config import settings

    provider = (getattr(settings, "llm_provider", "") or "").strip().lower()
    if not provider:
        return None

    from app.core.llm import call_llm

    prompt = JUDGE_PROMPT % {"golden": golden[:20000], "candidate": candidate[:20000]}
    try:
        raw = asyncio.run(call_llm(
            system="You are a precise grader. Reply with JSON only.",
            messages=[{"role": "user", "content": prompt}],
            model=model,          # pinned by the caller — never get_model()
            max_tokens=1000,
            agent_name="golden_judge",
        ))
    except Exception as exc:  # noqa: BLE001 — an unreachable judge is "not
        return Score(0.0, [f"judge unavailable: {exc}"],  # measured", not a
                     {"judge_error": 1.0})                # verdict on the doc

    text = raw if isinstance(raw, str) else str(raw)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return Score(0.0, [f"judge returned unparseable output: {text[:200]}"],
                     {"judge_error": 1.0})
    try:
        data = json.loads(m.group(0))
        value = float(data.get("score", 0.0))
    except (ValueError, TypeError) as exc:
        return Score(0.0, [f"judge JSON invalid: {exc}"], {"judge_error": 1.0})

    reasons = [str(r) for r in (data.get("reasons") or [])][:10]
    return Score(max(0.0, min(1.0, value)), reasons, {"judge_model_pinned": 1.0})
