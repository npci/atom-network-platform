# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Critic LLM — semantic layer on top of deterministic checks.

The critic reads the source and target artifacts for a checkpoint, scores
the target against the contract's rubric dimensions, and returns finding
strings that the judge folds into its decision alongside deterministic
findings.

Provider abstraction is delegated to `app.core.llm.call_llm`, which already
supports Claude, OpenAI, AiNxt, Ollama, and Gemini via `settings.llm_provider`. This
module adds per-checkpoint overrides so an operator can flip a checkpoint's
critic provider/model without touching code:

  app_configs:
    eval_critic.<checkpoint_id>.enabled   = "false"   (default: true)
    eval_critic.<checkpoint_id>.provider  = "openai" | "anthropic"/"claude" | "ainxt" | "ollama" | "gemini"
    eval_critic.<checkpoint_id>.model     = e.g. "gpt-4o-mini", "claude-3-5-sonnet", "gemini-3.5-flash", "llama3.1:8b-instruct"

Generator-vs-critic split: when no critic provider is configured for a
checkpoint, the critic auto-picks a *different* provider from the
generator's current default so the same model is never grading itself.
Operators can disable this via settings.eval_critic_cross_provider=false.

Invariants:
- The critic NEVER blocks the workflow. Any failure (LLM error, bad JSON,
  empty response) is logged and produces an empty finding list, so the
  judge falls back to deterministic-only judgement.
- The critic NEVER edits artifacts. It only reads them and returns
  structured findings.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE
from app.core.config import settings
from app.services.evaluation.checkpoints import CheckpointId
from app.services.evaluation.contracts import get_contract
from app.services.evaluation.schemas import CheckpointContract

logger = logging.getLogger(__name__)


CONFIG_KEY_PREFIX = "eval_critic."
DEFAULT_MAX_TOKENS = 1200
ARTIFACT_TRUNCATION_CHARS = 6000  # keep the prompt small so latency stays manageable


# ── Public API ──────────────────────────────────────────────────────────────

@dataclass(slots=True)
class CriticResult:
    findings: list[str]
    judge_model: str | None
    provider: str | None
    enabled: bool
    latency_ms: int
    error: str | None = None
    # Provenance: which knowledge-base sources grounded this critique (empty
    # when grounding is off or nothing was indexed). Surfaced in Eval Logs.
    grounding_sources: list[dict] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.findings


async def critique(
    db: Session,
    checkpoint_id: CheckpointId | str,
    source_artifacts: dict[str, dict],
    target_artifacts: dict[str, dict],
) -> CriticResult:
    """Run the critic for one checkpoint. Never raises.

    Returns CriticResult with finding strings (same shape the judge consumes
    from `run_checks`). When the critic is disabled per-checkpoint or the
    call fails, `findings` is empty and `enabled` reflects the reason.
    """
    import time
    start_ms = int(time.time() * 1000)

    try:
        contract = get_contract(checkpoint_id)
    except KeyError:
        return CriticResult(
            findings=[], judge_model=None, provider=None,
            enabled=False, latency_ms=0,
            error=f"No contract for checkpoint '{checkpoint_id}'",
        )

    config = read_critic_config(db, contract.checkpoint_id)
    if not config["enabled"]:
        logger.debug("critic disabled for checkpoint=%s", contract.checkpoint_id.value)
        return CriticResult(
            findings=[], judge_model=None, provider=config["provider"],
            enabled=False, latency_ms=0,
        )

    # Ground the judge on the product / code knowledge base. Fail-open: an
    # empty result yields the same prompt as the ungrounded path.
    from app.services.evaluation.grounding import retrieve_grounding
    grounding = retrieve_grounding(db, contract, _join_text(target_artifacts))

    prompt = _build_prompt(contract, source_artifacts, target_artifacts, grounding)
    if prompt is None:
        return CriticResult(
            findings=[], judge_model=None, provider=config["provider"],
            enabled=True, latency_ms=0,
            error="No source or target artifact text available; critic skipped",
        )

    try:
        from app.core.llm import call_llm  # local import to keep this module test-friendly
        raw = await call_llm(
            system=prompt["system"],
            messages=[{"role": "user", "content": prompt["user"]}],
            max_tokens=DEFAULT_MAX_TOKENS,
            model=config["model"],
            provider=config["provider"],
            agent_name="eval_critic",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "eval critic LLM call failed checkpoint=%s provider=%s model=%s error=%s",
            contract.checkpoint_id.value, config["provider"], config["model"], exc,
        )
        return CriticResult(
            findings=[], judge_model=None, provider=config["provider"],
            enabled=True, latency_ms=int(time.time() * 1000) - start_ms,
            error=f"{type(exc).__name__}: {exc}",
        )

    findings = _parse_findings(raw)
    latency = int(time.time() * 1000) - start_ms
    judge_model_tag = _format_judge_model_tag(config)
    logger.info(
        "eval critic ran checkpoint=%s provider=%s model=%s findings=%d latency_ms=%d",
        contract.checkpoint_id.value, config["provider"], config["model"], len(findings), latency,
    )
    return CriticResult(
        findings=findings,
        judge_model=judge_model_tag,
        provider=config["provider"],
        enabled=True,
        latency_ms=latency,
        grounding_sources=grounding.provenance(),
    )


# ── Configuration ──────────────────────────────────────────────────────────

def read_critic_config(db: Session, checkpoint_id: CheckpointId) -> dict:
    """Resolve critic config for a checkpoint with sensible fallbacks.

    Order of resolution per field:
      1. app_configs row `eval_critic.<checkpoint_id>.<field>`
      2. settings.eval_critic_default_* (env-level default for this env)
      3. settings.llm_provider (cross-provider default for "provider")
      4. None  (model defaults to provider's default model)
    """
    cp = checkpoint_id.value if isinstance(checkpoint_id, CheckpointId) else str(checkpoint_id)
    prefix = f"{CONFIG_KEY_PREFIX}{cp}."

    rows: dict[str, str] = {}
    try:
        result = db.execute(
            text("SELECT key, value FROM app_configs WHERE key LIKE :prefix"),
            {"prefix": f"{prefix}%"},
        ).all()
        for key, value in result:
            if not key or value is None:
                continue
            rows[str(key).removeprefix(prefix)] = str(value).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not read critic config for checkpoint=%s: %s — using defaults",
            cp, exc,
        )

    # Resolution
    enabled_raw = rows.get("enabled")
    if enabled_raw is None:
        enabled = bool(getattr(settings, "eval_critic_enabled_by_default", True))
    else:
        enabled = enabled_raw.strip().lower() not in ("false", "0", "no", "off", "")

    provider = rows.get("provider") or getattr(settings, "eval_critic_default_provider", None)
    model = rows.get("model") or getattr(settings, "eval_critic_default_model", None)

    # Cross-provider default: if no per-checkpoint provider AND the operator
    # didn't set eval_critic_default_provider, pick a provider different
    # from the generator so the critic doesn't grade its own output.
    if not provider:
        cross = bool(getattr(settings, "eval_critic_cross_provider", True))
        generator_provider = (getattr(settings, "llm_provider", "claude") or "claude").lower().strip()
        if cross:
            provider = _pick_cross_provider(generator_provider)
        else:
            provider = generator_provider

    return {
        "enabled": enabled,
        "provider": provider or None,
        "model": model or None,
    }


def _pick_cross_provider(generator_provider: str) -> str:
    """Choose a critic provider that differs from the generator's.

    Falls back to the generator's own provider if no opposing provider is
    plausible in the current environment.
    """
    table = {
        "claude":    "openai",
        "anthropic": "openai",
        "openai":    "anthropic",
        "ainxt":     "anthropic",
        "ollama":    "ollama",  # local-only env: nothing else to fall back to
        "gemini":    "gemini",  # local Google-key env: keep critic working unless explicitly split
    }
    return table.get(generator_provider, generator_provider)


# ── Prompt construction ────────────────────────────────────────────────────

def _truncate(text_value: str, limit: int = ARTIFACT_TRUNCATION_CHARS) -> str:
    if not text_value:
        return ""
    if len(text_value) <= limit:
        return text_value
    return text_value[:limit] + f"\n\n…[truncated at {limit} chars]"


def _join_text(artifacts: dict[str, dict]) -> str:
    """Concatenate artifact texts under stable section labels for the prompt.

    Empty artifacts are dropped entirely so callers can distinguish "nothing
    to evaluate" from "we have something". This is important: the critic
    skips its LLM call when both sides are empty.
    """
    chunks: list[str] = []
    for name, art in (artifacts or {}).items():
        if not isinstance(art, dict):
            continue
        body = art.get("content") or art.get("text") or ""
        if not body and isinstance(art.get("questions"), list):
            # Compact separators, and NO hard slice here: the old indent=2 + [:limit] cut
            # the Q/A JSON mid-structure with no note, so the critic judged a malformed,
            # partial clarification record. _truncate below annotates any cut it makes.
            body = json.dumps({
                "questions": art["questions"],
                "answers": art.get("answers", {}),
            }, separators=(",", ": "))
        if not body or not str(body).strip():
            continue
        chunks.append(f"### {name}\n{_truncate(body)}".rstrip())
    return "\n\n".join(chunks).strip()


def _build_prompt(
    contract: CheckpointContract,
    source_artifacts: dict[str, dict],
    target_artifacts: dict[str, dict],
    grounding=None,
) -> dict | None:
    source_text = _join_text(source_artifacts)
    target_text = _join_text(target_artifacts)
    if not source_text and not target_text:
        return None

    rubric_lines = []
    for dim in contract.rubric_dimensions:
        rubric_lines.append(
            f"- id: {dim.id}\n"
            f"  name: {dim.name}\n"
            f"  description: {dim.description}\n"
            f"  weight: {dim.weight}\n"
            f"  minimum_score: {dim.minimum_score}"
        )
    rubric_block = "\n".join(rubric_lines)

    # Knowledge-base grounding block (empty string when nothing was retrieved —
    # local laptop with no index — so the prompt is identical to the old path).
    grounding_block = ""
    if grounding is not None:
        from app.services.evaluation.grounding import format_grounding_block
        grounding_block = format_grounding_block(grounding)

    grounding_rule = (
        "5. KNOWLEDGE BASE CONTEXT below is authoritative product/code "
        "knowledge retrieved for this artifact. Where the target contradicts "
        "it (wrong error code, wrong API contract, a claim the product docs "
        "refute), flag it and cite the source by its [n] tag. If the context "
        "is empty, evaluate on the rubric alone — do not invent facts.\n"
        if grounding_block else ""
    )

    system = (
        "You are an objective domain reviewer for the Authority / the network change "
        "management platform. You read a SOURCE artifact and the TARGET "
        "artifact produced from it, then evaluate the TARGET against the "
        "supplied rubric"
        + (" and the authoritative KNOWLEDGE BASE CONTEXT" if grounding_block else "")
        + ".\n\n"
        "Rules:\n"
        "1. Cite specific section names, line ranges, or quoted phrases. "
        "Never hallucinate problems.\n"
        "2. If the target satisfies the dimension, score it >= the minimum "
        "and leave the issue blank.\n"
        "3. network domain conventions: error codes use U##, Z#, RB, XT, XD, "
        "YB, YC, YD, or 00 — not HTTP codes. FRs follow FR-## numbering. "
        "Tech specs must include API contract, error codes, and a "
        "state model for any new flow.\n"
        "4. Return ONLY valid JSON in the exact schema below. No prose, "
        "no markdown fences.\n"
        + grounding_rule
        + f"6. {ANTI_INJECTION_CLAUSE}\n"
    )

    kb_section = (
        f"KNOWLEDGE BASE CONTEXT (authoritative):\n{grounding_block}\n\n"
        if grounding_block else ""
    )

    user = (
        f"CHECKPOINT: {contract.checkpoint_id.value}\n"
        f"TRANSITION: {contract.from_stage} -> {contract.to_stage}\n"
        f"DESCRIPTION: {contract.description}\n\n"
        f"RUBRIC DIMENSIONS:\n{rubric_block}\n\n"
        f"{kb_section}"
        f"SOURCE ARTIFACTS:\n"
        f"{wrap_untrusted(source_text, 'SOURCE_ARTIFACT') if source_text else '(none)'}\n\n"
        f"TARGET ARTIFACT (the one to evaluate):\n"
        f"{wrap_untrusted(target_text, 'TARGET_ARTIFACT') if target_text else '(none)'}\n\n"
        "Return JSON with this exact shape:\n"
        "{\n"
        "  \"findings\": [\n"
        "    {\"dimension\": \"<rubric.id>\", \"score\": <0.0-1.0>, "
        "\"issue\": \"<short issue or empty>\"}\n"
        "  ],\n"
        "  \"summary\": \"<single sentence overall assessment>\"\n"
        "}\n"
    )

    return {"system": system, "user": user}


# ── Response parsing ────────────────────────────────────────────────────────

_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


def _parse_findings(raw: str) -> list[str]:
    """Convert critic JSON response to a flat list of finding strings.

    Strategy:
      1. Try to parse `raw` as JSON directly.
      2. If that fails, find the first {...} block and try again.
      3. For each finding dict where score < minimum or issue is non-empty,
         emit a string like
         "[critic:requirement_completeness score=0.55] FR-04 has no API design".
      4. Append the summary as extra context only when issue findings exist.
         A clean summary must not downgrade a PASS to WARN, especially under
         hard gates where non-PASS verdicts block transitions.
      5. On any parse error, return [] (advisory: never block on critic noise).
    """
    if not raw or not isinstance(raw, str):
        return []

    parsed = _try_json(raw)
    if parsed is None:
        return []

    out: list[str] = []
    findings = parsed.get("findings") if isinstance(parsed, dict) else None
    if isinstance(findings, list):
        for f in findings:
            if not isinstance(f, dict):
                continue
            dim = str(f.get("dimension") or "").strip()
            score = f.get("score")
            issue = str(f.get("issue") or "").strip()
            if not dim and not issue:
                continue
            score_str = ""
            try:
                if isinstance(score, (int, float)):
                    score_str = f" score={float(score):.2f}"
            except Exception:  # noqa: BLE001
                score_str = ""
            if issue:
                tag = f"[critic:{dim}{score_str}]" if dim else "[critic]"
                out.append(f"{tag} {issue}")

    summary = parsed.get("summary") if isinstance(parsed, dict) else None
    if out and isinstance(summary, str) and summary.strip():
        out.append(f"[critic:summary] {summary.strip()}")

    return out


def _try_json(raw: str) -> dict | None:
    text_value = raw.strip()
    # Strip common code-fence wrappers
    for fence in ("```json", "```"):
        if text_value.startswith(fence):
            text_value = text_value[len(fence):].lstrip()
            if text_value.endswith("```"):
                text_value = text_value[:-3].rstrip()
    try:
        parsed = json.loads(text_value)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    match = _JSON_BLOCK.search(text_value)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _format_judge_model_tag(config: dict) -> str:
    provider = config.get("provider") or "default"
    model = config.get("model") or "default"
    return f"critic:{provider}:{model}"
