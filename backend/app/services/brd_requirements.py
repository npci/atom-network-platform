# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""BRD requirement auto-segregation + v1 read helpers.

When a Product Kit version is shipped for the first time, classify the change's
BRD into mandatory / optional (+ tolerance) requirements so the negotiation
classifier can auto-reject / auto-accept partner counters from the very first
round. Idempotent: skips when requirements already exist (manual or a prior
auto-run), so it only segregates once and never clobbers the PM's edits.

v1 adds two read helpers used by the excel testcase engine's WS handler:
    get_functional_requirements(change_id, db)  — cached BRDRequirement rows
                                                  augmented with heuristic
                                                  operation + party + flow_type
                                                  tags derived from label/desc.
    get_feature_criteria(change_id, db)         — LLM pass over the latest
                                                  BRD extracting per-FR
                                                  feature-specific tag/value
                                                  criteria. NOT cached in v1
                                                  — cost is bounded by
                                                  cert_test_cases regeneration
                                                  frequency.
"""
import logging
import re

from sqlalchemy.orm import Session

from app.core.database import SessionLocal

logger = logging.getLogger(__name__)


# ── v1 read helpers ──────────────────────────────────────────────────────────

# Heuristic-tagging vocabulary, all from the active pack (genericisation
# sweep): `operation_patterns` / `party_patterns` are the pack's own regexes
# (stored as regexes, not synthesized from labels, so UPI's hand-tuned
# negative lookaheads survive byte-for-byte); `financial_operations` names the
# operations that make an FR a "financial" flow. A pack that declares none
# tags nothing and classifies everything "meta" — the planner works untagged.
# Cached per pack key: the compiled maps are re-derived only when DOMAIN_PACK
# changes (tests), not once per FR row.

from functools import lru_cache


@lru_cache(maxsize=4)
def _tagging_vocab(pack_key: str) -> tuple[dict, dict, frozenset]:
    _ = pack_key  # cache key only — the pack itself comes from the registry
    from app.core.domain.contract import (
        financial_operations_of, operation_patterns_of, party_patterns_of,
    )
    from app.core.domain.registry import get_active_pack

    pack = get_active_pack()
    op_patterns = {k: re.compile(p.pattern, re.I)
                   for k, p in operation_patterns_of(pack).items()}
    party_patterns = {k: re.compile(p.pattern, re.I)
                      for k, p in party_patterns_of(pack).items()}
    return op_patterns, party_patterns, financial_operations_of(pack)


def _heuristic_tag_fr(label: str, description: str | None) -> dict:
    """Return `{operations: [...], parties: [...], flow_type: "..."}` for a FR.

    Non-mandatory heuristic — the planner still works when tags are empty.
    """
    from app.core.domain.registry import active_pack_key

    op_patterns, party_patterns, money_ops = _tagging_vocab(active_pack_key())
    haystack = f"{label} {description or ''}"
    ops = [op for op, pat in op_patterns.items() if pat.search(haystack)]
    parties = [p for p, pat in party_patterns.items() if pat.search(haystack)]
    flow_type = "financial" if any(op in money_ops for op in ops) else "meta"
    return {
        "operations": ops,
        "parties":    parties,
        "flow_type":  flow_type,
    }


def get_functional_requirements(change_id: str, db: Session) -> list[dict]:
    """Return latest BRD's requirements augmented with heuristic operation+party+flow_type tags.

    Reads existing `BRDRequirement` rows (populated by `auto_segregate_brd_requirements`
    — an idempotent background task fired on first kit ship). If no rows exist,
    returns empty — caller falls through to legacy behaviour.

    Shape per entry:
        {
            "id":           <BRDRequirement.id>,
            "fr_id":        "FR-{index}" — synthetic since BRDRequirement has no FR-NN column
            "label":        <BRDRequirement.label>,
            "description":  <BRDRequirement.description>,
            "category":     <BRDRequirement.category>,
            "is_mandatory": <BRDRequirement.is_mandatory>,
            "operations":   list[str],   # from heuristic — may be empty
            "parties":      list[str],   # from heuristic — may be empty
            "flow_type":    "financial" | "meta",   # from heuristic
        }
    """
    from app.models.phase_c import BRDRequirement

    rows = (
        db.query(BRDRequirement)
        .filter(BRDRequirement.change_request_id == change_id)
        .order_by(BRDRequirement.created_at.asc(), BRDRequirement.id.asc())
        .all()
    )
    if not rows:
        return []

    result: list[dict] = []
    for idx, row in enumerate(rows, start=1):
        tags = _heuristic_tag_fr(row.label or "", row.description)
        result.append({
            "id":            row.id,
            "fr_id":         f"FR-{idx:02d}",   # synthetic — BRDRequirement has no native FR-NN
            "label":         row.label,
            "description":   row.description,
            "category":      row.category,
            "is_mandatory":  bool(row.is_mandatory),
            "operations":    tags["operations"],
            "parties":       tags["parties"],
            "flow_type":     tags["flow_type"],
        })
    logger.info(
        "get_functional_requirements: change=%s returned %d FR(s)",
        change_id, len(result),
    )
    return result


async def get_feature_criteria(change_id: str, db: Session) -> list[dict]:
    """Extract per-FR feature criteria from the latest BRD.

    Runs `brd_extractor.extract_brd_feature_criteria` uncached in v1. Cost is
    bounded by cert_test_cases regeneration frequency (typically 1-3 per BRD
    version). Future improvement: cache on the ChangeRequest row keyed by BRD
    version so regenerations are free.

    Returns empty list when:
      - no BRD content exists for the change,
      - the LLM extractor returns [] (BRD introduces no new tags),
      - LLM/parse failure (fail-soft).

    Callers treat empty as "generic-behaviour cases only, no new-tag prose"
    per v1 backward-compat rules.
    """
    from app.agents.brd_extractor import extract_brd_feature_criteria
    from app.models.brd import BRD

    brd = (
        db.query(BRD)
        .filter(BRD.change_request_id == change_id)
        .order_by(BRD.version.desc())
        .first()
    )
    if not brd or not (brd.content or "").strip():
        logger.info(
            "get_feature_criteria: no BRD content for change=%s - returning []",
            change_id,
        )
        return []

    criteria = await extract_brd_feature_criteria(brd.content)
    logger.info(
        "get_feature_criteria: change=%s brd_version=%s returned %d criterion(a)",
        change_id, brd.version, len(criteria),
    )
    return criteria


def reconcile_criteria_to_frs(criteria: list[dict], functional_requirements: list[dict]) -> list[dict]:
    """Remap each feature criterion's `fr_id` onto the functional-requirements' id space.

    The two extractions number FRs independently: `get_functional_requirements`
    assigns synthetic `FR-{idx:02d}` over BRDRequirement rows, while
    `extract_brd_feature_criteria` has the LLM number `fr_id` from the BRD's own
    Section-6. The planner sets each stub's `fr_ref` from the FUNCTIONAL-requirement
    ids, and the Writer resolves a criterion via `criterion_map.get(fr_ref)` — so
    without this join the criterion lookup keys two disjoint id spaces and always
    misses (the feature-grounding block silently degrades to boilerplate).

    Join precedence per criterion (canonical operation/party vocab is shared, so
    the match is exact, not fuzzy):
      1. `fr_id` already equals a functional-requirement id → keep as-is.
      2. first FR whose `operations` contains the criterion's `operation` AND
         whose `parties` contains its `responsible_party`.
      3. first FR whose `operations` contains the criterion's `operation`.
      4. no match → leave `fr_id` unchanged (Writer falls back to the FR label /
         scenario summary — no worse than before this reconciliation).

    Pure + deterministic (FR list order breaks ties). Returns a new list; inputs
    are not mutated.
    """
    if not criteria or not functional_requirements:
        return criteria
    fr_ids = {fr.get("fr_id") for fr in functional_requirements if fr.get("fr_id")}
    out: list[dict] = []
    for c in criteria:
        c = dict(c)
        if c.get("fr_id") in fr_ids:
            out.append(c)
            continue
        op = c.get("operation")
        party = c.get("responsible_party")
        match = next(
            (fr for fr in functional_requirements
             if op and op in (fr.get("operations") or [])
             and party and party in (fr.get("parties") or [])),
            None,
        ) or next(
            (fr for fr in functional_requirements
             if op and op in (fr.get("operations") or [])),
            None,
        )
        if match and match.get("fr_id"):
            c["fr_id"] = match["fr_id"]
        out.append(c)
    return out


async def auto_segregate_brd_requirements(change_id: str) -> int:
    """Background task fired on first kit ship. Returns the number of
    requirements created (0 when already configured or no BRD)."""
    from app.agents.brd_extractor import extract_brd_requirements
    from app.models.base import generate_uuid
    from app.models.brd import BRD
    from app.models.phase_c import BRDRequirement

    db = SessionLocal()
    try:
        # Idempotent: only segregate when nothing is configured yet.
        if db.query(BRDRequirement).filter(BRDRequirement.change_request_id == change_id).count() > 0:
            return 0

        brd = (
            db.query(BRD)
            .filter(BRD.change_request_id == change_id)
            .order_by(BRD.version.desc())
            .first()
        )
        if not brd or not (brd.content or "").strip():
            logger.info("BRD auto-segregation: no BRD content for change=%s — skipping", change_id)
            return 0

        extracted = await extract_brd_requirements(brd.content)
        if not extracted:
            logger.info("BRD auto-segregation: nothing extracted for change=%s", change_id)
            return 0

        created = 0
        for item in extracted:
            db.add(BRDRequirement(
                id=generate_uuid(),
                change_request_id=change_id,
                label=item["label"],
                description=item["description"],
                category=item["category"],
                is_mandatory=item["is_mandatory"],
                tolerance_config=item["tolerance_config"],
                source="ai",
                ai_rationale=item["rationale"],
            ))
            created += 1
        db.commit()
        logger.info("BRD auto-segregation: change=%s created %d requirement(s)", change_id, created)
        return created
    except Exception:
        logger.exception("BRD auto-segregation failed for change=%s", change_id)
        try:
            db.rollback()
        except Exception:
            pass
        return 0
    finally:
        db.close()
