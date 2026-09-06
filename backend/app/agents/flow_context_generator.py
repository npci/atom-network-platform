# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Index-time API FLOW-MAP generator (THE BOOK v3.4, reuse-first §).

Builds a per-repo ``flow_context`` row: a map of how the system's APIs compose
into transaction flows — which existing API carries the ACTUAL financial leg (the
debit/credit / money movement) versus the metadata/initiation/status APIs, plus
multi-leg sequences. Runs at index time (parallel to ``module_context``) off the
already-indexed facts (module entry points + XSD message types), with ONE cheap
LLM labelling pass. The reuse-first approach gate pulls this so the agent reasons
over a ready flow map instead of rediscovering it each run.

Deliberately generic — it never names a specific API; the LLM discovers the
money-movement leg from the repo's own entry points. Fail-soft + gated: a flag-off,
no-facts, LLM hiccup, or parse error just writes nothing and never breaks indexing.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("app.agentic")

_MAX_ENTRY_POINTS = 80
_MAX_XSD = 60


# ── Fact gathering (from what indexing already produced) ────────────────────────

def _gather_facts(db, repo_id: str) -> dict:
    """Collect the orientation facts the flow map is built from: module entry
    points + summaries (module_context) and XSD message types (xsd graph)."""
    from app.models.module_context import ModuleContext
    from app.models.xsd_graph import XsdSchemaNode

    modules = (db.query(ModuleContext)
               .filter(ModuleContext.repo_id == repo_id)
               .order_by(ModuleContext.depth, ModuleContext.module_path).all())
    entry_points: list[dict] = []
    module_lines: list[str] = []
    for m in modules:
        for ep in (m.entry_points or []):
            if isinstance(ep, dict) and ep.get("name"):
                entry_points.append({"module": m.module_path, "kind": ep.get("kind"), "name": ep["name"]})
        if m.summary:
            module_lines.append(f"- {m.module_path or '.'}: {m.summary.strip()[:240]}")
    xsd_rows = (db.query(XsdSchemaNode).filter(XsdSchemaNode.repo_id == repo_id).limit(400).all())
    xsd_names = sorted({(r.path or "").rsplit("/", 1)[-1] for r in xsd_rows if r.path})[:_MAX_XSD]
    return {"entry_points": entry_points[:_MAX_ENTRY_POINTS],
            "module_lines": module_lines[:40], "xsd_names": xsd_names}


def _build_prompt(facts: dict) -> str:
    eps = "\n".join(f"  - {e.get('kind') or 'api'}: {e['name']} (module {e.get('module') or '?'})"
                    for e in facts["entry_points"]) or "  (none detected)"
    mods = "\n".join(facts["module_lines"]) or "  (no module summaries)"
    xsds = ", ".join(facts["xsd_names"]) or "(none indexed)"
    return (
        "Modules:\n" + mods + "\n\nAPI entry points (handlers/endpoints):\n" + eps
        + "\n\nXSD message types: " + xsds + "\n\n"
        "Map how these APIs compose into transaction flows. Identify which API performs the "
        "ACTUAL money movement (the debit/credit leg) versus the metadata/initiation/status APIs "
        "around it, and the multi-leg sequences. Use ONLY APIs/types present above; never invent names.\n"
        "Output STRICT JSON only (no prose, no code fence):\n"
        '{"summary": "2-4 sentences on how transactions flow through these APIs",'
        ' "transaction_apis": [{"api": "name", "why": "why this is the real debit/credit leg"}],'
        ' "meta_apis": [{"api": "name", "why": "initiation/status/metadata role"}],'
        ' "flows": [{"name": "flow name", "steps": ["api A", "api B"]}]}'
    )


def _build_core_prompt(facts: dict) -> str:
    """A CORE / framework / shared-library repo (e.g. network-core: XSD domain, shared
    utils, data-access) has no request→process→response 'transaction flows' to map.
    What an app repo needs to know is what this repo PROVIDES — the schemas, domain
    types, and shared libraries it exposes for reuse. Same JSON contract as the flow
    prompt (so storage/retrieval are uniform): transaction/meta APIs stay empty and
    `flows` carries the provided capability groups."""
    mods = "\n".join(facts["module_lines"]) or "  (no module summaries)"
    xsds = ", ".join(facts["xsd_names"]) or "(none indexed)"
    eps = "\n".join(f"  - {e.get('kind') or '?'}: {e['name']} (module {e.get('module') or '?'})"
                    for e in facts["entry_points"]) or "  (none)"
    return (
        "This is a FRAMEWORK / shared-library repo (no business transaction flows of its own). "
        "Modules:\n" + mods + "\n\nXSD message types: " + xsds + "\n\nEntry points:\n" + eps + "\n\n"
        "Describe what this repo PROVIDES for the app repos that depend on it — the shared "
        "libraries, domain/message types (XSD-generated), and utilities an app would import + reuse. "
        "Use ONLY modules/types present above; never invent names.\n"
        "Output STRICT JSON only (no prose, no code fence):\n"
        '{"summary": "2-4 sentences: this is a framework/shared repo providing X, Y, Z — what app repos reuse from it",'
        ' "transaction_apis": [], "meta_apis": [],'
        ' "flows": [{"name": "<provided module/capability, e.g. network-domain-xsd>",'
        ' "steps": ["<key type/class/artifact consumers import>"]}]}'
    )


def _parse_flow_json(text: str) -> dict:
    """Tolerant parse of the LLM's JSON: strips a ```json fence, and SALVAGES a
    truncated reply (cut at max_tokens) by closing open strings/brackets. Last
    resort keeps the de-fenced raw text as the summary so the row is still useful."""
    raw = (text or "").strip()
    body = raw
    if body.startswith("```"):
        body = body.split("```", 2)[1] if body.count("```") >= 2 else body.strip("`")
        if body.lstrip().lower().startswith("json"):
            body = body.lstrip()[4:]
    body = body.strip()
    start = body.find("{")
    candidates = []
    if start >= 0:
        end = body.rfind("}")
        if end > start:
            candidates.append(body[start:end + 1])
        # Truncated output: scan with a bracket STACK (string/escape-aware) and close
        # whatever is still open, in the right order, so json.loads can take it.
        frag = body[start:].rstrip().rstrip(",")
        stack, in_str, esc = [], False, False
        for ch in frag:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch in "{[":
                    stack.append(ch)
                elif ch == "}" and stack and stack[-1] == "{":
                    stack.pop()
                elif ch == "]" and stack and stack[-1] == "[":
                    stack.pop()
        if in_str:
            frag += '"'
        frag += "".join("}" if c == "{" else "]" for c in reversed(stack))
        candidates.append(frag)
    obj = None
    for cand in candidates:
        try:
            obj = json.loads(cand)
            break
        except (ValueError, json.JSONDecodeError):
            continue
    if not isinstance(obj, dict):
        return {"summary": body[:1500] if start < 0 else raw[:1500],
                "transaction_apis": [], "meta_apis": [], "flows": []}
    def _lst(k):
        v = obj.get(k)
        return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []
    return {"summary": (obj.get("summary") or "").strip(),
            "transaction_apis": _lst("transaction_apis"), "meta_apis": _lst("meta_apis"),
            "flows": _lst("flows")}


# ── Generate + persist ──────────────────────────────────────────────────────────

def generate_flow_context(db, repo_id: str, base_commit_sha: str | None = None) -> int:
    """Build + persist the per-repo flow map (idempotent rebuild). Returns 1 if a
    row was written, else 0. Requires module_context to have been generated first
    (it reads the entry points from those rows)."""
    from app.models.flow_context import FlowContext
    from app.models.code_repo import CodeRepo
    from app.core.llm import call_llm
    from app.agents.module_context_generator import _run_coro

    facts = _gather_facts(db, repo_id)
    if not facts["entry_points"] and not facts["module_lines"]:
        return 0                                    # nothing to map yet
    # Role-aware: a CORE/framework repo gets a "what it provides" map (schemas, shared
    # libs, domain types), not a transaction-flow map — flows don't exist there. Auto-
    # detect a framework repo too (no API entry points at all ⇒ library, not a service).
    repo = db.get(CodeRepo, repo_id)
    role = (getattr(repo, "role", None) or "").lower()
    is_core = role == "core" or (role != "app" and not facts["entry_points"])
    if is_core:
        system = ("You document what a network FRAMEWORK / shared-library repo provides for "
                  "reuse (schemas, domain types, shared utilities). Be factual to the facts "
                  "given; never invent names. Output strict JSON.")
        prompt = _build_core_prompt(facts)
    else:
        system = ("You map a network system's API transaction flows for code-reuse decisions. "
                  "Be factual to the facts given; never invent API/class names. Output strict JSON.")
        prompt = _build_prompt(facts)
    text = _run_coro(call_llm(system=system, messages=[{"role": "user", "content": prompt}],
                              max_tokens=4000, agent_name="flow_context"))
    parsed = _parse_flow_json(text)

    db.query(FlowContext).filter(FlowContext.repo_id == repo_id).delete(synchronize_session=False)
    db.add(FlowContext(
        repo_id=repo_id, summary=parsed["summary"] or None,
        transaction_apis=parsed["transaction_apis"], meta_apis=parsed["meta_apis"],
        flows=parsed["flows"], entry_points=facts["entry_points"], base_commit_sha=base_commit_sha))
    db.flush()
    return 1


def maybe_generate_flow_context(db, repo_id: str, base_commit_sha: str | None = None) -> int:
    """Gated, fail-soft entry the indexing pipeline calls AFTER module_context.
    No-op unless ``use_flow_context_generation`` is on; never breaks an ingest."""
    from app.core.config import settings
    if not settings.use_flow_context_generation:
        return 0
    try:
        return generate_flow_context(db, repo_id, base_commit_sha)
    except Exception as e:  # noqa: BLE001 — flow map is best-effort orientation
        logger.warning("flow_context generation skipped for repo=%s: %s", repo_id, e)
        return 0
