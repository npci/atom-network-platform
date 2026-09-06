# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Feature taxonomy classifier — buckets come from the active domain pack.

Classifies a change-request's feature description into one of the buckets the
active domain pack declares (`feature_taxonomy:` in the pack YAML, read via
`feature_taxonomy_of`). Each bucket carries:

  - keywords:           used for a fallback keyword-overlap classifier
  - required_fields:    fields a complete spec MUST document for this bucket
                        (drives gap-detection / validator logic later)
  - analogue_queries:   seed queries that retrieve similar past features
                        (drives the 3-stage hybrid-search context builder;
                        declared as `seed_queries` in the pack)

Primary classification uses the LLM with a strict JSON schema; falls back to
keyword overlap if parsing fails. The pack is the single source of truth —
adding a new bucket only requires editing the pack YAML. A domain that
declares NO taxonomy gets one generic bucket (key "general", no seed queries,
no required fields): classification is then trivial and retrieval steering
simply adds nothing, rather than borrowing another domain's buckets.
"""
import logging
import re

from app.core.domain.contract import feature_taxonomy_of
from app.core.domain.registry import get_active_pack, prompt_block
from app.core.llm import call_llm
from app.core.llm_router import pick_model_for_agent
from app.core.json_recovery import parse_llm_json
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Taxonomy
# ──────────────────────────────────────────────────────────────────────────────

GENERAL_BUCKET_KEY = "general"

_GENERAL_BUCKET: dict[str, dict] = {
    GENERAL_BUCKET_KEY: {
        "label": "General",
        "keywords": [],
        "required_fields": [],
        "analogue_queries": [],
    },
}


def get_taxonomy() -> dict[str, dict]:
    """The active domain's taxonomy as `{bucket_key: bucket_config}`.

    Resolved from the pack on every call (the registry caches the pack
    itself), so a test that pins DOMAIN_PACK and clears `registry._load`
    sees the new domain's buckets without re-importing this module.

    A pack that declares no `feature_taxonomy` gets the single generic
    bucket — never another domain's list.
    """
    buckets = feature_taxonomy_of(get_active_pack())
    if not buckets:
        return {k: dict(v) for k, v in _GENERAL_BUCKET.items()}
    return {
        b.key: {
            "label": b.label,
            "keywords": list(b.keywords),
            "required_fields": list(b.required_fields),
            "analogue_queries": list(b.seed_queries),
        }
        for b in buckets
    }


# ──────────────────────────────────────────────────────────────────────────────
# Classifier
# ──────────────────────────────────────────────────────────────────────────────

# Neutral text; the domain arrives via the pack's own bucket labels (and the
# optional domain_name prompt block). No ecosystem vocabulary belongs here.
_CLASSIFY_SYSTEM = """You are a feature taxonomy classifier for {domain_name} \
change requests. Given a feature description, pick the single best primary \
bucket from this list and any applicable secondary labels.

Buckets:
{bucket_list}

Respond with ONLY a JSON object, no markdown fences, no commentary:
{{
  "primary": "<bucket_key>",
  "labels": ["<bucket_key>", ...],    // 0-3 additional bucket keys; may be empty
  "confidence": 0.0,                   // 0..1 your confidence in primary
  "rationale": "<one short sentence>"
}}

""" + ANTI_INJECTION_CLAUSE


def _bucket_list_for_prompt(taxonomy: dict[str, dict]) -> str:
    return "\n".join(
        f"  - {key}: {cfg['label']}" for key, cfg in taxonomy.items()
    )


def _tokenize(text: str) -> set[str]:
    """Lowercase + split + drop tokens shorter than 3 chars."""
    return {
        t for t in re.findall(r"[a-z0-9]+", text.lower())
        if len(t) >= 3
    }


def _default_bucket_key(taxonomy: dict[str, dict]) -> str:
    """First declared bucket — packs list their most-common bucket first."""
    return next(iter(taxonomy))


def _keyword_fallback(feature_description: str, taxonomy: dict[str, dict]) -> dict:
    """Classify by simple keyword-overlap scoring when LLM fails or is unavailable."""
    feature_tokens = _tokenize(feature_description)
    scores: dict[str, int] = {}
    for key, cfg in taxonomy.items():
        match_count = 0
        for kw in cfg["keywords"]:
            # multi-word keywords match as substring; single words as token set
            if " " in kw:
                if kw.lower() in feature_description.lower():
                    match_count += 2  # phrase match gets higher weight
            else:
                if kw.lower() in feature_tokens:
                    match_count += 1
        if match_count:
            scores[key] = match_count

    if not scores:
        default_key = _default_bucket_key(taxonomy)
        return {
            "primary": default_key,  # pack's first-declared (most-common) bucket
            "labels": [],
            "confidence": 0.2,
            "rationale": f"No keyword matches; defaulting to {default_key}.",
            "source": "keyword_fallback",
        }

    sorted_keys = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)
    primary = sorted_keys[0]
    labels = sorted_keys[1:3]  # up to 2 secondary labels
    total = sum(scores.values())
    confidence = round(min(0.7, scores[primary] / total), 2) if total else 0.2
    return {
        "primary": primary,
        "labels": labels,
        "confidence": confidence,
        "rationale": f"Keyword-overlap fallback: matched {scores[primary]} keyword(s).",
        "source": "keyword_fallback",
    }


async def classify(feature_description: str) -> dict:
    """Classify a feature description into the active domain's taxonomy.

    Returns a dict with keys:
      primary     — bucket_key (always present, validated against taxonomy)
      labels      — list[bucket_key]  (0-3 additional labels, all valid)
      confidence  — float 0..1
      rationale   — short explanation
      source      — "llm", "keyword_fallback" or "domain_pack"
      bucket      — full bucket config for convenience (required_fields, analogue_queries, ...)
    """
    taxonomy = get_taxonomy()

    if len(taxonomy) == 1:
        # A domain that declares no taxonomy (or a single bucket) has nothing
        # to classify into — skip the LLM call entirely.
        only_key = _default_bucket_key(taxonomy)
        return {
            "primary": only_key,
            "labels": [],
            "confidence": 1.0,
            "rationale": "The active domain pack declares a single bucket.",
            "source": "domain_pack",
            "bucket": taxonomy[only_key],
        }

    if not feature_description or not feature_description.strip():
        logger.warning("Empty feature description; returning default classification")
        fallback = _keyword_fallback("", taxonomy)
        return {
            **fallback,
            "bucket": taxonomy[fallback["primary"]],
        }

    # Stage 1: LLM classification
    try:
        # Slice 27 — taxonomy is a Purpose.ROUTING workload (fast bucket
        # classifier). Route to the lighter model when routing is enabled;
        # None falls back to the default frontier model.
        # Slice 28 — `agent_name` tags the observability trace with
        # purpose=ROUTING + model=routed, so cost dashboards can slice
        # taxonomy spend independently.
        raw = await call_llm(
            system=_CLASSIFY_SYSTEM.format(
                domain_name=prompt_block("domain_name", "this platform's"),
                bucket_list=_bucket_list_for_prompt(taxonomy),
            ),
            messages=[{"role": "user", "content": wrap_untrusted(feature_description[:4000], "FEATURE_DESCRIPTION")}],
            max_tokens=400,
            model=pick_model_for_agent("taxonomy"),
            agent_name="taxonomy",
        )
        parsed = await parse_llm_json(raw, fallback=None)

        if parsed and isinstance(parsed, dict) and parsed.get("primary") in taxonomy:
            # Validate secondary labels exist too
            valid_labels = [
                l for l in (parsed.get("labels") or [])
                if l in taxonomy and l != parsed["primary"]
            ][:3]
            result = {
                "primary":    parsed["primary"],
                "labels":     valid_labels,
                "confidence": float(parsed.get("confidence", 0.5)),
                "rationale":  parsed.get("rationale", ""),
                "source":     "llm",
                "bucket":     taxonomy[parsed["primary"]],
            }
            logger.info(
                "Taxonomy: primary=%s confidence=%.2f labels=%s",
                result["primary"], result["confidence"], result["labels"],
            )
            return result
    except Exception as e:
        logger.warning("Taxonomy LLM classification failed: %s", e)

    # Stage 2: keyword fallback
    result = _keyword_fallback(feature_description, taxonomy)
    result["bucket"] = taxonomy[result["primary"]]
    logger.info("Taxonomy (fallback): primary=%s confidence=%.2f",
                result["primary"], result["confidence"])
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Helpers callable from retrieval / validators
# ──────────────────────────────────────────────────────────────────────────────

def get_analogue_queries(classification: dict) -> list[str]:
    """Return combined analogue_queries for primary + secondary labels."""
    taxonomy = get_taxonomy()
    queries: list[str] = []
    primary = classification.get("primary")
    if primary and primary in taxonomy:
        queries.extend(taxonomy[primary]["analogue_queries"])
    for label in classification.get("labels", []):
        if label in taxonomy:
            queries.extend(taxonomy[label]["analogue_queries"])
    # Dedupe preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            ordered.append(q)
    return ordered


def get_required_fields(classification: dict) -> list[str]:
    """Return required_fields for the primary bucket."""
    taxonomy = get_taxonomy()
    primary = classification.get("primary")
    if primary and primary in taxonomy:
        return list(taxonomy[primary]["required_fields"])
    return []
