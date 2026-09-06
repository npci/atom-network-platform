# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Context-Assembler — builds the scoped, stale-aware ContextPack (THE BOOK §4/§8).

Deterministic glue, not a new engine: it reuses the existing RAG/impact/blueprint
machinery, **scoped to the selected repos** (§5) and **stale-aware** (§5 — index
results are advisory when the clone has drifted). The ContextPack it returns
feeds the XSD-Discovery and Code-Change subagents (S9).

Fixes the known regulatory-drop bug: the legacy ``_compress_doc_for_code_change``
SKIP-keywords dropped the BRD "## 10. Regulatory & Compliance" section wholesale
(losing the error-code tables). Here we parse the doc into a full
``{heading: body}`` map and prune NOTHING — the agent pulls what it needs.
"""
from __future__ import annotations
from app.core.prompts import load_prompt

import logging
import os
from dataclasses import dataclass, field

from app.agents import repo_scope, workspace_local
from app.core.config import settings
# Reuses the hierarchical chunker's section splitter (private, but the canonical
# section parser in this codebase — keep in step if it moves).
from app.rag.doc_chunker_hierarchical import _extract_sections

logger = logging.getLogger("app.agentic")
_NOTES_CAP = 8 * 1024


@dataclass
class ContextPack:
    selected_repo_ids: list[str]
    # Roster of the selected repos with their human label + build role, so the agent
    # understands the multi-repo topology (core = framework/XSDs, app = business
    # logic) instead of seeing opaque ids. [{id, label, role}]
    repos: list[dict] = field(default_factory=list)
    repo_base_sha: dict[str, str] = field(default_factory=dict)
    stale_index: dict[str, bool] = field(default_factory=dict)
    module_notes: dict[str, str] = field(default_factory=dict)
    impact_files: list[str] = field(default_factory=list)
    brd_sections: dict[str, str] = field(default_factory=dict)
    tsd_sections: dict[str, str] = field(default_factory=dict)
    # Prior DOCUMENT-ONLY XSD assessment (legacy /xsd/assess verdict), when one exists.
    # Pull-based + advisory: the agent may consult it via read_doc(doc='assessment')
    # but must verify against code — it never inherits the conclusion.
    assessment_sections: dict[str, str] = field(default_factory=dict)
    # The RATIFIED Change-Analysis plan, FULL FIDELITY, served via read_doc(doc='plan').
    # Every truncation marker in the rendered plan digests points here — the digest may
    # clip, this must not. Empty when no ratified analysis exists.
    plan_sections: dict[str, str] = field(default_factory=dict)
    # {repo_id: {extension: count}} — every file extension actually present in the
    # clone. Purely DERIVED by walking the workspace: no extension list, no language
    # or schema-format knowledge, so a Python/Protobuf repo censuses as such. See
    # `_file_census` for why this exists.
    file_census: dict[str, dict[str, int]] = field(default_factory=dict)
    intent: str = ""


def doc_sections(content: str | None) -> dict[str, str]:
    """Parse a doc into ``{heading: body}``, keeping EVERY section.

    No relevance pruning and no keyword SKIP list — notably the
    Regulatory/Compliance section (with its error-code tables) is preserved,
    which the legacy compressor dropped. The code-change agent selects what it
    needs from the full map.
    """
    if not content or not content.strip():
        return {}
    out: dict[str, str] = {}
    for s in _extract_sections(content):
        body = content[s.body_start:s.body_end].strip()
        if not body:
            continue
        title = s.title if s.level > 0 else "Overview"
        if title in out:                       # repeated heading → suffix, never overwrite
            n = 2
            while f"{title} ({n})" in out:
                n += 1
            title = f"{title} ({n})"
        out[title] = body
    return out



# A16 (architecture review Medium #9, "No Query-Level Caching for Repeated
# Reads") — `assemble_context_pack()` re-runs `_latest_content()` for BRD/TSD/
# assessment on EVERY invocation, and it is invoked once per phase of an
# agentic run (analysis, each code_change continuation round, review, ...) —
# not just once per run. A run with several fix-loop rounds therefore
# re-queries the same, rarely-changing document content from Postgres
# repeatedly. This is a per-process, size-bounded cache keyed by
# `(doc_id, version)` — the version is the authoritative "latest" identity
# per the query's own ORDER BY, so a cache hit is provably the same content
# without re-touching the DB, and a NEW version (the doc was regenerated)
# is a different key, never serving stale content. `agentic_tools.py` already
# has an analogous process-local `_READ_CACHE` for file reads (keyed
# path+mtime) — this mirrors that established pattern for document reads.
_DOC_CONTENT_CACHE: dict[tuple[str, str, int], str] = {}
_DOC_CONTENT_CACHE_MAX = 256


def _content_at_version(db, model, change_request_id: str, version: int) -> str | None:
    """Fetch a doc's content at a SPECIFIC version — the read side of ADR-0005's
    TSD version lock. Used only when a run has a ``tsd_version_locked`` value, so
    a TSD regenerated mid-run (which bumps ``version`` and would otherwise change
    what ``_latest_content`` returns) cannot alter the contract under a running
    code agent. Cache-compatible: uses the SAME ``(table, id, version)`` cache key
    as ``_latest_content``, so a version that was also ever "latest" is a cache
    hit either way. Falls back to None (never raises) if that exact version was
    since deleted — callers already handle a None doc gracefully."""
    row_id = (
        db.query(model.id)
        .filter(model.change_request_id == change_request_id, model.version == version)
        .order_by(model.created_at.desc())
        .first()
    )
    if row_id is None:
        return None
    doc_id = row_id[0]
    cache_key = (model.__tablename__, str(doc_id), int(version or 0))
    cached = _DOC_CONTENT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    row = db.query(model).filter(model.id == doc_id).first()
    content = row.content if row else None
    if content is not None:
        if len(_DOC_CONTENT_CACHE) >= _DOC_CONTENT_CACHE_MAX:
            _DOC_CONTENT_CACHE.pop(next(iter(_DOC_CONTENT_CACHE)))
        _DOC_CONTENT_CACHE[cache_key] = content
    return content


def _latest_content(db, model, change_request_id: str) -> str | None:
    # version is the authoritative "latest" key (created_at can collide when rows
    # are written in one transaction); created_at breaks ties.
    #
    # Two-step query so a cache hit skips loading the (potentially 50KB+)
    # `content` column entirely: first resolve just the (id, version) the DB
    # currently considers "latest" (cheap — no large TEXT column), then only
    # fetch `content` on a cache miss.
    row_id_version = (
        db.query(model.id, model.version)
        .filter(model.change_request_id == change_request_id)
        .order_by(model.version.desc(), model.created_at.desc())
        .first()
    )
    if row_id_version is None:
        return None
    doc_id, version = row_id_version
    cache_key = (model.__tablename__, str(doc_id), int(version or 0))
    cached = _DOC_CONTENT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    row = db.query(model).filter(model.id == doc_id).first()
    content = row.content if row else None
    if content is not None:
        if len(_DOC_CONTENT_CACHE) >= _DOC_CONTENT_CACHE_MAX:
            # Simple eviction: drop an arbitrary (insertion-order-oldest in
            # CPython 3.7+) entry rather than pulling in a full LRU
            # dependency for a 256-entry bound — good enough for a
            # process-local, best-effort cache.
            _DOC_CONTENT_CACHE.pop(next(iter(_DOC_CONTENT_CACHE)))
        _DOC_CONTENT_CACHE[cache_key] = content
    return content


def _format_module(r) -> str:
    """Render one module_context row. The deterministic facts (entry points + the
    symbol→file map) are JUMP TARGETS — file paths so the agent reads the right file
    instead of greping for the name; the LLM functional_flow is explicitly marked
    LOW-AUTHORITY so it orients but is never treated as ground truth (§19)."""
    parts = [f"## {r.module_path}", (r.summary or "").strip()]
    if r.entry_points:
        eps = [f"{e.get('kind')}:{e.get('name')} → {e.get('file')}" for e in r.entry_points[:8]
               if isinstance(e, dict)]
        if eps:
            parts.append("entry points: " + ", ".join(eps))
    if r.key_types:
        # Older rows stored bare name strings; only the {name, file} shape is a jump
        # target, so render those and let pre-reindex rows fall through to the summary.
        kt = [f"{t.get('name')} → {t.get('file')}" for t in r.key_types[:15]
              if isinstance(t, dict) and t.get("file")]
        if kt:
            parts.append("key types (symbol → file — read these directly): " + ", ".join(kt))
    if r.functional_flow:
        parts.append(f"flow (LOW-AUTHORITY — verify with read_file): {r.functional_flow.strip()}")
    return "\n".join(p for p in parts if p)


_FLOW_ADVISORY = load_prompt("agents/context_assembler/flow_advisory.md")


def _format_flow(r, flow: str | None = None) -> str:
    """Render the per-repo flow map (reuse-first §) with KEY-BASED retrieval, mirroring
    module_context: no ``flow`` → a thin index (summary + API roles + flow NAMES); pass a
    flow name → that flow's step sequence. Always framed as orientation, never truth."""
    flows = [f for f in (r.flows or []) if isinstance(f, dict)]
    names = [f.get("name") or "?" for f in flows]
    if flow:
        q = flow.strip().lower()
        hits = ([f for f in flows if (f.get("name") or "").lower() == q]
                or [f for f in flows if q in (f.get("name") or "").lower()])
        if not hits:
            return (f"(no indexed flow matches {flow!r}; indexed flows: "
                    f"{', '.join(names) or 'none'}. The flow may still EXIST in code — "
                    "this map is incomplete; trace it from the entry points with grep/read_file)")
        parts = [_FLOW_ADVISORY]
        for f in hits[:3]:
            parts.append(f"## {f.get('name')}\nsteps: "
                         + " → ".join(str(s) for s in (f.get("steps") or [])))
        if r.transaction_apis:
            parts.append("transaction (debit/credit) APIs:\n" + "\n".join(
                f"  - {a.get('api')}: {a.get('why', '')}" for a in r.transaction_apis if isinstance(a, dict)))
        return "\n".join(p for p in parts if p)
    parts = [_FLOW_ADVISORY]
    if r.summary:
        parts.append(r.summary.strip())
    if r.transaction_apis:
        parts.append("transaction (debit/credit) APIs:\n" + "\n".join(
            f"  - {a.get('api')}: {a.get('why', '')}" for a in r.transaction_apis if isinstance(a, dict)))
    if r.meta_apis:
        parts.append("meta / initiation / status APIs:\n" + "\n".join(
            f"  - {a.get('api')}: {a.get('why', '')}" for a in r.meta_apis if isinstance(a, dict)))
    if names:
        # A CORE/framework repo has no transaction/meta APIs — its `flows` entries are
        # PROVIDED modules/capabilities, so label them as such instead of "flows".
        is_provides = not r.transaction_apis and not r.meta_apis
        if is_provides:
            parts.append("provided modules / capabilities (call again with `flow=<name>` for the "
                         "key types it exposes; may be INCOMPLETE — verify in code): " + ", ".join(names))
        else:
            parts.append("indexed flows (call again with `flow=<name>` for the step sequence; "
                         "this list may be INCOMPLETE — verify in code): " + ", ".join(names))
    return "\n".join(p for p in parts if p)


def _module_index_line(r) -> str:
    """One compact index line per module — name + a short summary clip, indented by
    depth. The FULL record (entry points, flow, key types, deps) is pulled on demand
    via the ``module_context`` tool, so this push stays a thin discovery index (§19)."""
    summary = (r.summary or "").strip().replace("\n", " ")
    if len(summary) > 140:
        clipped = summary[:140].rsplit(" ", 1)[0]        # back off to a word boundary
        summary = (clipped or summary[:140]).rstrip() + "…"
    indent = "  " * max(0, r.depth or 0)
    return f"{indent}- {r.module_path or '.'}" + (f" — {summary}" if summary else "")


def module_notes(db, selected_repo_ids: list[str], run_id: str | None = None) -> dict[str, str]:
    """Per-repo MODULE INDEX: a thin list of index-time ``module_context`` modules
    (§19) — names + a one-line summary — so the agent knows what exists and pulls a
    module's full context via the ``module_context`` tool. Falls back to the in-repo
    ``MODULE_NOTES.md`` from the clone (§14 hybrid) when no index rows exist."""
    from app.models.module_context import ModuleContext

    out: dict[str, str] = {}
    for rid in selected_repo_ids:
        rows = (
            db.query(ModuleContext)
            .filter(ModuleContext.repo_id == rid)
            .order_by(ModuleContext.depth, ModuleContext.module_path)   # deterministic order
            .limit(200)
            .all()
        )
        if rows:
            # Cap on WHOLE-LINE boundaries — a bare [:_NOTES_CAP] slice cut a module line
            # mid-word ("DbSyncProperti…"). Drop trailing modules with a pointer instead.
            lines: list[str] = []
            used = 0
            for r in rows:
                ln = _module_index_line(r)
                if used + len(ln) + 1 > _NOTES_CAP:
                    lines.append(f"… (+{len(rows) - len(lines)} more modules — "
                                 "call module_context to list all)")
                    break
                lines.append(ln)
                used += len(ln) + 1
            out[rid] = "\n".join(lines)
            continue
        if run_id:
            try:
                p = workspace_local.repo_dir(run_id, rid) / "MODULE_NOTES.md"
                if p.is_file():
                    out[rid] = p.read_text(encoding="utf-8", errors="replace")[:_NOTES_CAP]
            except Exception:  # noqa: BLE001 — notes are best-effort orientation
                pass
    return out


def _impact_files(db, descriptions: list[str | None]) -> list[str]:
    """Blast-radius files for the context pack. Advisory, never a gate.

    Backend is chosen by ``settings.impact_backend`` ("sql" default | "age").
    Both fail-open to [] — the graph may be unpopulated or unavailable."""
    # COMBINE every usable description (intent first, then TSD/BRD) instead of taking only
    # the first hit: the intent is usually a terse one-liner, and the richer doc text carries
    # the symbol names the seed extractor actually feeds on. Backends slice at 5000 chars, so
    # intent-first keeps its seeds dominant.
    desc = "\n".join(d.strip() for d in descriptions if d and len(d.strip()) >= 10)
    if not desc:
        return []
    if getattr(settings, "impact_backend", "sql") == "age":
        return _impact_files_age(db, desc)
    return _impact_files_sql(db, desc)


def _impact_files_sql(db, desc: str) -> list[str]:
    """Blast radius via the pure-SQL graph (kg/sql_graph) — no AGE. Extracts
    symbol seeds from the description (same extractor the AGE path uses), then
    unions the seed symbols' own files with their inbound callers. Each query is
    savepoint-scoped inside sql_graph, so a failure can't poison the caller's
    transaction (the tx-poison class of bug)."""
    try:
        from app.rag.graph_retriever import _extract_query_seeds
        from app.kg import sql_graph
        seeds = _extract_query_seeds(desc[:5000])
        if not seeds:
            return []
        chunks = sql_graph.find_chunks_by_symbol_name(db, seeds) + sql_graph.inbound_callers(db, seeds)
        files = list(dict.fromkeys(c["source_file"] for c in chunks if c.get("source_file")))
        return files[:25]
    except Exception as e:  # noqa: BLE001
        logger.debug("impact analysis (sql) failed (fail-open): %s", e)
        return []


def _impact_files_age(db, desc: str) -> list[str]:
    """Legacy Apache AGE / Cypher blast-radius path. Fail-open ([])."""
    try:
        from app.kg.impact_analyzer import analyze_impact
        report = analyze_impact(db=db, change_description=desc[:5000])
        return list(report.files_affected[:25])
    except Exception as e:  # noqa: BLE001
        logger.debug("impact analysis (age) failed (fail-open): %s", e)
        return []


def _plan_sections(db, change_request_id: str) -> dict[str, str]:
    """The RATIFIED Change-Analysis plan as ``{heading: body}`` for read_doc(doc='plan').
    Full fidelity (pretty-printed JSON per section) — this is the retrieval path behind
    every truncation marker in the rendered plan digests. Fail-open to {}."""
    import json
    try:
        from app.models.change_analysis import ChangeAnalysis
        ca = (db.query(ChangeAnalysis)
              .filter(ChangeAnalysis.change_request_id == change_request_id,
                      ChangeAnalysis.status == "ratified")
              .order_by(ChangeAnalysis.version.desc()).first())
        if not ca:
            return {}
        out: dict[str, str] = {}
        for name, blob in (("Functional plan", ca.functional_plan),
                           ("Technical analysis", ca.technical_analysis),
                           ("Flow spec", ca.flow_spec)):
            if blob:
                out[name] = json.dumps(blob, indent=1, ensure_ascii=False, default=str)
        return out
    except Exception as e:  # noqa: BLE001 — optional context, never blocks assembly
        logger.debug("plan sections load failed (fail-open): %s", e)
        return {}


def _redact_doc_sections_for_llm(
    sections: dict[str, str], doc_label: str, run_id: str | None,
) -> dict[str, str]:
    """T6 (THREAT_MODEL.md) — filter PII out of document content before it can
    reach an external LLM provider.

    The ContextPack exists solely to be rendered into agent prompts
    (`agentic_subagents.py` renders `brd_sections`/`tsd_sections`/
    `assessment_sections`/`plan_sections` directly into the prompt body), so
    this function sits at the single chokepoint where every document-sourced
    prompt input is built — rather than at each of the ~8 render sites.

    Uses `PROFILE_DOC`, NOT the aggressive default profile: BRD/TSD content is
    a machine-readable contract the code agent must implement against, and the
    default profile's bare 9–18-digit pattern matches timeouts, epoch
    timestamps, response codes and byte budgets. Redacting those would corrupt
    the contract — a privacy control causing a correctness bug. `PROFILE_DOC`
    only redacts digit runs adjacent to a PII-indicating label, plus the
    always-high-signal mobile/MPIN patterns.

    Off by default (`pii_redaction_docs_enabled=False`) so the codegen hot path
    is opt-in and can be validated against the golden/eval suite first. When
    off, this returns the input unchanged — byte-for-byte today's behaviour.
    Fails OPEN: a redaction error must never break run assembly, since that
    would convert a best-effort privacy filter into an availability incident.
    """
    if not sections or not getattr(settings, "pii_redaction_docs_enabled", False):
        return sections
    try:
        from app.core.pii_redaction import redact_doc_sections
        redacted, count = redact_doc_sections(
            sections, doc_label=doc_label, correlation_id=run_id)
        if count:
            # security_architecture_skills.md §13.2 — structured security
            # telemetry. Never logs the redacted VALUES, only the count.
            logger.info(
                "SECURITY_EVENT event=pii_redacted_before_llm_call severity=low "
                "doc=%s run_id=%s redaction_count=%d decision=redacted",
                doc_label, run_id or "-", count,
            )
        return redacted
    except Exception:  # noqa: BLE001 — never break assembly over a best-effort filter
        logger.exception(
            "pii redaction failed for doc=%s run_id=%s — passing content through unredacted",
            doc_label, run_id or "-",
        )
        return sections


def _file_census(repo_ids: list[str], run_id: str | None) -> dict[str, dict[str, int]]:
    """Count files by extension in each repo's clone: {repo_id: {ext: count}}.

    WHY. An analysis agent picks its own search patterns, and a plausible-looking
    sweep can silently omit a whole file class. A real run globbed `**/*.java`,
    `**/*.xml`, `**/*.properties`, `**/*.yml` and `**/*.yaml` — thorough enough to
    look complete, yet it never enumerated the repo's one `.xsd` (the published
    contract it was changing) or its one `.md`. Both were readable the whole time;
    the agent simply never asked. The resulting plan changed the generated Java and
    not the schema, and invented a participant topology the README contradicts.

    A census cannot be reasoned around: the prompt states what IS there, so an
    unexamined class is visibly unexamined rather than invisible.

    Derived only — walks the clone and reads suffixes off whatever it finds. It
    encodes no extension list and no notion of which extensions matter, so it
    carries no domain or language assumptions. Mirrors `agentic_tools.glob`'s walk
    (prune `.git`, nothing else) so the census describes exactly the file set glob
    would search. Best-effort: an un-cloned or unreadable repo yields no entry
    rather than failing assembly.
    """
    out: dict[str, dict[str, int]] = {}
    if not run_id:
        return out
    for rid in repo_ids:
        try:
            root = workspace_local.repo_dir(run_id, rid)
            if not root.is_dir():
                continue
            counts: dict[str, int] = {}
            for dirpath, dirnames, filenames in os.walk(root):
                if ".git" in dirnames:
                    dirnames.remove(".git")
                for fn in filenames:
                    ext = os.path.splitext(fn)[1].lower() or "(no extension)"
                    counts[ext] = counts.get(ext, 0) + 1
            if counts:
                out[rid] = counts
        except Exception:  # noqa: BLE001 — orientation aid, never blocks assembly
            logger.exception("file census failed repo=%s run=%s", rid, run_id)
    return out


def assemble_context_pack(
    db,
    *,
    change_request_id: str,
    selected_repo_ids: list[str],
    repo_base_sha: dict[str, str] | None = None,
    run_id: str | None = None,
    intent: str = "",
    tsd_version_locked: int | None = None,
) -> ContextPack:
    """Assemble the ContextPack for a run. ``repo_base_sha`` (from the S4 clones)
    drives stale detection; absent, repos are treated as fresh. ``intent`` is the
    free-text change description — used to drive impact analysis in the
    quick-start flow where there is no BRD/TSD. ``tsd_version_locked`` (ADR-0005)
    pins the TSD content to a SPECIFIC version instead of "whatever is latest
    right now" — set once a run's TSD approval gate passes, so a TSD regenerated
    mid-run does not change the contract the code agent is working against.
    None (the default, and every run created before this existed) preserves the
    prior "always latest" behaviour exactly."""
    repos = repo_scope.validate_selection(db, selected_repo_ids)   # §5 hard gate
    repo_ids = [r.id for r in repos]
    base_sha = dict(repo_base_sha or {})
    # Always one entry PER selected repo (a partial repo_base_sha must not leave a
    # repo missing from stale_index → KeyError downstream). A repo with no base SHA
    # can't be checked for drift, so is_stale reports it fresh.
    stale = {rid: repo_scope.is_stale(db, rid, base_sha.get(rid)) for rid in repo_ids}

    from app.models.brd import BRD
    from app.models.tech_spec import TechSpec
    from app.models.xsd import XSD
    brd = _latest_content(db, BRD, change_request_id)
    if tsd_version_locked is not None:
        tsd = _content_at_version(db, TechSpec, change_request_id, tsd_version_locked)
        if tsd is None:   # locked version was deleted/unreadable — fail open to latest
            tsd = _latest_content(db, TechSpec, change_request_id)
    else:
        tsd = _latest_content(db, TechSpec, change_request_id)
    # Legacy document-only XSD assessment, if the change ran it (advisory; see ContextPack).
    try:
        assessment = _latest_content(db, XSD, change_request_id)
    except Exception:  # noqa: BLE001 — optional context, never blocks assembly
        assessment = None

    return ContextPack(
        selected_repo_ids=repo_ids,
        repos=[{"id": r.id, "label": r.label, "role": (getattr(r, "role", None) or "app")}
               for r in repos],
        repo_base_sha=base_sha,
        stale_index=stale,
        module_notes=module_notes(db, repo_ids, run_id),
        # intent first so impact analysis still runs in the quick-start flow (no BRD/TSD).
        # NOTE: impact analysis runs on the UNREDACTED text on purpose — it is a
        # local SQL/AGE lookup against this platform's own database, not an
        # external LLM call, so T6 (which is specifically about what crosses the
        # boundary to an external provider) does not apply to it. Redacting here
        # would only degrade impact-file matching for no privacy gain.
        impact_files=_impact_files(db, [intent, tsd, brd]),
        file_census=_file_census(repo_ids, run_id),
        brd_sections=_redact_doc_sections_for_llm(doc_sections(brd), "brd", run_id),
        tsd_sections=_redact_doc_sections_for_llm(doc_sections(tsd), "tsd", run_id),
        assessment_sections=_redact_doc_sections_for_llm(
            doc_sections(assessment), "assessment", run_id),
        plan_sections=_redact_doc_sections_for_llm(
            _plan_sections(db, change_request_id), "plan", run_id),
        intent=intent or "",
    )
