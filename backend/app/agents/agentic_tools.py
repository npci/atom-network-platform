# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Agentic tool set + registry (THE BOOK §8).

The tools the coding subagent drives through the bounded loop. Every tool is:

* **repo-scoped** — a path resolves to a *selected* repo's clone or it is refused
  (§5 rules 2/4, via :mod:`repo_scope`);
* **contained** — paths can't escape the clone dir (no ``../`` traversal);
* **ground-truth** — reads/edits hit the clone, never the index.

Guards enforced here: **read-before-edit** (you can't ``edit_file`` a file you
haven't ``read_file``'d this run) and the **4-level edit ladder** with its
single-match guard (:mod:`agentic_edit`). ``MODULE_NOTES.md`` is auto-injected
(once per directory, labeled orientation-only) on the first read under it.

This slice ships the lexical + file tools (read/grep/glob/edit/create/delete/
submit_plan). The structural tier (``symbol_graph``/``ast_query``/``lsp_*``) and
``code_search_semantic`` plug into the same registry in a later slice.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.agents import workspace_local
from app.agents.agentic_edit import apply_edit, EditError
from app.agents.platform_adapter import adapter

logger = logging.getLogger("app.agentic")
from app.agents.repo_scope import assert_repo_selected, RepoSelectionError
from app.core.config import settings

_MODULE_NOTES = "MODULE_NOTES.md"
_NOTES_CAP = 8 * 1024            # 8 KB (§8)
_READ_MAX = 200 * 1024          # cap a single read so a huge file can't blow the context
_GREP_MAX_LINES = 200
_SKELETON_MAX_SYMBOLS = 400     # cap the outline a huge file returns (a 25k-line file fits easily)
_BIG_FILE_HEAD_LINES = 150      # non-code big file: show this many head lines + a line-count note
_LANG_BY_EXT = {".java": "java", ".py": "python", ".ts": "typescript", ".tsx": "typescript",
                ".js": "javascript", ".jsx": "javascript"}

# Content cache for file reads, keyed by (absolute path, mtime). A re-read of an UNCHANGED file
# is served from memory (still stat()s for freshness — the win is skipping the full read_text on
# unchanged files); an edit bumps the mtime → automatic invalidation, never a stale hit. Also the
# backstop that makes history compaction lossless: an evicted read is reconstructed from here.
_READ_CACHE: dict[tuple[str, float], str] = {}
_READ_CACHE_MAX = 256


def _cached_read_text(target) -> str:
    key = (str(target), target.stat().st_mtime)
    hit = _READ_CACHE.get(key)
    if hit is not None:
        return hit
    text = target.read_text(encoding="utf-8", errors="replace")
    if len(_READ_CACHE) >= _READ_CACHE_MAX:
        _READ_CACHE.pop(next(iter(_READ_CACHE)))      # bounded; FIFO-evict the oldest entry
    _READ_CACHE[key] = text
    return text


class ToolError(Exception):
    """A tool failed in a way the model can recover from → is_error tool_result."""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── Artifacts ─────────────────────────────────────────────────────────────────

@dataclass
class FileOp:
    op: str                    # "add" | "modify" | "delete"
    repo_id: str
    path: str                  # repo-relative
    content: str | None        # None for delete
    content_hash: str | None


@dataclass
class RunContext:
    """Mutable state for one subagent run. The runtime owns one of these."""
    run_id: str
    selected_repo_ids: list[str]
    read_files: set[tuple[str, str]] = field(default_factory=set)        # (repo_id, path) read this run
    # READ GRANULARITY (evidence integrity): which files were read IN FULL vs only in line
    # ranges. A ranged read of 5 lines must not substantiate a claim about an unrelated
    # part of a 25k-line file — _verify_evidence consults these.
    full_reads: set[tuple[str, str]] = field(default_factory=set)        # whole-content reads
    read_ranges: dict[tuple[str, str], list[tuple[int, int]]] = field(default_factory=dict)
    # Stale-edit guard (hashline-lite): sha256 of the file content the model LAST SAW —
    # recorded on read/write, checked by edit_file. A mismatch means the file changed on
    # disk since (e.g. a build step or a sibling process rewrote it) and the model's
    # old_string anchor targets content it never read — refuse and force a re-read
    # instead of landing an edit against a stale mental model.
    read_hashes: dict[tuple[str, str], str] = field(default_factory=dict)
    # Full outputs of truncated run_command calls, keyed "out<N>" — retrievable (paged)
    # via the read_output tool instead of forcing an expensive/non-reproducible re-run.
    command_outputs: dict[str, str] = field(default_factory=dict)
    intel_queried: set[str] = field(default_factory=set)                 # structural-intel tokens (§8 gate)
    file_ops: dict[tuple[str, str], FileOp] = field(default_factory=dict)  # keyed (repo_id, path)
    notes_injected: set[str] = field(default_factory=set)               # dirs whose MODULE_NOTES were shown
    plan: dict | None = None
    db: object | None = None        # SQLAlchemy session — only the index-backed tools (find_existing_xsd) use it
    index_calls_ok: bool | None = None  # cached call-graph coverage probe (None = unprobed); see _calls_index_ok
    doc_sections: dict = field(default_factory=dict)   # {"brd": {heading: body}, "tsd": {...}} — pulled via read_doc
    # The run whose clone holds the working tree. None ⇒ self. A Phase-B (code) run sets
    # this to its Phase-A parent so the file tools read/edit the SHARED tree (combined MR).
    workspace_run_id: str | None = None
    proposal: dict | None = None    # reuse-vs-new options surfaced via propose_approach (gate)
    awaiting_decision: bool = False  # set by propose_approach → ends the loop for the human gate
    concerns: list = field(default_factory=list)  # disruptive-change declines via flag_concern
    # XSD phase (Phase A): restrict file writes to schema files (.xsd/.xjb). Java and
    # other consumer edits belong to the code phase, so the XSD step stays schema-only.
    schema_only: bool = False
    # Code phase (Phase B): the INVERSE — schema (.xsd/.xjb) was finalized + human-approved in
    # Phase A and is the fixed baseline here, so a code-change write to schema does not land
    # directly. It is STAGED as an amendment proposal for a human to approve (see
    # `schema_amendments`); the agent implements Java/consumer changes in the meantime.
    code_phase: bool = False
    # 1c fail-back: basenames of .xsd/.xjb the code phase TRIED to write but the schema write-lock
    # did not let through — i.e. schema Phase A did not freeze. Deduped so the
    # needs_contract_amendment signal fires once per file, not once per retry.
    schema_writes_refused: set[str] = field(default_factory=set)
    # Staged schema amendments (fix 2). A code-phase schema write is captured here instead of
    # being flatly refused: the run finishes its Java work, then parks at
    # awaiting_schema_amendment so a human sees the exact before/after hunk and decides.
    # Keyed (repo_id, path, old_string) so a retried identical edit stages once.
    schema_amendments: dict[tuple[str, str, str], dict] = field(default_factory=dict)
    # Working-memory FACT SHEET: load-bearing facts recorded at the moment of discovery
    # (record_fact + server auto-facts), pinned into the brief by the runtime and carried
    # across phase handoffs. facts_rev lets the runtime re-pin only on change (each re-pin
    # costs a prompt-cache re-read).
    facts: list[dict] = field(default_factory=list)
    facts_rev: int = 0

    def changeset(self) -> list[FileOp]:
        return list(self.file_ops.values())


# ── Path resolution (scoping + containment) ──────────────────────────────────────

def _repo_root(ctx: RunContext, repo_id: str) -> Path:
    assert_repo_selected(repo_id, ctx.selected_repo_ids)   # raises RepoSelectionError
    # Phase B operates on its Phase-A parent's clone (workspace_run_id); default = self.
    return workspace_local.repo_dir(ctx.workspace_run_id or ctx.run_id, repo_id)


def _resolve_repo_id(ctx: RunContext, repo_id: str | None = None, path: str | None = None) -> str:
    """Best-effort recovery of a missing/blank ``repo_id`` for the read-only
    orientation tools. The AiNxt OpenAI→Anthropic tool-call shim sometimes DROPS
    arguments, so a perfectly-formed model call arrives with ``repo_id`` missing —
    which used to fail with a raw TypeError and waste a whole LLM turn. Recover when
    it's unambiguous (single selected repo, or a path that exists in exactly one
    repo); otherwise raise a CLEAR, actionable error the model can retry against.
    Never used by the mutating tools — those keep repo_id strictly required."""
    sel = ctx.selected_repo_ids or []
    if repo_id and repo_id != "all":
        return repo_id                       # validated downstream (_repo_root)
    if len(sel) == 1:
        return sel[0]
    if path:
        # Contain the path inside each repo BEFORE probing existence — _resolve refuses
        # `../` escapes, so a traversal path can't be used as a host-filesystem oracle.
        hits = []
        for rid in sel:
            try:
                if _resolve(ctx, rid, path).is_file():
                    hits.append(rid)
            except ToolError:
                continue
        if len(hits) == 1:
            return hits[0]
    raise ToolError(
        "repo_id is required and could not be inferred. Selected repos: "
        + (", ".join(sel) or "(none)")
        + (f"; '{path}' was not found in exactly one of them." if path else "")
        + " — call again with an explicit repo_id (or, for grep/glob, omit repo_id to "
          "search ALL selected repos at once).")


def _resolve(ctx: RunContext, repo_id: str, rel_path: str) -> Path:
    """Resolve a repo-relative path to an absolute clone path, refusing escapes."""
    root = _repo_root(ctx, repo_id).resolve()
    target = (root / rel_path).resolve()
    if root != target and root not in target.parents:
        raise ToolError(f"path {rel_path!r} escapes repo {repo_id}")
    return target


# ── MODULE_NOTES injection (§8/§19) ──────────────────────────────────────────────

def _module_notes(ctx: RunContext, repo_id: str, rel_path: str) -> str | None:
    """Nearest MODULE_NOTES.md walking up from the file's dir to the repo root,
    shown once per directory, capped, and labeled orientation-only."""
    root = _repo_root(ctx, repo_id).resolve()
    start = (root / rel_path).resolve().parent
    d = start
    while True:
        notes = d / _MODULE_NOTES
        if notes.is_file():
            key = f"{repo_id}:{notes.parent.relative_to(root)}"
            if key in ctx.notes_injected:
                return None
            ctx.notes_injected.add(key)
            body = notes.read_text(encoding="utf-8", errors="replace")[:_NOTES_CAP]
            return (
                "[MODULE_NOTES — orientation only; verify specifics with read_file "
                "before relying on them]\n" + body
            )
        if d == root or d == d.parent:   # stop at repo root OR filesystem root (never loop)
            return None
        d = d.parent


# ── Tools ─────────────────────────────────────────────────────────────────────

def _file_skeleton(path: str, content: str) -> str | None:
    """A navigable OUTLINE of a large file — each top-level symbol's kind, name and LINE
    RANGE — so the agent reads only the slices it needs (read_file start_line/end_line)
    instead of pulling a 25k-line file into context. Crucial for mixed-product files where
    only one product's methods are relevant. Returns None for non-code / unparseable files
    (caller falls back to a head + line-count note)."""
    import os
    lang = _LANG_BY_EXT.get(os.path.splitext(path)[1].lower())
    if not lang:
        return None
    try:
        from app.rag import code_chunker_langs as C
        if lang not in C.supported_languages():
            return None
        chunks = C.extract_chunks(path, content, lang)
    except Exception:                                   # noqa: BLE001 — degrade to head fallback
        return None
    rows = sorted((c for c in chunks if c.get("symbol_kind") != "file" and c.get("line_start")),
                  key=lambda c: c["line_start"])
    if not rows:
        return None
    out = []
    for c in rows[:_SKELETON_MAX_SYMBOLS]:
        label = (c.get("signature") or f"{c.get('symbol_kind', 'symbol')} {c.get('symbol_name', '')}").strip()
        out.append(f"  L{c['line_start']}-{c.get('line_end', c['line_start'])}: {label[:160]}")
    if len(rows) > _SKELETON_MAX_SYMBOLS:
        out.append(f"  … +{len(rows) - _SKELETON_MAX_SYMBOLS} more symbols (refine with grep)")
    return "\n".join(out)


def read_file(ctx: RunContext, repo_id: str | None = None, path: str | None = None,
              start_line: int | None = None, end_line: int | None = None) -> str:
    if not path:
        raise ToolError("read_file needs a path")
    repo_id = _resolve_repo_id(ctx, repo_id, path)
    target = _resolve(ctx, repo_id, path)
    if not target.is_file():
        raise ToolError(f"file not found: {path}")
    text = _cached_read_text(target)
    if len(text) > _READ_MAX and not (start_line or end_line):
        # Don't dump (or hard-error on) a huge file — that kills the context window. Return its
        # STRUCTURE so the agent reads only the ranges it needs. NOT counted as a full read:
        # the agent must read an actual range before it can edit (read-before-edit still holds).
        # Stale-edit self-heal: for a file the agent HAS already read, this skeleton view is
        # the re-read the guard's error demands — refresh the known hash so the guard
        # unwedges (a mis-anchored edit still fails apply_edit's exact-match, safely).
        if (repo_id, path) in ctx.read_hashes:
            ctx.read_hashes[(repo_id, path)] = _sha256(text)
        nlines = text.count("\n") + 1
        skel = _file_skeleton(path, text)
        if skel:
            return (f"[{path} is large ({nlines} lines / {len(text)} bytes) — showing its STRUCTURE, not "
                    f"the body. Call read_file again with start_line/end_line for the part you need; the "
                    f"whole file is rarely required — in a mixed-product file, read only YOUR product's "
                    f"methods.]\n{skel}")
        head = "\n".join(text.splitlines()[:_BIG_FILE_HEAD_LINES])
        return (f"[{path} is large ({nlines} lines) — first {_BIG_FILE_HEAD_LINES} lines shown; call "
                f"read_file with start_line/end_line for any other range.]\n{head}")
    ctx.read_files.add((repo_id, path))
    ctx.read_hashes[(repo_id, path)] = _sha256(text)    # full-file hash even for a ranged read

    body = text
    if start_line or end_line:
        lines = text.splitlines()
        lo = max((start_line or 1) - 1, 0)
        hi = end_line or len(lines)
        body = "\n".join(lines[lo:hi])
        # Granularity record: this read covers ONLY [lo+1, hi] — evidence citing lines
        # outside every read range is not verified by it (_verify_evidence).
        ctx.read_ranges.setdefault((repo_id, path), []).append((lo + 1, min(hi, len(lines))))
    else:
        ctx.full_reads.add((repo_id, path))

    notes = _module_notes(ctx, repo_id, path)
    return f"{notes}\n\n{body}" if notes else body


def _all_repos(ctx: RunContext, fn, label: str) -> str:
    """Run a per-repo search over EVERY selected repo, grouped by repo header. The
    multi-repo production topology (a core/framework repo holding the schemas ·
    an app repo holding the business logic) means consumers often live in a
    DIFFERENT repo than the schema — a
    single-repo search silently misses them, so all-repo is the discovery default."""
    parts = []
    for rid in ctx.selected_repo_ids or []:
        try:
            body = fn(rid)
        except Exception as e:  # noqa: BLE001 — one repo's failure must not kill the fan-out
            # Unmissable + repo-scoped: buried between other repos' real matches, a quiet
            # "(error)" skims as "searched, no hits" — and if THIS repo holds the symbol
            # the agent concludes it doesn't exist. Catch beyond ToolError too (e.g. a
            # missing clone raising OSError): the other repos' results still matter.
            body = f"(⚠ {label} FAILED in this repo — treat as UNSEARCHED, not 'no matches': {e})"
        parts.append(f"## repo {rid}\n{body}")
    return "\n\n".join(parts) or "(no repos selected)"


def grep(ctx: RunContext, repo_id: str | None = None, pattern: str = "", path: str | None = None) -> str:
    if not pattern:
        raise ToolError("grep needs a pattern")
    if not repo_id or repo_id == "all":
        return _all_repos(ctx, lambda rid: grep(ctx, rid, pattern, path=None), "grep")

    root = _repo_root(ctx, repo_id)
    # --untracked: also search NEW files the agent just created (git grep skips
    # untracked by default → it would be blind to the agent's own new files).
    # Still respects .gitignore, so build output (target/) is excluded.
    # -E (EXTENDED regex): the model writes ERE-style patterns — `A|B|C` alternation,
    # `x+`, `(grp)`. git grep's DEFAULT is BASIC regex, where `|`/`+`/`(` are LITERAL,
    # so `class Foo|interface Foo` searched for the literal string with a pipe and
    # returned "(no matches)" even when Foo exists — a silent false negative the model
    # then read as "code absent". Verified on run 353259ba: every `|` grep returned
    # nothing, including `class ConfigParamService|interface ConfigParamService` on a
    # repo that plainly has `public class ConfigParamService`.
    argv = ["git", "grep", "-n", "-E", "--no-color", "--untracked", "-e", pattern]
    if path:
        _resolve(ctx, repo_id, path)         # validate containment
        argv += ["--", path]
    res = adapter.run_command(root, argv)
    if res.timed_out:
        raise ToolError(f"grep TIMED OUT in {repo_id} for pattern {pattern!r} — narrow it "
                        "(add path=, or a more specific pattern) and retry")
    if res.exit_code not in (0, 1):           # 1 == no matches (not an error)
        raise ToolError(f"grep FAILED in {repo_id} for pattern {pattern!r}: "
                        f"{res.stderr.strip()[:200]}")
    lines = res.stdout.splitlines()
    out = "\n".join(lines[:_GREP_MAX_LINES])
    if len(lines) > _GREP_MAX_LINES:
        out += f"\n… ({len(lines) - _GREP_MAX_LINES} more matches — narrow the pattern)"
    return out or "(no matches)"


def glob(ctx: RunContext, repo_id: str | None = None, pattern: str = "") -> str:
    if not pattern:
        raise ToolError("glob needs a pattern")
    if not repo_id or repo_id == "all":
        return _all_repos(ctx, lambda rid: glob(ctx, rid, pattern), "glob")

    root = _repo_root(ctx, repo_id).resolve()
    hits: list[str] = []
    # os.walk so we can PRUNE .git (don't descend a 100k-file object store), and
    # match on path COMPONENTS (a dir named "foo.git" is not the git dir).
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for fn in filenames:
            rel = os.path.relpath(os.path.join(dirpath, fn), root)
            if fnmatch.fnmatch(rel, pattern):
                hits.append(rel)
    hits.sort()
    out = "\n".join(hits[:500])
    if len(hits) > 500:                       # mirror grep — a silent cap reads as complete
        out += f"\n… ({len(hits) - 500} more files matched — narrow the pattern)"
    return out or "(no matches)"


def _skip_non_schema_write(ctx: RunContext, path: str) -> bool:
    """Phase A (XSD) should write schema only (.xsd/.xjb). For now a non-schema write
    (e.g. a .java consumer) is SILENTLY skipped and just logged — it must NOT error or
    disrupt the XSD flow. An XSD phase that ends up with no schema edit is fine; the
    stage simply shows "no XSD change needed", and the Java lands later in the code
    phase. Returns True when the write should be skipped.

    TODO(revisit): this is a stop-gap. Decide the real policy — either route the Java
    edit into the code phase properly, or let the XSD phase make it — instead of
    silently dropping it here.
    """
    if ctx.schema_only and not path.lower().endswith((".xsd", ".xjb")):
        logger.warning("xsd-phase: silently skipping non-schema write to %s (run=%s) — "
                       "deferred to the code phase", path, ctx.run_id)
        return True
    return False


_MAX_STAGED_AMENDMENTS = 12


def _is_schema(path: str) -> bool:
    return path.lower().endswith((".xsd", ".xjb"))


def _stage_schema_write(ctx: RunContext, repo_id: str, path: str, *,
                        old_string: str = "", new_string: str = "",
                        create_content: str | None = None) -> str | None:
    """Capture a code-phase schema write as an AMENDMENT PROPOSAL instead of applying it.

    Inverse of `_skip_non_schema_write`: the code phase (Phase B) must not silently rewrite
    the .xsd/.xjb a human approved in Phase A — a code run once quietly rewrote a schema it
    was only meant to consume, and re-opening an approved artifact mid-implementation drifts
    scope past what anyone signed off on. So the write still does not LAND here.

    But a flat refusal was its own trap. When the schema is genuinely wrong on the wire — a
    purpose code that collides with a live value, say — there is no Java-side fix, and the
    old behaviour returned "[REFUSED] … LOCKED" with no alternative. The reviewer kept
    demanding the edit, the agent kept being unable to make it, and the run escalated to a
    human seven times over seventeen hours without ever being able to act on the answer.
    Neither "let the agent edit approved schema" nor "refuse forever" is right; the missing
    third option is STAGE IT AND ASK.

    So the edit is recorded verbatim (exact old/new text, so the human sees a real hunk and
    approval can replay it byte-for-byte) and the run parks at ``awaiting_schema_amendment``
    when the code work is done. Returns the tool message to hand back to the agent, or None
    when this write is not a code-phase schema write and should proceed normally.
    """
    if not (ctx.code_phase and _is_schema(path)):
        return None

    base = path.rsplit("/", 1)[-1]
    creating = create_content is not None
    key = (repo_id, path, ("<create>" if creating else old_string))

    if key in ctx.schema_amendments:
        # Same edit, second attempt. Say so plainly: a retry loop here is the exact failure
        # this fix exists to end, and the agent needs to hear "already handled, move on".
        return (f"[ALREADY STAGED] {path} — this schema amendment is already staged for human "
                "approval (you proposed the identical change earlier this run). Do NOT retry it. "
                "Continue with the Java/consumer work you CAN complete; if the remaining work "
                "genuinely depends on this amendment landing, say so in your summary and finish "
                "everything else.")

    if len(ctx.schema_amendments) >= _MAX_STAGED_AMENDMENTS:
        return (f"[NOT STAGED] {path} — {_MAX_STAGED_AMENDMENTS} schema amendments are already "
                "staged for this run, which is past the point where a human can usefully review "
                "them one by one. The schema baseline is likely wrong for this change: stop "
                "editing schema, finish what you can in Java, and explain the mismatch in your "
                "summary so Phase A can be re-run.")

    ctx.schema_amendments[key] = {
        "repo_id": repo_id, "path": path, "file": base,
        "kind": "create" if creating else "edit",
        "old_string": (old_string or "")[:4000],
        "new_string": (new_string or "")[:4000],
        "content": (create_content or "")[:20000] if creating else None,
        "staged_at_iteration": getattr(ctx, "iteration", None),
    }
    logger.info("code-phase: staged schema amendment for %s (run=%s, %d staged)",
                path, ctx.run_id, len(ctx.schema_amendments))

    # 1c fail-back signal, kept: the code phase needs a schema change Phase A did not make.
    # Now it has a real consumer (the amendment gate) instead of being emitted into the void.
    if base not in ctx.schema_writes_refused:
        ctx.schema_writes_refused.add(base)
        try:
            if ctx.db is not None:
                from app.agents.agentic_events import emit_event
                emit_event(ctx.db, ctx.run_id, "needs_contract_amendment",
                           {"schema": base, "path": path, "staged": True,
                            "action": (f"⚠ the code phase needs a change to schema '{base}' that "
                                       "Phase A did not make — it is staged for your approval and "
                                       "will be shown before the run continues")})
        except Exception:  # noqa: BLE001 — surfacing must never break the write guard
            pass

    what = "creating a new schema file" if creating else "editing the approved schema"
    return (f"[STAGED — NOT YET APPLIED] {path} was NOT modified on disk. Schema is the "
            f"human-approved Phase-A baseline, so {what} needs a human's sign-off. Your exact "
            "change has been recorded and will be shown to a human for approval when this round "
            "ends; if they approve it, it is applied verbatim and you resume with the amended "
            "schema.\n"
            "DO NOT retry this write and do NOT treat the change as done. Right now:\n"
            "1. Finish every part of the task that does NOT depend on this amendment — Java, "
            "mappers, validators, tests. That work is real and is kept either way.\n"
            "2. If some remaining work depends on the amendment, leave it and name it explicitly "
            "in your summary rather than faking it or working around it with a value you know "
            "is wrong.\n"
            "3. State in your summary WHY the schema must change (what breaks otherwise, with "
            "file:line evidence), so the human is deciding on evidence rather than on your "
            "assertion.")


def _skip_schema_write(ctx: RunContext, path: str) -> bool:
    """True when `path` is a schema file a code-phase run may not write directly.

    Retained as the predicate for callers that only need the yes/no (the staging payload
    comes from :func:`_stage_schema_write`)."""
    return bool(ctx.code_phase and _is_schema(path))


def intel_gate_reason(ctx: RunContext, tool_name: str, tool_input: dict | None) -> str | None:
    """Policy gate (enforced by the agent LOOP, not the edit mechanic): block a .java
    MODIFY until structural blast-radius intel was gathered this run (impact_analysis /
    callers / symbol_graph / ast_query / lsp_diagnostics) — so a shared-symbol change is
    made WITH its consumers known, not blind. Returns a blocking reason, or None to allow.

    ON by default (``agentic_require_intel_before_java_edit``); set False for the legacy
    blind-edit flow. Exemptions: gate off; non-edit tool; non-.java path; a file CREATED
    this run (op=add — your own new file has no existing consumers); intel already
    gathered. grep does NOT satisfy it; an xsd: token (schema_guardian) does not either —
    only call-graph/structural tokens (symbol:/path:) count."""
    from app.core.config import settings
    if not getattr(settings, "agentic_require_intel_before_java_edit", True):
        return None
    if tool_name != "edit_file":
        return None
    inp = tool_input or {}
    path = str(inp.get("path") or "")
    if not path.lower().endswith(".java"):
        return None
    op = ctx.file_ops.get((inp.get("repo_id"), path))
    if op is not None and op.op == "add":
        return None                       # editing a file you created this run — no blast radius
    if any(t.startswith(("symbol:", "path:")) for t in ctx.intel_queried):
        return None
    return ("before editing Java, gather blast-radius intel first: call impact_analysis "
            "(or callers / symbol_graph) for the symbol you're changing, or ast_query on "
            "this file — this catches consumers you'd otherwise miss. (grep doesn't count.)")


def _java_parses_ok(content: str) -> bool:
    """True if ``content`` parses as Java with no tree-sitter ERROR nodes. Fail-open: returns
    True when the grammar is unavailable or anything goes wrong, so a missing optional dependency
    (or an attribute drift in the bindings) can never block a legitimate edit."""
    try:
        from app.rag.symbol_graph_extractor_java import _get_parser
        parser = _get_parser()
        if parser is None:
            return True
        tree = parser.parse(content.encode("utf-8", errors="replace"))
        return not tree.root_node.has_error
    except Exception as e:                       # noqa: BLE001 — fail-open
        logger.debug("java parse-check unavailable (fail-open): %s", e)
        return True


def _edit_echo(new_content: str, new_string: str) -> str:
    """±3 lines of the file around the applied replacement — evidence of what actually landed.
    A level-3/4 fuzzy match writes ``new_string`` verbatim even when the anchor's indentation
    differed; without an echo that mistake is invisible until the next build burns a round."""
    pos = new_content.find(new_string) if new_string else -1
    if pos < 0:
        return "(replacement applied — re-read the file to see the result)"
    first_line = new_content.count("\n", 0, pos)
    lines = new_content.splitlines()
    lo = max(0, first_line - 3)
    hi = min(len(lines), first_line + new_string.count("\n") + 4)
    snippet = "\n".join(lines[lo:hi])
    if len(snippet) > 800:
        snippet = snippet[:800] + "…"
    return f"file now reads (lines {lo + 1}-{hi}):\n{snippet}"


_XSD_PATTERN_RE = re.compile(r"<(?:\w+:)?pattern\b[^>]*\bvalue=\"([^\"]*)\"", re.I)


def _xsd_facet_warnings(path: str, text: str) -> str:
    """Sanity-check xs:pattern facets on a schema write. A pattern reaches the file through a
    JSON tool argument, so it crosses two escaping layers; doubling a backslash once too often
    turns `\\.` (escaped dot) into `\\\\` — a LITERAL BACKSLASH — which silently WIDENS the
    allowed character set. The result is valid XSD, valid XML and builds clean, so nothing else
    in this phase notices: schema_guardian only answers reuse-vs-create and there is no compile
    of the facet itself. Advisory only — returns '' when the facets look sane."""
    if not path.lower().endswith(".xsd"):
        return ""
    from xml.sax.saxutils import unescape
    out: list[str] = []
    for raw in _XSD_PATTERN_RE.findall(text):
        pat = unescape(raw, {"&quot;": '"', "&apos;": "'"})
        if "\\\\" in pat:
            out.append(f'pattern "{pat}" allows a LITERAL BACKSLASH. If you meant an escaped '
                       r'dot, the XSD needs \. — which in a JSON tool argument is written '
                       r'"\\." and NOT "\\\\.". Re-check the character set you were given.')
            continue
        if "\\p{" in pat or "\\P{" in pat:     # Unicode category escapes: valid XSD, no Python equivalent
            continue
        try:
            re.compile(pat)
        except re.error as e:
            out.append(f'pattern "{pat}" does not compile as a regex ({e}) — check it for a '
                       "stray bracket, quantifier or escape.")
    if not out:
        return ""
    return "\n⚠ XSD facet check — the schema was written, fix these now:\n" + "\n".join(
        f"  - {w}" for w in out)


def edit_file(ctx: RunContext, repo_id: str, path: str,
              old_string: str, new_string: str) -> str:
    if _skip_non_schema_write(ctx, path):
        return (f"[SKIPPED] {path} was NOT modified. The XSD phase writes schema (.xsd/.xjb) only; "
                "make this edit in the code phase instead. Do not count it as done.")
    # _resolve FIRST — it is what validates repo selection and path containment,
    # and staging used to happen before it and return, so a staged schema write
    # bypassed containment entirely. `schema_amendment.apply()` later joins the
    # stored path onto the repo dir with no check of its own, so the escape
    # became an arbitrary file write on operator approval.
    target = _resolve(ctx, repo_id, path)
    staged = _stage_schema_write(ctx, repo_id, path,
                                 old_string=old_string, new_string=new_string)
    if staged is not None:
        return staged
    if (repo_id, path) not in ctx.read_files:
        raise ToolError(f"read_file {path} before editing it (read-before-edit)")
    if not target.is_file():
        raise ToolError(f"file not found: {path}")
    content = target.read_text(encoding="utf-8", errors="replace")
    # Stale-edit guard: the on-disk content must match what the model last read/wrote.
    # A mismatch means something else rewrote the file — an edit placed by exact-string
    # (or worse, fuzzy) match against remembered content could land in the wrong place
    # or clobber the external change. Refuse; a re-read refreshes the hash and unblocks.
    known = ctx.read_hashes.get((repo_id, path))
    if known is not None and _sha256(content) != known:
        raise ToolError(f"{path} has CHANGED on disk since you last read it — your edit is "
                        "anchored to stale content. read_file it again (the current version), "
                        "then re-issue the edit against what is actually there.")
    try:
        new_content, level = apply_edit(content, old_string, new_string)
    except EditError as e:
        raise ToolError(str(e))
    # LF-normalize the in-memory result (a model-supplied new_string can carry \r\n):
    # every reader normalizes to LF, so hashes/FileOps must too — and the write below
    # re-expands to the file's own EOL, which would double a stray \r otherwise.
    new_content = new_content.replace("\r\n", "\n")
    # Guarded post-edit syntax check (Slice 16): if the edit turns a previously CLEAN Java parse
    # into a broken one it introduced a syntax error — reject BEFORE writing (disk untouched) so
    # the agent fixes it now instead of burning a build. Fires only on a clean→broken REGRESSION
    # (never penalizes a file the grammar already couldn't parse) and only when use_ast_editor is on.
    from app.core.config import settings as _s
    if (getattr(_s, "use_ast_editor", False) and path.lower().endswith(".java")
            and _java_parses_ok(content) and not _java_parses_ok(new_content)):
        raise ToolError(f"edit rejected — it introduces a Java syntax error in {path} (unbalanced "
                        "braces or an incomplete statement). Re-read the surrounding lines and emit "
                        "a corrected, syntactically-complete edit.")
    # Match the file's existing dominant EOL: writing LF onto a CRLF source rewrites
    # every line of the diff and drowns the actual edit (−24,789/+24,890 on a 25K-line
    # file for a small change). Detection reads the CURRENT (pre-write) bytes.
    workspace_local.write_preserving_eol(target, new_content)
    ctx.read_hashes[(repo_id, path)] = _sha256(new_content)   # your own edit is "seen" content
    op = "add" if (repo_id, path) in ctx.file_ops and ctx.file_ops[(repo_id, path)].op == "add" else "modify"
    ctx.file_ops[(repo_id, path)] = FileOp(op, repo_id, path, new_content, _sha256(new_content))
    return (f"edited {path} (match level {level})\n{_edit_echo(new_content, new_string)}"
            + _xsd_facet_warnings(path, new_content))


def create_file(ctx: RunContext, repo_id: str, path: str, content: str) -> str:
    if _skip_non_schema_write(ctx, path):
        return (f"[SKIPPED] {path} was NOT created. The XSD phase writes schema (.xsd/.xjb) only; "
                "create this file in the code phase instead. Do not count it as done.")
    # _resolve FIRST — see the matching comment in edit_file(). Staging before
    # containment let an LLM-supplied `../` path reach schema_amendment.apply().
    target = _resolve(ctx, repo_id, path)
    staged = _stage_schema_write(ctx, repo_id, path, create_content=content)
    if staged is not None:
        return staged
    if target.exists():
        raise ToolError(f"{path} already exists — use edit_file")
    target.parent.mkdir(parents=True, exist_ok=True)
    content = content.replace("\r\n", "\n")   # keep disk + hashes LF-consistent with readers
    workspace_local.write_preserving_eol(target, content)
    ctx.read_files.add((repo_id, path))      # a file you just wrote is "known"
    ctx.full_reads.add((repo_id, path))      # …in FULL: you authored every line of it
    ctx.read_hashes[(repo_id, path)] = _sha256(content)
    ctx.file_ops[(repo_id, path)] = FileOp("add", repo_id, path, content, _sha256(content))
    return f"created {path}" + _xsd_facet_warnings(path, content)


def delete_file(ctx: RunContext, repo_id: str, path: str) -> str:
    target = _resolve(ctx, repo_id, path)
    if not target.is_file():
        raise ToolError(f"file not found: {path}")
    target.unlink()
    ctx.file_ops[(repo_id, path)] = FileOp("delete", repo_id, path, None, None)
    ctx.read_files.discard((repo_id, path))
    ctx.read_hashes.pop((repo_id, path), None)
    return f"deleted {path}"


def ask_decision(ctx: RunContext, question: str, blocked_item: str,
                 options: list | None = None) -> str:
    """A3 — the code agent surfaces a decision it must NOT make itself: a binding directive
    conflicts with observed code reality, or a required decision is missing and underivable.
    Ends the loop; the run parks at awaiting_code_decision until a human answers (the answer
    becomes a ledger directive). May resolve gaps only — never reopens a ratified directive."""
    q = (question or "").strip()
    blocked = (blocked_item or "").strip()
    if not q or not blocked:
        return ("ask_decision NOT recorded — `question` and `blocked_item` (the directive / plan "
                "item this blocks, quoted) are both required. If you can safely proceed within "
                "the ratified directives, proceed instead of asking.")
    ctx.proposal = {"kind": "code_decision", "question": q[:800], "blocked_item": blocked[:400],
                    "options": [o for o in (options or []) if isinstance(o, dict)][:4]}
    ctx.awaiting_decision = True
    return "decision question recorded — stopping for the human's answer"


def submit_plan(ctx: RunContext, summary: str, files: list | None = None,
                reuse_decisions: list | None = None,
                reconciliation: list | None = None) -> str:
    ctx.plan = {"summary": summary, "files": files or [], "reuse_decisions": reuse_decisions or [],
                "reconciliation": reconciliation or []}
    return "plan recorded"


def _verify_evidence(ctx: RunContext, evidence: list) -> tuple[list, list]:
    """Split evidence into VERIFIED (a file the agent actually read this run, across any
    selected repo) vs UNVERIFIED. The UPI codebase is large and confusing, so a reuse-vs-
    new decision must be grounded in code the agent really opened — not asserted from
    memory. Read-tracking (ctx.read_files) is the ground truth the model can't fake.

    GRANULARITY: a file read ONLY in line ranges verifies a claim only when the claim
    cites a ``line`` inside (±30 of) a range actually read — five viewed lines must not
    substantiate a claim about an unrelated part of a 25k-line file. Full reads (and
    files whose read granularity predates tracking, e.g. resume-seeded) keep path-level
    credit. Unverified items carry a ``why_unverified`` reason for the bounce message."""
    read_paths = {p for (_rid, p) in ctx.read_files}
    full_paths = {p for (_rid, p) in ctx.full_reads}
    ranges_by_path: dict[str, list[tuple[int, int]]] = {}
    for (_rid, p), rs in ctx.read_ranges.items():
        ranges_by_path.setdefault(p, []).extend(rs)

    def _match(path: str) -> str | None:
        """The read path this citation refers to. DETERMINISTIC: `read_paths` is a set, so
        picking the first suffix match made the choice depend on set iteration order — with
        the same basename in two repos, `m` (and therefore which file's read RANGES the
        granularity check consults) varied run to run. Sorted + longest-first: the most
        specific match wins, and the same input always resolves the same way."""
        if path in read_paths:
            return path
        cands = [rp for rp in read_paths
                 if rp.endswith("/" + path) or path.endswith("/" + rp)]
        return max(sorted(cands), key=len) if cands else None

    verified, unverified = [], []
    for e in (evidence or []):
        if not isinstance(e, dict):
            continue
        path = (e.get("file") or e.get("path") or "").strip()
        m = _match(path) if path else None
        if not m:
            unverified.append({**e, "file": path, "why_unverified": "not read this run"})
            continue
        if m in full_paths or path in full_paths:
            verified.append({**e, "file": path})
            continue
        rs = ranges_by_path.get(m) or ranges_by_path.get(path) or []
        if not rs:                      # known but granularity untracked (resume-seeded) — path-level credit
            verified.append({**e, "file": path})
            continue
        try:
            line = int(e.get("line")) if e.get("line") is not None else None
        except (TypeError, ValueError):
            line = None
        if line is not None and any(lo - 30 <= line <= hi + 30 for lo, hi in rs):
            verified.append({**e, "file": path})
        else:
            spans = ", ".join(f"{lo}-{hi}" for lo, hi in rs[:6])
            unverified.append({**e, "file": path, "why_unverified":
                               (f"you read only lines {spans} of this file — cite a `line` "
                                "inside a range you read, or read the relevant range first")})
    return verified, unverified


def propose_approach(ctx: RunContext, summary: str, options: list,
                     recommended: str | None = None, evidence: list | None = None) -> str:
    """Reuse-first decision gate: after mapping the existing flows, surface 2-3 ways to
    accommodate the requirement (reuse/extend an existing API vs a new one) for a human
    to choose. Records the proposal and ENDS this pass — no edits happen until the human
    decides (the orchestrator stops the run at the approach-decision gate).

    EVIDENCE is required and validated: each item {claim, file} must cite a file you
    actually READ this run. Citing files you didn't open is rejected — the decision must
    be grounded in the real code (UPI's flows are not guessable). If too few claims are
    verified, this returns WITHOUT recording the proposal so you go read the code first."""
    opts = options if isinstance(options, list) else []
    # Schema drift: options sometimes arrive as plain STRINGS (seen on off-framework asks,
    # e.g. a whole-platform migration). Coerce to minimal option dicts — the decision gate
    # needs {id, title} to render and to record the human's choice. Drop blanks BEFORE
    # numbering so synthesized ids stay contiguous (option-1, option-2, …) and a caller's
    # `recommended="option-N"` still lines up with a real option.
    opts = [o for o in opts if isinstance(o, dict) or str(o).strip()]
    opts = [o if isinstance(o, dict)
            else {"id": f"option-{n + 1}", "title": str(o).strip()[:120], "how_it_fits": str(o).strip()}
            for n, o in enumerate(opts)]
    # Normalise the plan-divergence flag to a REAL bool — the agent passes a string ("yes"/"no"),
    # and a literal "no" is truthy in JS, so the UI badge would fire on every option otherwise.
    for o in opts:
        if isinstance(o, dict):
            d = o.get("diverges_from_plan")
            o["diverges_from_plan"] = d is True or str(d).strip().lower() in ("yes", "true", "1", "y")
            o["divergence_note"] = (o.get("divergence_note") or "").strip()
    verified, unverified = _verify_evidence(ctx, evidence or [])
    # Gate on grounding: a genuine reuse-vs-new call needs at least 2 verified citations
    # (e.g. the transaction-carrying flow + a consumer). Don't record an ungrounded
    # proposal — bounce the agent back to read the code.
    if len(verified) < 2:
        read_n = len(ctx.read_files)
        _uv = "; ".join(f"{u.get('file') or '?'} ({u.get('why_unverified') or 'not read'})"
                        for u in unverified[:6])
        return ("propose_approach NOT recorded — your reuse-vs-new decision is not grounded in code. "
                f"You provided {len(verified)} verified citation(s) (files you actually read) but need ≥2. "
                + (f"Unverified: {_uv}. " if _uv else "")
                + f"You've read {read_n} file(s) so far. Open the actual flow + consumer files "
                "(find_existing_xsd, grep for the transaction/debit-credit leg, read_file them), "
                "then call propose_approach again citing what you read in `evidence` "
                "(include `line` for a partially-read file).")
    ctx.proposal = {"summary": summary, "options": opts, "recommended": recommended,
                    "evidence": verified, "unverified_evidence": unverified}
    ctx.awaiting_decision = True
    note = (f" ({len(unverified)} citation(s) ignored — not actually read)" if unverified else "")
    return (f"approach proposal recorded ({len(opts)} option(s); recommended={recommended}; "
            f"{len(verified)} grounded citation(s){note}). "
            "Stopping for the human's decision — do not edit anything.")


def propose_revision(ctx: RunContext, summary: str, options: list,
                     recommended: str | None = None) -> str:
    """Disruptive-revision conversation (refine loop), one round per request: the human
    asked for a change that would genuinely BREAK things (never additive work, never mere
    plan/approach divergence). Instead of silently declining, STOP and converse — explain
    what breaks and offer 2-3 SAFER, immediately-implementable alternatives that achieve
    their underlying goal. The human picks one, or explicitly proceeds anyway (recorded as
    accepted risk) — either way their next answer is final."""
    opts = options if isinstance(options, list) else []
    # Drop blanks before numbering — keep synthesized ids contiguous (see propose_approach).
    opts = [o for o in opts if isinstance(o, dict) or str(o).strip()]
    opts = [o if isinstance(o, dict)
            else {"id": f"option-{n + 1}", "title": str(o).strip()[:120], "how_it_fits": str(o).strip()}
            for n, o in enumerate(opts)]
    ctx.proposal = {"kind": "revision", "summary": summary, "options": opts,
                    "recommended": recommended}
    ctx.awaiting_decision = True
    return (f"revision proposal recorded ({len(opts)} safer option(s)). Stopping for the "
            "human's decision — do not apply the disruptive change.")


def flag_concern(ctx: RunContext, message: str, severity: str = "warning",
                 declined_change: str | None = None) -> str:
    """Record a concern about a requested change. With ``declined_change`` set (genuine
    breakage only — would break existing consumers, drop/rename a required in-use element,
    change a JAXB-bound type, or violate the transaction-flow contract), the change is
    declined and only the safe parts applied. Without it, the concern is an on-record
    objection and the request is still applied — the human's explicit ask wins."""
    ctx.concerns.append({"severity": severity or "warning", "message": message,
                         "declined_change": declined_change})
    if declined_change:
        return "concern recorded — declined change NOT applied; continue with the safe parts."
    return "objection recorded — now APPLY the request as the human asked."


def ask_clarifications(ctx: RunContext, questions: list) -> str:
    """Change-Analysis gate (kind='analysis'): after reading the code, ask the PRODUCT
    MANAGER one BATCH of implementation-shaping questions and STOP. Each question is
    functional (the PM is schema-literate but not a developer); each carries 2-4 options
    and a per-option plain-language consequence sourced from the code you read. Code
    MECHANISM questions are NOT asked here — they are deferred to Phase B.
    ENDS your pass; the orchestrator stops at the clarifications gate.

    Anti-hallucination gate: an option that proposes a concrete NEW value (proposed_value)
    is the exact spot where an invented identifier becomes a human-clickable choice and
    then a binding ledger directive — so those options are (1) refused without verified
    evidence citing files actually read this run, and (2) occupancy-checked server-side,
    with the result attached to the option the PM sees. A recommendation may not ride on
    a value the platform found occupied (or could not scan)."""
    qs = [q for q in (questions if isinstance(questions, list) else []) if isinstance(q, dict)]
    value_opts = [(q, o) for q in qs for o in (q.get("options") or [])
                  if isinstance(o, dict) and str(o.get("proposed_value") or "").strip()]
    stripped: list[str] = []
    if value_opts:
        evidence = [e for q in qs for e in (q.get("evidence") or []) if isinstance(e, dict)]
        verified, unverified = _verify_evidence(ctx, evidence)
        if not verified:
            vals = ", ".join(sorted({str(o.get("proposed_value")).strip() for _, o in value_opts}))
            return ("ask_clarifications NOT recorded — option(s) propose concrete value(s) "
                    f"({vals}) with no verified evidence. A proposed value must come from the "
                    "code, not memory: grep the existing value space, read the file(s) that "
                    "define it (constants/enums/registry), then call again with `evidence` "
                    "citing those files. Options that DEFER the value (authority assigns it "
                    "later / PM provides it) need no proposed_value and no evidence.")
        for q, o in value_opts:
            occ = _value_occupancy(ctx, str(o["proposed_value"]))
            o["occupancy"] = occ
            if not occ["complete"]:
                marker = (f" [platform check on '{occ['value']}' INCOMPLETE — availability "
                          "unverified]")
            elif occ["hits"]:
                marker = (f" [platform check: '{occ['value']}' already appears at "
                          f"{occ['hits']} code location(s) — availability NOT confirmed]")
            else:
                marker = (f" [platform check: no quoted occurrence of '{occ['value']}' in "
                          f"{len(occ['repos_scanned'])} repo(s)]")
            o["consequence"] = (str(o.get("consequence") or "").rstrip() + marker).strip()
            # The PM must never see "recommended" on a value the platform found occupied
            # or could not scan — the evidence stays on the option; the endorsement goes.
            if (occ["hits"] or not occ["complete"]) and q.get("recommended") == o.get("id"):
                q["recommended"] = None
                stripped.append(occ["value"])
            # Auto-fact: the occupancy verdict is server truth — pin it so it cannot fade
            # out of attention later in this run (and it travels to later phases).
            if occ["complete"]:
                _add_fact(ctx,
                          (f"proposed value '{occ['value']}' already appears at {occ['hits']} "
                           f"quoted code location(s), e.g. {occ['sample'][0]}" if occ["hits"]
                           else f"proposed value '{occ['value']}' has no quoted occurrence in "
                                f"{len(occ['repos_scanned'])} scanned repo(s)"),
                          source="platform:occupancy-check", kind="verified")
        for q in qs:
            if any(isinstance(o, dict) and str(o.get("proposed_value") or "").strip()
                   for o in (q.get("options") or [])):
                q["verified_evidence"] = verified
                if unverified:
                    q["unverified_evidence"] = unverified
    ctx.proposal = {"kind": "clarifications", "questions": qs}
    ctx.awaiting_decision = True
    note = ""
    if value_opts:
        note = f"; {len(value_opts)} value-proposing option(s) occupancy-checked"
        if stripped:
            note += ("; recommendation removed from " + ", ".join(sorted(set(stripped)))
                     + " — the PM sees the occupancy evidence instead")
    return (f"clarifications recorded ({len(qs)} question(s){note}). Stopping for the PM's "
            "answers — do not analyse further until they respond.")


def _coerce_plan_view(value, name: str):
    """propose_plan's views must be JSON OBJECTS. The model occasionally hand-serializes
    one into a (sometimes malformed) JSON string — if we store that, the UI can't read it
    (e.g. functional_plan.overview renders as "(no overview)"). Parse a string back to a dict
    via the lenient recovery; if it can't be recovered, return an error so the tool rejects
    the call and the model retries with a real object. Mirrors the propose_approach string
    coercion (commit 3398a595)."""
    if value is None or isinstance(value, dict):
        return (value or {}), None
    if isinstance(value, str):
        from app.core.json_recovery import parse_llm_json_sync
        parsed = parse_llm_json_sync(value, fallback=None)
        if isinstance(parsed, dict):
            return parsed, None
        return None, (f"{name} was passed as a string that is not a valid JSON object. Pass "
                      f"{name} as a JSON OBJECT (not an escaped/stringified JSON), then call "
                      "propose_plan again.")
    return None, f"{name} must be a JSON object, got {type(value).__name__}."


def propose_plan(ctx: RunContext, summary: str, functional_plan: dict,
                 technical_analysis: dict, flow_spec: dict | None = None,
                 evidence: list | None = None) -> str:
    """Change-Analysis final gate: present the implementation PLAN for ratification. Two
    views: functional_plan (PM-facing business language; every statement derived from a
    technical finding) and technical_analysis (full fidelity — impacted repos/modules/flows,
    real XSD files+namespaces, data_model_changes, reuse findings). flow_spec is the
    machine-readable actors/steps/messages/states that owns step IDs the BRD/TSD render from.

    EVIDENCE is required and validated like propose_approach: ≥2 citations to files you
    actually READ this run. An ungrounded plan is NOT recorded — go read the code first."""
    verified, unverified = _verify_evidence(ctx, evidence or [])
    if len(verified) < 2:
        read_n = len(ctx.read_files)
        _uv = "; ".join(f"{u.get('file') or '?'} ({u.get('why_unverified') or 'not read'})"
                        for u in unverified[:6])
        return ("propose_plan NOT recorded — the plan is not grounded in code. "
                f"You provided {len(verified)} verified citation(s) but need ≥2. "
                + (f"Unverified: {_uv}. " if _uv else "")
                + f"You've read {read_n} file(s). Read the impacted flow + a consumer, then "
                "call propose_plan again citing what you read in `evidence` "
                "(include `line` for a partially-read file).")
    # The model sometimes passes a view as a (possibly malformed) JSON string instead of an
    # object; coerce it back to a dict, and reject the call if it's unrecoverable so we never
    # persist a string the UI can't read (functional_plan.overview → "(no overview)").
    fp, fp_err = _coerce_plan_view(functional_plan, "functional_plan")
    ta, ta_err = _coerce_plan_view(technical_analysis, "technical_analysis")
    fs, fs_err = _coerce_plan_view(flow_spec, "flow_spec")
    for err in (fp_err, ta_err, fs_err):
        if err:
            return "propose_plan NOT recorded — " + err
    # Party-flow requirement, enforced in-loop like the evidence rule (a prompt-only
    # mandate was skipped in practice): every UPI message the plan's technical surface
    # touches needs a party_flows entry. Detection is by message TOKEN, not file name —
    # messages usually live inside combined schema files (network-meta.xsd).
    from app.agents.plan_files import touched_message_stems
    stems = touched_message_stems(ta, fs)
    if stems:
        entries = [e for e in ((fs or {}).get("party_flows") or []) if isinstance(e, dict)]
        # A flow counts as covering a message when the wire token appears in its `api`
        # OR in any hop's `message` — a business-named flow ("Balance Enquiry") whose
        # hops route RespBalEnq is fully grounded and must not bounce the plan.
        covered = {t.lower() for e in entries
                   for src in ([str(e.get("api") or "")]
                               + [str(hp.get("message") or "") for hp in (e.get("hops") or [])
                                  if isinstance(hp, dict)])
                   for t in re.findall(r"[A-Za-z0-9_]+", src)}
        missing = [s for s in stems if s.lower() not in covered]
        if missing:
            return ("propose_plan NOT recorded — flow_spec.party_flows is missing an entry "
                    "for: " + ", ".join(missing) + ". For EACH touched message add "
                    "{api, classification:'new'|'existing_modified', parties:[only those "
                    "actually involved], hops:[{from, to, message, evidence, "
                    "confidence:'confirmed'|'assumed'}]}. Derive hops from the code you read "
                    "(flow_context/handlers) and confirm with domain_docs_search; a hop backed by "
                    "neither is confidence='assumed'. Then call propose_plan again.")
    ctx.proposal = {
        "kind": "plan",
        "summary": summary,
        "functional_plan": fp,
        "technical_analysis": ta,
        "flow_spec": fs,
        "evidence": verified,
        "unverified_evidence": unverified,
    }
    ctx.awaiting_decision = True
    return (f"plan recorded ({len(verified)} grounded citation(s)). Stopping for "
            "ratification — PM ratifies the functional plan, tech-lead the technical analysis.")


def run_command(ctx: RunContext, repo_id: str | None = None, argv: list | None = None,
                timeout_s: int | None = None) -> str:
    """DIAGNOSIS ONLY (§9.3/§9.4): run an allowlisted command in the clone and
    return its raw output so the agent can investigate a build/test failure
    (e.g. `mvn compile`, `mvn dependency:tree`, `git diff`, tail a log).

    NON-GATING + NON-FATAL by construction: nothing run here affects the
    verification verdict — the runtime's VerificationPlan owns that — and a
    non-zero exit or timeout is *information*, never a run failure. Every command
    is allowlisted (PlatformAdapter) + git-guarded + logged (the runtime emits a
    tool_call event per call, which is the observation hook)."""
    repo_id = _resolve_repo_id(ctx, repo_id)
    assert_repo_selected(repo_id, ctx.selected_repo_ids)
    from app.agents.platform_adapter import adapter, CommandNotAllowed, ALLOWED_ARGV0
    if isinstance(argv, str):
        import json
        try:
            parsed = json.loads(argv)
            if isinstance(parsed, list):
                argv = parsed
        except (json.JSONDecodeError, TypeError):
            pass
    if not isinstance(argv, list) or not argv:
        raise ToolError('run_command needs argv as a non-empty JSON array of strings, '
                        'e.g. ["mvn","compile"] — got: ' + repr(argv)[:120])
    # Path containment: reject absolute-path arguments to file-reading commands
    # that could leak host files outside the workspace (e.g. `cat /etc/shadow`).
    _FILE_READING_CMDS = {"cat", "head", "tail", "grep", "ls"}
    from app.agents.platform_adapter import _argv0_key
    cmd_key = _argv0_key(str(argv[0]))
    if cmd_key in _FILE_READING_CMDS:
        root = _repo_root(ctx, repo_id).resolve()
        for arg in argv[1:]:
            a = str(arg)
            if a.startswith("-"):
                continue
            if a.startswith("/"):
                resolved = Path(a).resolve()
                if root not in resolved.parents and resolved != root:
                    raise ToolError(
                        f"absolute path {a!r} is outside the workspace — "
                        "use repo-relative paths or the read_file/grep tools"
                    )
            if ".." in a.split("/"):
                tentative = (root / a).resolve()
                if root not in tentative.parents and tentative != root:
                    raise ToolError(
                        f"path {a!r} escapes the workspace via '..' traversal"
                    )
    try:
        res = adapter.run_command(_repo_root(ctx, repo_id), [str(a) for a in argv], timeout_s=timeout_s)
    except CommandNotAllowed as e:
        raise ToolError(f"command not allowed: {e} — allowed: {', '.join(sorted(ALLOWED_ARGV0))}. "
                        "For file search use the glob/grep TOOLS, not shell find/grep.")
    out = (res.stdout or "")
    err = (res.stderr or "")
    full = out + (("\n--- stderr ---\n" + err) if err.strip() else "")
    # Give the agent the REAL output so it can diagnose with its own reasoning. Only
    # cap on genuinely huge logs — and when we do, keep the actual ERROR lines (which
    # live in the MIDDLE of a long reactor build) PLUS a generous tail, not a head/tail
    # snippet that throws the diagnostics away.
    _CAP = 16000
    if len(full) > _CAP:
        errs = [ln for ln in full.splitlines()
                if "[ERROR]" in ln or "error:" in ln.lower() or "BUILD FAILURE" in ln][:150]
        # Retrieval pointer (P1): keep the FULL output retrievable so a diagnostic line that
        # fell outside the excerpt can be paged in with read_output — re-running a build to
        # see the rest is slow and may not even reproduce.
        oid = _stash_output(ctx, full)
        full = (f"[long output ({len(full)} chars) — showing all error lines + the tail; the FULL "
                f"output is cached: read_output(id=\"{oid}\", start_line=…, end_line=…) pages "
                f"through the rest]\n"
                + "\n".join(errs) + "\n…\n" + full[-9000:])
    return (f"exit={res.exit_code} timed_out={res.timed_out} ({res.duration_ms}ms) [diagnosis, non-gating]\n"
            f"$ {' '.join(str(a) for a in argv)}\n{full}")


_OUTPUT_STASH_MAX = 8          # bounded memory: keep only the most recent truncated outputs


def _stash_output(ctx: RunContext, full: str) -> str:
    """Cache a truncated command's FULL output on the run context, FIFO-bounded. Returns the id.
    Ids are monotone over the run (max existing + 1) so an evicted id is never reused — a stale
    marker in old history must error clearly, not silently serve a DIFFERENT command's output."""
    oid = f"out{max((int(k[3:]) for k in ctx.command_outputs), default=0) + 1}"
    while len(ctx.command_outputs) >= _OUTPUT_STASH_MAX:
        ctx.command_outputs.pop(next(iter(ctx.command_outputs)))
    ctx.command_outputs[oid] = full
    return oid


def read_output(ctx: RunContext, id: str = "", start_line: int | None = None,
                end_line: int | None = None) -> str:
    """Page through the FULL output of an earlier truncated run_command call (see the
    [long output … read_output(id=…)] marker). Line-ranged; each page is itself capped."""
    if not id:
        raise ToolError('read_output needs the id from the truncation marker, e.g. "out1"')
    full = ctx.command_outputs.get(id)
    if full is None:
        avail = ", ".join(ctx.command_outputs) or "(none cached this run)"
        raise ToolError(f"no cached output {id!r} — available: {avail}. Only truncated "
                        "run_command outputs are cached; re-run the command otherwise.")
    lines = full.splitlines()
    lo = max((start_line or 1) - 1, 0)
    hi = min(end_line or len(lines), len(lines))
    body = "\n".join(lines[lo:hi])
    if len(body) > 16000:
        body = body[:16000] + f"\n…[page truncated — narrow the line range; {id} has {len(lines)} lines total]"
    return f"[{id} lines {lo + 1}-{hi} of {len(lines)}]\n{body}"


def _calls_index_ok(ctx: RunContext) -> bool:
    """Cached coverage probe: does ANY selected repo have a populated ``calls`` edge?

    ``False`` means the symbol-graph index is structurally BLIND for this run (the extractor
    was off at ingest, or the index is stale) — so an empty ``symbol_graph``/``callers``/
    ``impact_analysis`` result means *cannot determine*, NOT *nothing exists*. Probes once per
    run (LIMIT 1) and caches on the context. Fail-open to ``True`` so a probe error never makes
    us over-warn and train the model to distrust good data."""
    if ctx.index_calls_ok is not None:
        return ctx.index_calls_ok
    ok = True
    if ctx.db is not None:
        try:
            from sqlalchemy import String, cast
            from app.models.document_chunk import DocumentChunk
            rid_col = DocumentChunk.metadata_["repo_id"].as_string()
            hit = (ctx.db.query(DocumentChunk)
                   .filter(rid_col.in_(list(ctx.selected_repo_ids)),
                           DocumentChunk.calls.isnot(None),
                           cast(DocumentChunk.calls, String) != "[]")
                   .limit(1).first())
            ok = hit is not None
        except Exception as e:                       # noqa: BLE001 — fail-open (don't over-warn)
            logger.debug("calls-index coverage probe failed (fail-open): %s", e)
            ok = True
    ctx.index_calls_ok = ok
    return ok


def _blind_index_note(ctx: RunContext) -> str:
    """Sharp, deterministic suffix appended to an EMPTY structural-intel result when the
    call-graph index is unpopulated — so the model can't read emptiness as 'safe to change'.
    Empty string when the index is populated (the empty is then a genuine 'no callers')."""
    if _calls_index_ok(ctx):
        return ""
    return ("\n⚠ the code index has NO call-graph edges for these repos (the symbol-graph extractor "
            "was off at ingest, or the index is stale) — an EMPTY result here means 'cannot determine', "
            "NOT 'none exist'. Use ast_query + grep across all repos and treat the blast radius as "
            "UNKNOWN; do NOT infer that a change is safe from this emptiness.")


def symbol_graph(ctx: RunContext, repo_id: str | None = None, symbol: str = "") -> str:
    """Structural nav (§8): where a symbol is DEFINED + its callers, from the
    indexed calls/called_by graph (Slice 17). Cheapest tier, no live process.

    Reads the INDEX, which can lag the clone — results are labelled advisory;
    confirm a specific signature/caller with ast_query or read_file."""
    if not symbol:
        raise ToolError("symbol_graph needs a symbol")
    repo_id = _resolve_repo_id(ctx, repo_id)
    assert_repo_selected(repo_id, ctx.selected_repo_ids)
    if ctx.db is None:
        return "(symbol graph unavailable — use ast_query/grep)"
    ctx.intel_queried.add(f"symbol:{symbol}")
    from sqlalchemy import String, cast
    from app.models.document_chunk import DocumentChunk
    rid = DocumentChunk.metadata_["repo_id"].as_string()
    defs = (ctx.db.query(DocumentChunk)
            .filter(rid == repo_id, DocumentChunk.symbol_name == symbol).limit(10).all())
    # Narrow with an ESCAPED LIKE (so `_`/`%` in a Java identifier aren't treated
    # as wildcards — `process_refund` must not match `processXrefund`), then
    # confirm EXACT membership in Python (calls is a JSON string array).
    esc = symbol.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    candidates = (ctx.db.query(DocumentChunk)
                  .filter(rid == repo_id,
                          cast(DocumentChunk.calls, String).ilike(f'%"{esc}"%', escape="\\"))
                  .limit(100).all())
    callers = [c for c in candidates if symbol in (c.calls or [])][:25]
    lines = [f"[symbol_graph — from index, may be stale; confirm with ast_query/read_file]"]
    if defs:
        lines.append("defined:")
        for d in defs:
            lines.append(f"  {d.source_file}:{d.line_start or '?'} ({d.symbol_kind or 'symbol'})"
                         + (f" calls={d.calls}" if d.calls else ""))
    else:
        lines.append(f"(no indexed definition for {symbol!r})")
    if callers:
        lines.append("callers (methods that invoke it):")
        for c in callers[:25]:
            lines.append(f"  {c.source_file}:{c.line_start or '?'} {c.symbol_name or ''}")
    note = _blind_index_note(ctx)
    if note and not defs and not callers:            # fully empty AND the index is blind → don't read as 'absent'
        lines.append(note.strip())
    return "\n".join(lines)


def callers(ctx: RunContext, symbol: str = "", repo_id: str | None = None) -> str:
    """Cross-file, cross-REPO callers of a method/symbol (who invokes it) from the indexed
    call graph — the consumer list to update BEFORE you change a signature. Unlike symbol_graph
    (one repo, def+callers), this sweeps ALL selected repos, which is where a multi-repo UPI
    change breaks: the schema/type lives in core, the callers in the app repo. Advisory (the
    index can lag the clone) — confirm with grep/read_file, and grep the name for a full sweep."""
    if not symbol:
        raise ToolError("callers needs a symbol")
    if ctx.db is None:
        return "(call graph unavailable — grep the symbol name across repos instead)"
    ctx.intel_queried.add(f"symbol:{symbol}")
    from sqlalchemy import String, cast
    from app.models.document_chunk import DocumentChunk
    repos = [_resolve_repo_id(ctx, repo_id)] if repo_id else list(ctx.selected_repo_ids)
    for rid in repos:
        assert_repo_selected(rid, ctx.selected_repo_ids)
    # ESCAPED LIKE so `_`/`%` in an identifier aren't wildcards, then confirm EXACT membership
    # in Python (calls is a JSON string array) — mirrors symbol_graph's caller query.
    esc = symbol.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rid_col = DocumentChunk.metadata_["repo_id"].as_string()
    candidates = (ctx.db.query(DocumentChunk)
                  .filter(rid_col.in_(repos),
                          cast(DocumentChunk.calls, String).ilike(f'%"{esc}"%', escape="\\"))
                  .limit(200).all())
    hits = [c for c in candidates if symbol in (c.calls or [])]
    if not hits:
        return (f"(no indexed callers of {symbol!r} across {len(repos)} repo(s) — the index may "
                "lag; grep the name across all repos to be sure)" + _blind_index_note(ctx))
    lines = [f"[callers of {symbol!r} — from index across all selected repos, may lag; confirm with grep/read_file]"]
    for c in hits[:40]:
        rid = (c.metadata_ or {}).get("repo_id", "?")
        lines.append(f"  {rid}  {c.source_file}:{c.line_start or '?'}  {c.symbol_name or ''}")
    return "\n".join(lines)


def impact_analysis(ctx: RunContext, symbol: str = "", repo_id: str | None = None) -> str:
    """Blast radius of changing ``symbol`` (a method/class/interface): who CALLS it
    (consumers to update before a signature change), who SUBCLASSES / IMPLEMENTS it,
    and the set of affected files — your SCOPE FENCE. Edit within this set; touching
    files OUTSIDE it is scope drift unless the plan requires it. Richer than `callers`
    (adds inheritance + the fence). Prefers the AGE impact graph when operators enable
    it, else a pure-SQL caller+inheritance sweep across selected repos. Advisory (the
    index can lag) — confirm a specific signature with grep/read_file."""
    if not symbol:
        raise ToolError("impact_analysis needs a symbol")
    if ctx.db is None:
        return "(impact graph unavailable — use callers + grep across repos instead)"
    ctx.intel_queried.add(f"symbol:{symbol}")
    # 1) AGE-backed analyzer when operators have enabled it — one pass yields callers +
    #    subclasses + implementations + documenting docs. Fail-open to the SQL path.
    from app.core.config import settings
    if getattr(settings, "use_impact_analyzer", False):
        try:
            from app.kg.impact_analyzer import analyze_impact
            rep = analyze_impact(db=ctx.db, target_symbols=[(symbol, None)])
            if rep.total_impacted() > 0:
                return _format_impact_report(symbol, rep)
        except Exception as e:                       # noqa: BLE001 — degrade to the SQL path
            logger.warning("impact_analysis AGE path failed for %r: %s", symbol, e)
    # 2) Pure-SQL fallback (no AGE): repo-scoped callers + inheritance neighbours,
    #    straight off the document_chunks JSON columns (needs the symbol-graph extractor
    #    to have run at ingest, else it returns empty and the agent falls back to grep).
    return _impact_sql_fallback(ctx, symbol, repo_id)


def _format_impact_report(symbol: str, rep) -> str:
    lines = [f"[impact_analysis of {symbol!r} — from the impact graph; advisory, confirm with grep/read_file]"]
    if rep.files_affected:
        lines.append("SCOPE FENCE — files that depend on this symbol (edit within this set; going "
                     "outside is scope drift unless the plan requires it):")
        lines += [f"  {f}" for f in rep.files_affected[:40]]
    lines.append(f"impacted: callers={len(rep.callers)} subclasses={len(rep.subclasses)} "
                 f"implementations={len(rep.implementations)} documenting_docs={len(rep.documenting)} "
                 f"total={rep.total_impacted()}")
    return "\n".join(lines)


def _impact_sql_fallback(ctx: RunContext, symbol: str, repo_id: str | None) -> str:
    from sqlalchemy import String, cast
    from app.models.document_chunk import DocumentChunk
    repos = [_resolve_repo_id(ctx, repo_id)] if repo_id else list(ctx.selected_repo_ids)
    for rid in repos:
        assert_repo_selected(rid, ctx.selected_repo_ids)
    rid_col = DocumentChunk.metadata_["repo_id"].as_string()
    # ESCAPED LIKE so `_`/`%` in an identifier aren't wildcards, then confirm EXACT
    # membership in Python (calls/implements are JSON string arrays) — mirrors `callers`.
    esc = symbol.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    call_cands = (ctx.db.query(DocumentChunk)
                  .filter(rid_col.in_(repos),
                          cast(DocumentChunk.calls, String).ilike(f'%"{esc}"%', escape="\\"))
                  .limit(200).all())
    callers_hits = [c for c in call_cands if symbol in (c.calls or [])]
    subclass_hits = (ctx.db.query(DocumentChunk)
                     .filter(rid_col.in_(repos), DocumentChunk.inherits == symbol)
                     .limit(100).all())
    impl_cands = (ctx.db.query(DocumentChunk)
                  .filter(rid_col.in_(repos),
                          cast(DocumentChunk.implements, String).ilike(f'%"{esc}"%', escape="\\"))
                  .limit(100).all())
    impl_hits = [c for c in impl_cands if symbol in (c.implements or [])]
    if not (callers_hits or subclass_hits or impl_hits):
        return (f"(no indexed impact for {symbol!r} across {len(repos)} repo(s) — the index may lag, "
                "or the symbol-graph extractor was off at ingest; grep the name across repos to be sure)"
                + _blind_index_note(ctx))

    def _rid(c):
        return (c.metadata_ or {}).get("repo_id", "?")
    affected = sorted({f"{_rid(c)}  {c.source_file}" for c in (callers_hits + subclass_hits + impl_hits)})
    lines = [f"[impact_analysis of {symbol!r} — SQL caller+inheritance sweep across {len(repos)} repo(s); "
             "advisory, confirm with grep/read_file]",
             "SCOPE FENCE — files that depend on this symbol (edit within this set; going outside is "
             "scope drift unless the plan requires it):"]
    lines += [f"  {f}" for f in affected[:40]]
    if callers_hits:
        lines.append(f"callers ({len(callers_hits)}): " + ", ".join(
            f"{c.symbol_name or '?'}@{c.source_file}:{c.line_start or '?'}" for c in callers_hits[:20]))
    if subclass_hits:
        lines.append(f"subclasses ({len(subclass_hits)}): " + ", ".join(
            f"{c.symbol_name or c.source_file}" for c in subclass_hits[:20]))
    if impl_hits:
        lines.append(f"implementors ({len(impl_hits)}): " + ", ".join(
            f"{c.symbol_name or c.source_file}" for c in impl_hits[:20]))
    return "\n".join(lines)


def jaxb_accessors(ctx: RunContext, element: str = "", repo_id: str | None = None) -> str:
    """The Java class/symbol bound to an XSD element, from the JAXB element→Java link index —
    so you open the RIGHT generated/consumer class instead of guessing the getter/setter name.
    Advisory: it points you at the class; confirm the EXACT getX()/setX()/isX() with read_file or
    ast_query (xjc pluralizes/camel-cases unpredictably and is never guessable)."""
    if not element:
        raise ToolError("jaxb_accessors needs an element name")
    if ctx.db is None:
        return "(JAXB link index unavailable — open the generated class with read_file/ast_query)"
    from app.models.xsd_graph import XsdJavaLink
    repos = [_resolve_repo_id(ctx, repo_id)] if repo_id else list(ctx.selected_repo_ids)
    for rid in repos:
        assert_repo_selected(rid, ctx.selected_repo_ids)
    rows = (ctx.db.query(XsdJavaLink)
            .filter(XsdJavaLink.repo_id.in_(repos), XsdJavaLink.xpath == element)
            .order_by(XsdJavaLink.confidence.desc()).limit(20).all())
    if not rows:
        return (f"(no JAXB link indexed for element {element!r} — it may be newly added, or the "
                "binding wasn't mapped; open the generated class with read_file/ast_query)")
    lines = [f"[JAXB links for {element!r} — advisory; open the class to confirm exact accessors]"]
    for r in rows:
        lines.append(f"  {r.symbol_chunk_id_or_path or '?'}  (source={r.source or '?'}, conf={(r.confidence or 0.0):.2f})")
    return "\n".join(lines)


def show_diff(ctx: RunContext, repo_id: str | None = None) -> str:
    """Show YOUR accumulated change-set so far — every file you've added/modified/deleted this run,
    as a unified diff against the base. SELF-CHECK with it before verify_change and before you
    finish: did you touch every file the plan needs, and nothing you didn't intend? Ground truth
    from the working tree (includes the Phase-A XSD edits). Build output is excluded."""
    from app.agents import workspace_local
    from app.agents.platform_adapter import adapter
    ws = ctx.workspace_run_id or ctx.run_id
    repos = [_resolve_repo_id(ctx, repo_id)] if repo_id else list(ctx.selected_repo_ids)
    out: list[str] = []
    for rid in repos:
        assert_repo_selected(rid, ctx.selected_repo_ids)
        try:
            rd = workspace_local.repo_dir(ws, rid)
            paths = [p for _op, p in workspace_local.changed_files(ws, rid)]
            if not paths:
                continue
            adapter.run_command(rd, ["git", "add", "-A", "-N", "--", *paths])   # show new files in the diff
            res = adapter.run_command(rd, ["git", "diff", "HEAD", "--", *paths])
            adapter.run_command(rd, ["git", "reset", "-q", "--", *paths])        # undo the intent-to-add
            d = (getattr(res, "stdout", "") or "").strip()
            if d:
                out.append(f"# {rid}\n" + (d if len(d) <= 20000 else d[:20000] + "\n… (truncated — read the file for the rest)"))
        except Exception as e:  # noqa: BLE001 — introspection must never break the loop
            out.append(f"# {rid}: (could not render diff: {e})")
    return "\n\n".join(out) or "(no changes yet — you have not edited anything this run)"


def git_history(ctx: RunContext, path: str = "", repo_id: str | None = None,
                start_line: int | None = None, end_line: int | None = None) -> str:
    """Why does this code exist? `git blame`/`git log` for a file in the clone — pass a line range to
    BLAME those lines (who/when/which commit last changed each), or omit it for the file's recent
    commit history. Read this BEFORE you change code you don't fully understand (a workaround, an odd
    constant, a guard) so you don't silently undo someone's intent. (Generated JAXB sources stay
    DO-NOT-EDIT regardless of history.)"""
    if not path:
        raise ToolError("git_history needs a path")
    repo_id = _resolve_repo_id(ctx, repo_id, path)
    assert_repo_selected(repo_id, ctx.selected_repo_ids)
    from app.agents import workspace_local
    from app.agents.platform_adapter import adapter
    rd = workspace_local.repo_dir(ctx.workspace_run_id or ctx.run_id, repo_id)
    if start_line or end_line:
        lo = max(int(start_line or 1), 1)
        hi = max(int(end_line or lo), lo)
        res = adapter.run_command(rd, ["git", "blame", "-L", f"{lo},{hi}", "--", path])
        label = f"blame {path}:{lo}-{hi}"
    else:
        res = adapter.run_command(rd, ["git", "log", "-n", "15", "--no-merges",
                                       "--date=short", "--pretty=format:%h %ad %an: %s", "--", path])
        label = f"recent commits for {path}"
    txt = (getattr(res, "stdout", "") or "").strip()
    if not txt:
        # A FAILED git command (bad -L range, bad path, corrupt clone) also has empty
        # stdout — reporting it as "no history / may be new" would hand the agent a
        # false fact about the code. "couldn't check" must never read as "checked".
        err = (getattr(res, "stderr", "") or "").strip()
        if getattr(res, "exit_code", 0) not in (0, None) or err:
            return (f"(git {label} FAILED: {err[:200] or f'exit {res.exit_code}'} — history "
                    f"UNKNOWN, not necessarily a new file; fix the range/path and retry)")
        return f"(no git history for {path!r} — it may be new this change, or the clone is too shallow)"
    return f"[{label}]\n" + (txt if len(txt) <= 8000 else txt[:8000] + "\n… (truncated)")


def ast_query(ctx: RunContext, repo_id: str | None = None, path: str = "") -> str:
    """Structural nav (§8): tree-sitter parse of a Java file in the WORKING TREE
    (ground truth — no staleness). Returns its classes/kind/inheritance, methods,
    and imports so the agent navigates structure precisely instead of guessing."""
    if not path:
        raise ToolError("ast_query needs a path")
    repo_id = _resolve_repo_id(ctx, repo_id, path)
    assert_repo_selected(repo_id, ctx.selected_repo_ids)
    if not path.lower().endswith(".java"):
        return "(ast_query supports .java only — use read_file/grep for other files)"
    target = _resolve(ctx, repo_id, path)
    if not target.is_file():
        raise ToolError(f"file not found: {path}")
    ctx.intel_queried.add(f"path:{repo_id}:{path}")
    from app.rag.symbol_graph_extractor_java import extract, to_dict
    g = to_dict(extract(target.read_text(encoding="utf-8", errors="replace")))
    out = [f"[ast_query — parsed from the working tree (ground truth): {path}]"]
    if g.get("imports"):
        out.append("imports: " + ", ".join(g["imports"][:30]))
    for cls in g.get("classes", []):
        head = f"{cls.get('kind','class')} {cls.get('name')}"
        if cls.get("inherits"):
            head += f" extends {cls['inherits']}"
        if cls.get("implements"):
            head += f" implements {', '.join(cls['implements'])}"
        out.append(head)
        for m in cls.get("methods", []):
            out.append(f"  - {m.get('name')}()" + (f" calls {m['calls']}" if m.get("calls") else ""))
    return "\n".join(out)


def find_existing_xsd(ctx: RunContext, repo_id: str | None = None, query: str = "") -> str:
    """Prefer-existing: look up indexed XSD schemas by FILE NAME or NAMESPACE, so the agent
    extends an existing schema before creating a new one (§7.4). Omitting ``repo_id`` searches
    ALL selected repos — the schemas usually live in the core/framework repo, not the business
    one. Element/type/attribute names are NOT indexed: to find the schema that defines a TYPE
    (e.g. payTrans) or an attribute, grep the .xsd files instead."""
    if repo_id and repo_id != "all":
        assert_repo_selected(repo_id, ctx.selected_repo_ids)
    if ctx.db is None:
        return "(xsd index unavailable — grep/glob the .xsd files directly)"
    from app.models.xsd_graph import XsdSchemaNode
    from app.agents.xsd_namespace import namespace_variant_note, sibling_namespace_spellings
    # Match on each WORD, not the whole phrase: the query is one ILIKE against a path, so a
    # natural multi-word ask ("network-common payTrans note") matched nothing while "network-common"
    # alone matched — reuse-first then hinged on how the model happened to phrase it.
    terms = [t for t in re.split(r"[^\w.\-]+", query or "") if len(t) > 1]
    match = None
    for t in terms:
        like = f"%{t}%"
        m = XsdSchemaNode.path.ilike(like) | XsdSchemaNode.target_namespace.ilike(like)
        match = m if match is None else (match | m)
    # Namespace spelling-variant awareness: if the query names a known NPCI
    # namespace (in any spelling), also match its sibling spellings so reuse
    # doesn't silently fail across the npci.org / www.npci.org.in split (§7.4).
    for sib in sibling_namespace_spellings(query):
        sm = XsdSchemaNode.target_namespace.ilike(f"%{sib}%")
        match = sm if match is None else (match | sm)
    q = ctx.db.query(XsdSchemaNode)
    if match is not None:                       # no usable terms → list what IS indexed
        q = q.filter(match)
    if repo_id and repo_id != "all":
        q = q.filter(XsdSchemaNode.repo_id == repo_id)
    else:
        q = q.filter(XsdSchemaNode.repo_id.in_(ctx.selected_repo_ids or []))
    rows = q.limit(50).all()
    if rows and terms:
        # OR-ing the terms is what stops a false "not found", but it also lets one generic word
        # ("schema", matching every namespace) drag in the whole repo. Keep only the rows that
        # matched the MOST terms — the specific token wins over the filler.
        scored = [(sum(t.lower() in f"{r.path} {r.target_namespace}".lower() for t in terms), r)
                  for r in rows]
        best = max(s for s, _ in scored)
        rows = [r for s, r in scored if s == best]
    rows = rows[:20]
    if not rows:
        # "didn't match" ≠ "doesn't exist". The old wording asserted absence, and the prompt
        # tells the agent a search with no matches proves the symbol is gone — together that
        # invites creating a duplicate of a schema that is sitting right there.
        indexed = (ctx.db.query(XsdSchemaNode)
                   .filter(XsdSchemaNode.repo_id.in_(ctx.selected_repo_ids or [])).count())
        return (f"(no schema FILE NAME or NAMESPACE matches {query!r} — {indexed} schema(s) are "
                "indexed for the selected repos. This lookup does NOT cover element/type/attribute "
                "names, so it is NOT evidence the schema is missing: glob '**/*.xsd' and grep for "
                "the type before concluding anything needs to be created.)")
    lines = []
    for r in rows:
        note = namespace_variant_note(r.target_namespace)
        lines.append(f"[repo {r.repo_id}] {r.path}  (ns={r.target_namespace})"
                     + (f"  ⚠ {note}" if note else ""))
    return "\n".join(lines)


def schema_guardian(ctx: RunContext, repo_id: str | None = None, path: str = "") -> str:
    """Deterministic reuse-vs-create CHECK on a proposed XSD (§7.4). Reads the schema at
    ``path`` in the working tree, finds existing schemas in the SAME namespace, and reports
    PER ELEMENT whether it is: redundant (already defined identically → REUSE, don't redefine),
    conflict (defined differently → ESCALATE, don't fork a shared type), or novel (new is
    justified). Call it before finalizing a reuse/extend/new decision or creating a new .xsd.
    Deterministic + advisory — it never decides a breaking change, it escalates to the human."""
    if not path:
        raise ToolError("schema_guardian needs the path to the proposed .xsd")
    repo_id = _resolve_repo_id(ctx, repo_id, path)
    target = _resolve(ctx, repo_id, path)
    if not target.is_file():
        raise ToolError(f"file not found: {path}")
    proposed = target.read_text(encoding="utf-8", errors="replace")
    ctx.intel_queried.add(f"xsd:{repo_id}:{path}")
    from app.agents.schema_guardian import analyze_reuse
    from app.agents.xsd_namespace import same_namespace
    from app.agents.xsd_graph_builder import parse_schema
    tns = parse_schema(proposed).target_namespace
    siblings: list[tuple[str, str]] = []
    if ctx.db is not None and tns:
        from app.models.xsd_graph import XsdSchemaNode
        rows = (ctx.db.query(XsdSchemaNode)
                .filter(XsdSchemaNode.repo_id.in_(ctx.selected_repo_ids or [])).all())
        for r in rows:
            if r.path == path and r.repo_id == repo_id:
                continue                                   # never compare a schema against itself
            if not same_namespace(tns, r.target_namespace):
                continue
            try:
                sib = _resolve(ctx, r.repo_id, r.path)
                if sib.is_file():
                    siblings.append((r.path, sib.read_text(encoding="utf-8", errors="replace")))
            except ToolError:
                continue
    verdict = analyze_reuse(proposed, siblings)
    # "couldn't check" ≠ "checked, none exist": with no index (db None) or an unparsed
    # namespace the comparison never RAN — saying "new is plausible" there invites
    # forking an existing shared schema, the exact failure this tool exists to prevent.
    if siblings:
        footer = f"\n(compared against {len(siblings)} sibling schema(s) in the same namespace)"
    elif ctx.db is None or not tns:
        cause = "the schema index is unavailable" if ctx.db is None else \
                "the proposed schema's targetNamespace could not be parsed"
        footer = (f"\n(⚠ sibling comparison COULD NOT RUN — {cause}. This is NOT evidence "
                  "that no sibling schema exists: grep the repos for this namespace/type "
                  "before deciding to create new)")
    else:
        footer = "\n(no sibling schemas in this namespace are indexed — new is plausible)"
    return verdict.render() + footer


def module_context(ctx: RunContext, repo_id: str | None = None, module: str | None = None) -> str:
    """Module-wise orientation (§19): fetch the index-time ``module_context`` for a
    module BY NAME/PATH. Omit ``module`` to list every module in the repo so the
    agent can choose one. Low-authority orientation — confirm specifics with
    read_file/ast_query."""
    repo_id = _resolve_repo_id(ctx, repo_id)
    assert_repo_selected(repo_id, ctx.selected_repo_ids)
    if ctx.db is None:
        return "(module context unavailable — glob/read_file the pom.xml + sources)"
    from app.models.module_context import ModuleContext
    from app.agents.context_assembler import _format_module
    rows = (ctx.db.query(ModuleContext)
            .filter(ModuleContext.repo_id == repo_id)
            .order_by(ModuleContext.depth, ModuleContext.module_path).all())
    if not rows:
        return "(no module context indexed for this repo — re-index to generate it)"
    names = [r.module_path or "." for r in rows]
    if not module:
        return "modules (call again with one as `module`): " + ", ".join(names)
    q = module.strip().strip("/").lower()
    # match precedence: exact path → basename → substring
    hits = ([r for r in rows if (r.module_path or ".").lower() == q]
            or [r for r in rows if (r.module_path or "").rsplit("/", 1)[-1].lower() == q]
            or [r for r in rows if q in (r.module_path or "").lower()])
    if not hits:
        return f"(no module matches {module!r}; available: {', '.join(names)})"
    return "\n\n".join(_format_module(r) for r in hits[:5])


_ASSESSMENT_ADVISORY = ("[prior DOCUMENT-ONLY XSD assessment — ADVISORY: written from the "
                        "BRD/TSD before any code was examined. Use it as orientation; verify "
                        "every claim against the code and do NOT inherit its conclusion.]")


def read_doc(ctx: RunContext, doc: str | None = None, heading: str | None = None,
             query: str | None = None) -> str:
    """Pull BRD / Tech-Spec / ratified-plan / prior-XSD-assessment content ON DEMAND (§4).
    The prompt carries only a section OUTLINE + a compliance seed; use this to fetch any
    section's full body by heading, or search section bodies by keyword. ``doc`` ∈ {brd,
    tsd, plan, assessment} (omit to span all); omit ``heading`` and ``query`` for the
    outline. ``plan`` serves the FULL ratified Change-Analysis plan — the retrieval path
    behind every CLIPPED/OMITTED marker in the rendered plan digests."""
    docs = ctx.doc_sections or {}
    _ALL = ("brd", "tsd", "assessment", "plan")
    which = [doc] if doc in _ALL else list(_ALL)
    if not any((docs.get(k) or {}) for k in which):
        # Name the doc actually asked for. The old blanket "no BRD/Tech-Spec" fired even when
        # the BRD was attached and only the requested doc was missing, so the agent concluded
        # it had no requirements at all and worked from the change intent alone.
        have = [k for k in _ALL if (docs.get(k) or {})]
        if doc in _ALL and have:
            return (f"(no {doc.upper()} attached to this run — not produced for this change; "
                    f"attached: {', '.join(k.upper() for k in have)} — call read_doc with "
                    f"doc={have[0]!r} or no args for the outline)")
        return "(no BRD/Tech-Spec attached to this run — work from the change intent + the code)"

    def _label(k: str, hh: str) -> str:
        head = f"## {k.upper()} — {hh}"
        return f"{_ASSESSMENT_ADVISORY}\n{head}" if k == "assessment" else head

    if not heading and not query:                              # outline mode
        lines: list[str] = []
        for k in which:
            secs = docs.get(k) or {}
            if secs:
                tag = " (document-only, ADVISORY)" if k == "assessment" else ""
                lines.append(f"# {k.upper()}{tag} outline (call read_doc with a heading to fetch):")
                lines += [f"  - {h}" for h in secs]
        return "\n".join(lines) or "(no sections)"

    if heading:                                                # fetch by heading: exact → contains
        h = heading.strip().lower()
        for k in which:
            for hh, body in (docs.get(k) or {}).items():
                if hh.lower() == h:
                    return f"{_label(k, hh)}\n{body}"
        for k in which:
            for hh, body in (docs.get(k) or {}).items():
                if h in hh.lower():
                    return f"{_label(k, hh)}\n{body}"
        return f"(no section heading matches {heading!r}; call read_doc with no args for the outline)"

    terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 2]   # keyword search
    scored: list[tuple[int, str, str, str]] = []
    for k in which:
        for hh, body in (docs.get(k) or {}).items():
            hay = (hh + " " + body).lower()
            score = sum(hay.count(t) for t in terms)
            if score:
                scored.append((score, k, hh, body))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return f"(no sections match {query!r}; call read_doc with no args for the outline)"
    out = []
    for _, k, hh, body in scored[:3]:
        clip = body if len(body) <= 1500 else (
            body[:1500] + f"… [+{len(body) - 1500} chars — fetch the FULL section with "
                          f"read_doc(doc={k!r}, heading={hh!r})]")
        out.append(f"{_label(k, hh)}\n{clip}")
    if len(scored) > 3:
        out.append(f"({len(scored) - 3} more sections also matched: "
                   + ", ".join(hh for _, _, hh, _ in scored[3:8])
                   + ("…" if len(scored) > 8 else "") + " — fetch any by heading)")
    return "\n\n".join(out)


def flow_context(ctx: RunContext, repo_id: str | None = None, flow: str | None = None) -> str:
    """Index-time FLOW MAP (reuse-first §) with KEY-BASED retrieval: omit ``flow`` for
    the thin index (summary + which APIs carry the transaction leg + flow NAMES); pass a
    flow name to pull that flow's step sequence. ORIENTATION ONLY — never a source of
    truth: verify against the code, and check for flows the map doesn't list."""
    repo_id = _resolve_repo_id(ctx, repo_id)
    assert_repo_selected(repo_id, ctx.selected_repo_ids)
    if ctx.db is None:
        return "(flow map unavailable — trace the entry points with grep/read_file)"
    from app.models.flow_context import FlowContext
    from app.agents.context_assembler import _format_flow
    row = ctx.db.query(FlowContext).filter(FlowContext.repo_id == repo_id).first()
    if row is None:
        return ("(no flow map indexed for this repo — re-index to generate it; "
                "meanwhile trace flows from the API entry points with grep/read_file)")
    return _format_flow(row, flow)


def _disk_ops(ctx: RunContext) -> tuple[list[FileOp], list[str]]:
    """Changed files read from the WORKSPACE (disk = ground truth), mirroring the
    orchestrator's _disk_change_set: includes edits from earlier continuation rounds
    and a Phase-A parent's schema edits — not just this loop's in-memory ops. This is
    what makes verify_change rebuild a changed schema module (regenerating JAXB
    sources) even when this loop never touched it.

    Returns ``(ops, failed_repo_ids)`` — a repo whose enumeration raised is REPORTED,
    not silently skipped: a partial ops list is truthy, so without the second value a
    workspace/FS failure shrank the verified set and the agent read a green verdict
    over a change-set missing a whole repo."""
    ws = ctx.workspace_run_id or ctx.run_id
    ops: list[FileOp] = []
    failed: list[str] = []
    for rid in ctx.selected_repo_ids or []:
        try:
            rd = workspace_local.repo_dir(ws, rid)
            for op, path in workspace_local.changed_files(ws, rid):
                content = None
                if op != "delete":
                    try:
                        content = (rd / path).read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        content = None
                ops.append(FileOp(op=op, repo_id=rid, path=path, content=content,
                                  content_hash=_sha256(content) if content is not None else None))
        except Exception as e:  # noqa: BLE001 — disk read is best-effort; fall back to in-memory
            logger.warning("_disk_ops: repo %s enumeration failed: %s", rid, e)
            failed.append(rid)
            continue
    return ops, failed


def verify_change(ctx: RunContext, repo_id: str | None = None) -> str:
    """Self-verify (§9.3): build/compile the edits made SO FAR through the run's
    selected verification backend and report REAL errors, so the agent fixes them
    before finishing. DIAGNOSTIC ONLY — the runtime re-runs the authoritative gate
    after this phase regardless. Degrades gracefully when there is no local build
    toolchain (says CI will verify) so the agent never spins."""
    ops, bad_repos = _disk_ops(ctx)
    ops = ops or ctx.changeset()
    if not ops:
        # No disk ops AND no in-memory ops → the agent really hasn't edited yet; a failed
        # workspace read is only a blind spot to note, not a hard error (edits that DO
        # exist but can't be read are covered by the partial marker below).
        note = (f" ⚠ note: the workspace for repo(s) {', '.join(bad_repos)} could not be "
                "read — any edits already made there were NOT seen." if bad_repos else "")
        return f"(no changes yet to verify — make your edits first, then call verify_change){note}"
    # A repo that failed to enumerate is EXCLUDED from the build below — any verdict
    # (even green) covers a partial change-set and must say so.
    partial = (f"\n⚠ repo(s) {', '.join(bad_repos)} could not be read from the workspace — "
               "this verification EXCLUDES them; their edits are NOT covered by the verdict "
               "above. Retry verify_change before trusting a green result."
               if bad_repos else "")
    from types import SimpleNamespace
    from app.agents.verifier import select_verifier
    from app.agents import verification_plan

    verifier = select_verifier()
    outcome = verifier.verify(ctx.db, ctx.workspace_run_id or ctx.run_id, SimpleNamespace(operations=ops))
    if outcome.status == "unverified":
        # Two very different conditions share this status: a DELIBERATE no-toolchain
        # deferral (CI covers it) vs the verifier failing to run (infra). Telling the
        # agent "CI will verify; focus on correctness" for the latter switches OFF its
        # self-verification against an error that a retry might clear.
        reason = outcome.reason or "no local build toolchain"
        # Known deliberate deferrals: the CI/off backend, a missing toolchain, or a
        # sealed environment gap ("build environment: …" from run_plan). Anything else
        # reaching "unverified" is the verifier itself failing.
        deferred = (getattr(verifier, "name", "") == "deferred"
                    or "toolchain" in reason.lower()
                    or reason.lower().startswith("build environment"))
        if deferred:
            return (f"verification unavailable in this environment ({reason}) — your change "
                    f"will be verified by CI after approval; focus on correctness.{partial}")
        return (f"⚠ the verifier FAILED to run ({reason}) — this is an infra error, NOT a "
                f"pass and NOT deferred-to-CI. Retry verify_change; if it keeps failing, "
                f"verify with run_command (the build gate's own mvn command) instead.{partial}")
    if outcome.status == "verified":
        tail = " and tests pass" if (outcome.gates or {}).get("required_tests") else ""
        return f"✅ verified: the changed code compiles{tail}.{partial}"
    errors = verification_plan.format_errors(outcome)
    gates = ", ".join(f"{k}={'ok' if v else 'FAIL'}" for k, v in (outcome.gates or {}).items())
    reason = f" — verifier reason: {outcome.reason}" if getattr(outcome, "reason", None) else ""
    body = "\n".join(f"  - {e}" for e in errors) or \
        (f"  (non-zero build with no parsed diagnostics{reason} — re-run the FAILING gate's "
         "own command via run_command to inspect; a plain 'mvn compile' can pass while the "
         "scoped gate command fails)")
    return f"❌ needs_fix [{gates}]\n{body}\nFix these, then call verify_change again to confirm.{partial}"


def code_search_semantic(ctx: RunContext, query: str, top_k: int = 12) -> str:
    """ADVISORY semantic (RAG) code search over the indexed code — surfaces code
    related to a concept by MEANING. Use your judgement: it's an embedding match, so
    it can be noisy or miss things. Reach for it to DISCOVER where a concept lives
    when you don't know the keywords; prefer grep for exact strings and ast_query for
    a file's precise structure. Always confirm a hit with read_file."""
    if not ctx.selected_repo_ids:
        raise ToolError("no repos selected — select at least one repo before searching")
    if ctx.db is None:
        return "(code index unavailable — use grep/glob on the clone)"
    from app.models.document_chunk import CODE_SOURCE_CATEGORIES
    k = max(1, min(int(top_k or 12), 20))
    try:
        from app.rag.retrieval import retrieve
        # Force the cross-encoder reranker ON for code search (without flipping the global
        # default): the agent's accuracy hinges on the TOP few hits being the right ones, and
        # reranking sharply improves ordering. Fail-open if the model can't load.
        chunks = retrieve(query, ctx.db, top_k=k,
                          categories=list(CODE_SOURCE_CATEGORIES), use_reranker=True)
    except Exception as e:  # noqa: BLE001
        return f"(code search unavailable: {e} — use grep/glob)"
    if not chunks:
        return "(no semantic matches — try grep, or rephrase the query)"
    lines = ["[code_search — from the index, may lag the clone; confirm with read_file]"]
    for c in chunks[:k]:
        path = c.get("source_file") or "?"
        sym = (c.get("symbol_name") or "").strip()
        snippet = (c.get("content") or "").strip().replace("\n", " ")[:240]
        lines.append(f"  {path}{(' :: ' + sym) if sym else ''}\n    {snippet}…")
    return "\n".join(lines)


# Document-KB categories for domain_docs_search — everything that is an OFFICIAL
# document, deliberately excluding code-source categories (code_search covers
# those) so a doc citation can never silently be a code snippet or vice versa.
# Pack-driven vocabulary for tool docs/schemas (genericisation sweep): the
# tool names stay stable (they are wired into prefaces and dispatch tables);
# the domain words inside their descriptions come from the active pack.
from app.core.domain.registry import prompt_block as _PB

_DOMAIN_NAME = _PB("domain_name", "platform")
_AUTHORITY_NAME = _PB("authority", "the platform operator")
_DOCS_LABEL = f"{_DOMAIN_NAME}/{_AUTHORITY_NAME}"
_ELEMENT_EXAMPLES = _PB("example_element_names", "e.g. a message root element name")
_MODULE_PATH_EXAMPLE = _PB("example_module_path", "'<repo>/<module>'")

_UPI_DOC_CATEGORIES = (
    "rbi_guideline", "upi_product_doc", "past_brd", "api_spec", "xsd",
    "npci_circular", "npci_error_code", "npci_tsd", "npci_product_note",
    "npci_faq", "npci_product_deck", "npci_xml_spec", "api_design_knowledge",
)


def domain_docs_search(ctx: RunContext, query: str, top_k: int = 8) -> str:
    """Semantic search over the official domain documents in the knowledge base
    (circulars, product docs, API specs, error-code tables, wire-format samples).
    SUPPORTING evidence only — a doc may CONFIRM a party/flow/field claim the code
    shows, it must never ORIGINATE a hop or party the code does not show. No repo
    scoping: the KB is platform-wide."""
    if ctx.db is None:
        return "(document KB unavailable — ground the flow in code evidence only)"
    k = max(1, min(int(top_k or 8), 20))
    try:
        from app.rag.retrieval import retrieve
        chunks = retrieve(query, ctx.db, top_k=k,
                          categories=list(_UPI_DOC_CATEGORIES), use_reranker=True)
    except Exception as e:  # noqa: BLE001
        return f"(doc search unavailable: {e} — ground the flow in code evidence only)"
    if not chunks:
        return ("(no document matches — treat the topic as NOT covered by the docs: "
                "say so plainly rather than filling the gap from memory)")
    lines = ["[upi_docs — official documents, SUPPORTING evidence only: a doc may CONFIRM "
             "a flow/party/field claim, never ORIGINATE one the code does not show. Cite "
             "the source name next to every claim you take from here.]"]
    for c in chunks[:k]:
        src = c.get("source_file") or "?"
        cat = c.get("doc_category") or ""
        snippet = (c.get("content") or "").strip().replace("\n", " ")[:300]
        lines.append(f"  {src}{(' [' + cat + ']') if cat else ''}\n    {snippet}…")
    return "\n".join(lines)


def lsp_diagnostics(ctx: RunContext, repo_id: str, path: str) -> str:
    """Type-aware INLINE diagnostics for a .java file via the Java LSP (jdtls) — a
    faster spot-check than a full build while iterating. ADVISORY: the authoritative
    gate is verify_change (real mvn). Available only when the LSP is enabled on a
    high-RAM host; otherwise it degrades and you should use verify_change."""
    assert_repo_selected(repo_id, ctx.selected_repo_ids)
    if not settings.agentic_lsp_enabled:
        return ("(LSP disabled on this host — enable AGENTIC_LSP_ENABLED on a high-RAM server; "
                "meanwhile use verify_change / run_command 'mvn compile' for diagnostics)")
    if not path.lower().endswith(".java"):
        return "(lsp_diagnostics supports .java files only — use ast_query/read_file otherwise)"
    ctx.intel_queried.add(f"path:{repo_id}:{path}")
    from app.agents import lsp_client
    res = lsp_client.diagnostics(str(workspace_local.repo_dir(ctx.workspace_run_id or ctx.run_id, repo_id)), path)
    if isinstance(res, str):
        return res                                     # degraded / error message
    if not res:
        return f"✅ no LSP diagnostics for {path}"
    errs = [d for d in res if d["severity"] == "error"]
    lines = [f"[lsp_diagnostics — {path}: {len(errs)} error(s), {len(res) - len(errs)} other]"]
    for d in res[:40]:
        lines.append(f"  {path}:{d['line']}:{d['col']} {d['severity']}: {d['message']}")
    return "\n".join(lines)


# ── Registry + Anthropic schemas ──────────────────────────────────────────────


_OCCUPANCY_SAMPLE = 8

# ── Grounding: fact sheet + value-occupancy gate (retrofit PR-2, by feature) ──
# Upstream introduced these across 3d97e32a / 4db0dea1 / f49bbcf0, one of which is a
# governance commit. Taken here as the FINAL upstream state of the feature rather
# than replayed commit-by-commit: the first attempt did the latter and shipped a
# prompt promising an occupancy check whose gate had been left behind.
_FACTS_MAX = 30
_FACT_CHARS = 220
_FACT_GLYPH = {"verified": "✓", "human_decision": "⚖", "assumption": "≈"}


def format_facts(facts: list[dict]) -> str:
    """Render a fact sheet for a prompt block. Plain data in (works for ctx.facts and for
    the handoff-persisted copy a later phase renders). '' when empty."""
    rows = [f for f in (facts or []) if isinstance(f, dict) and f.get("fact")]
    if not rows:
        return ""
    lines = ["FACT SHEET (recorded this change at the moment of discovery — "
             "✓ verified in code · ⚖ human decision · ≈ assumption):"]
    for f in rows:
        glyph = _FACT_GLYPH.get(f.get("kind"), "≈")
        src = f" [{f['source']}]" if f.get("source") else ""
        lines.append(f"  {f.get('id', '?')} {glyph} {f['fact']}{src}")
    return "\n".join(lines)


_FACTS_SATURATED = ("fact sheet FULL — further auto-facts are NOT being recorded; free a slot "
                    "by superseding a stale entry (record_fact supersedes='F<n>')")


def _add_fact(ctx: RunContext, fact: str, source: str, kind: str) -> None:
    """Server-side fact writer (auto-facts): dedupe by text, respect the cap —
    an auto-fact must never error a tool call it piggybacks on. Saturation is NOT
    silent: the first dropped auto-fact pins a sentinel entry onto the sheet (the one
    place the model reliably re-reads) instead of facts quietly vanishing."""
    f = (fact or "").strip()[:_FACT_CHARS]
    if not f or any(x.get("fact") == f for x in ctx.facts):
        return
    if len(ctx.facts) >= _FACTS_MAX:
        if not any(x.get("fact") == _FACTS_SATURATED for x in ctx.facts):
            ctx.facts.append({"id": "F!", "fact": _FACTS_SATURATED, "source": "", "kind": "assumption"})
            ctx.facts_rev += 1
        return
    ctx.facts.append({"id": f"F{len(ctx.facts) + 1}", "fact": f,
                      "source": (source or "")[:120], "kind": kind})
    ctx.facts_rev += 1


def record_fact(ctx: RunContext, fact: str = "", source: str = "",
                supersedes: str | None = None) -> str:
    """Working-memory fact sheet: record ONE load-bearing fact at the moment you learn it —
    an existing value/constant binding, a human decision, a verified constraint. The sheet
    stays pinned in your context for the whole run and travels to later phases, so a fact
    recorded here cannot fade out of attention 40 iterations later. Provenance is enforced:
    a file source must be one you actually read this run; otherwise declare the fact as
    'human_decision' or 'assumption' — never dress a guess as a finding."""
    f = (fact or "").strip()
    src = (source or "").strip()
    if not f or not src:
        return ("record_fact NOT recorded — both `fact` (one sentence) and `source` (the "
                "file you read, or 'human_decision' / 'assumption') are required.")
    if src in ("human_decision", "assumption"):
        kind = src
    else:
        verified, _ = _verify_evidence(ctx, [{"claim": f, "file": src}])
        if not verified:
            return (f"record_fact NOT recorded — source '{src}' is not a file you read this "
                    "run. read_file it first and cite it exactly, or use "
                    "source='human_decision' / 'assumption'.")
        kind = "verified"
    entry = {"fact": f[:_FACT_CHARS], "source": src[:120], "kind": kind}
    if supersedes:
        for x in ctx.facts:
            if x.get("id") == supersedes:
                x.update(entry)
                ctx.facts_rev += 1
                return f"fact {supersedes} superseded.\n{format_facts(ctx.facts)}"
        return f"record_fact NOT recorded — no fact with id '{supersedes}' to supersede."
    if any(x.get("fact") == entry["fact"] for x in ctx.facts):
        return "already on the sheet — not duplicated.\n" + format_facts(ctx.facts)
    if len(ctx.facts) >= _FACTS_MAX:
        return (f"record_fact NOT recorded — the sheet is full ({_FACTS_MAX}). Pass "
                "supersedes='F<n>' to replace an entry this fact outranks, or skip it: "
                "the sheet is for LOAD-BEARING facts only.")
    entry["id"] = f"F{len(ctx.facts) + 1}"
    ctx.facts.append(entry)
    ctx.facts_rev += 1
    return f"fact {entry['id']} recorded.\n{format_facts(ctx.facts)}"


def occupancy_in_roots(roots: dict[str, "Path"], literal: str) -> dict:
    """Occupancy sweep core: where does ``literal`` already appear as a quoted string
    ("X" / 'X') under each of ``roots`` (``{repo_id: checkout_path}``)?

    Split out of :func:`_value_occupancy` so callers that have repo paths but no
    ``RunContext`` — notably the Phase-A schema freeze, which occupancy-checks every enum
    literal a schema edit ADDS before a human approves it — reuse the exact same
    server-executed evidence path rather than reimplementing (and diverging from) it.
    """
    lit = (literal or "").strip().strip("\"'")
    if not lit:
        return {"value": lit, "hits": 0, "sample": [], "repos_scanned": [], "complete": False}
    hits: list[str] = []
    scanned: list[str] = []
    complete = True
    for rid, root in (roots or {}).items():
        try:
            # -F (fixed strings): the literal is data, not a regex. --untracked mirrors
            # grep(): the agent's own new files count as occupancy too.
            res = adapter.run_command(root, ["git", "grep", "-n", "-F", "--no-color",
                                             "--untracked", "-e", f'"{lit}"', "-e", f"'{lit}'"])
            if res.timed_out or res.exit_code not in (0, 1):     # 1 == no matches
                complete = False
                continue
            scanned.append(rid)
            hits.extend(f"[{rid}] {line}" for line in res.stdout.splitlines())
        except Exception:  # noqa: BLE001 — a repo that can't be scanned must read as
            # UNCHECKED, never as "no occurrences" (the schema_guardian rule).
            complete = False
    return {"value": lit, "hits": len(hits), "sample": hits[:_OCCUPANCY_SAMPLE],
            "repos_scanned": scanned, "complete": complete}


def _value_occupancy(ctx: RunContext, literal: str) -> dict:
    """Deterministic occupancy sweep for a proposed literal VALUE: where does it already
    appear as a quoted string ("X" / 'X') across ALL selected repos? Server-executed, so
    the result is evidence the model cannot fake or misremember — the incident this guards
    against was an agent proposing a code value as free while the value sat bound in a
    constants file it had read 38 iterations earlier. Advisory by design: a hit is
    evidence AGAINST availability (the human sees where), not proof of collision — the
    same token can be a version, a port, an unrelated constant."""
    roots: dict[str, Path] = {}
    unresolved = False
    for rid in ctx.selected_repo_ids or []:
        try:
            roots[rid] = _repo_root(ctx, rid)
        except Exception:  # noqa: BLE001 — an unresolvable repo is UNCHECKED, not empty
            unresolved = True
    occ = occupancy_in_roots(roots, literal)
    if unresolved:
        occ["complete"] = False
    return occ



# ── Governance skill execution (retrofit PR-5) ───────────────────────────────
# run_skill_script executes DECLARED bundle scripts; gov_bash is the Claude-Code
# parity shell for model-authored commands. Both refuse unless this run's review
# phase materialized a bundle, so no other agent kind can reach them. gov_bash
# additionally requires the docker backend — see governance_sandbox.run_shell.
_SCRIPT_RUNNERS = {"python", "python3", "node", "bash", "sh", "ruby", "perl", "java", "npx"}


_SCRIPT_SUFFIXES = (".py", ".js", ".sh", ".rb", ".pl", ".mjs", ".ts")


def _invokes_a_script(command: str) -> bool:
    """Does this command actually RUN a script, as opposed to merely mentioning one?

    Gates the user-facing "skill-script execution FAILED" banner. A substring test
    (``".py" in command``) fired on ordinary probing — `grep -rn "foo.py" .` exits 1 when
    it finds nothing, which is a normal negative result, not a crashed tool. Banners that
    cry wolf stop being read, and this one's whole job is to be believed.

    So: split on shell separators and inspect each segment's FIRST token — a script runs
    only when that token is an interpreter (with a script-ish argument) or is itself a
    script path. `grep`/`ls`/`cat` in leading position never match, whatever they mention.
    """
    import re as _re
    for seg in _re.split(r"&&|\|\||[;|\n]", command or ""):
        toks = seg.strip().split()
        if not toks:
            continue
        while toks and ("=" in toks[0] and not toks[0].startswith("-")):
            toks = toks[1:]          # strip leading VAR=value assignments
        if not toks:
            continue
        head = toks[0].rsplit("/", 1)[-1]
        if head in _SCRIPT_RUNNERS:
            if any(t.endswith(_SCRIPT_SUFFIXES) for t in toks[1:]):
                return True
            continue
        if head.endswith(_SCRIPT_SUFFIXES) or toks[0].startswith(("scripts/", "./scripts/")):
            return True
    return False


def _note_script_failure(run_id: str, entry: dict) -> None:
    """Append a skill-script execution failure to the stage's sidecar file so
    the orchestrator surfaces it to the USER (feed event + stage-card banner).
    A script the skill needed that did not run must never stay buried in the
    transcript — the reviewer's prose may or may not mention it."""
    import json as _json
    from pathlib import Path

    from app.agents import workspace_local as _wl
    try:
        p = Path(_wl.run_dir(run_id)) / "_skill_bundle" / "_script_failures.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(entry) + "\n")
    except Exception:  # noqa: BLE001 — recording must never break the tool itself
        pass


def run_skill_script(ctx: RunContext, script: str, repo_id: str | None = None) -> str:
    """Governance stages only: execute a script from THIS run's materialized skill
    bundle in the sandbox and return its contract-parsed result. Refuses unless the
    governance review phase materialized a bundle for this run (so no other agent
    kind can ever reach it), and only scripts DECLARED in the bundle's exec manifest
    are runnable — an agent cannot invent an invocation."""
    import json as _json
    from pathlib import Path

    from app.agents import workspace_local as _wl

    bundle_dir = Path(_wl.run_dir(ctx.run_id)) / "_skill_bundle"
    manifest_path = bundle_dir / "_exec_manifest.json"
    if not manifest_path.is_file():
        raise ToolError("no skill bundle is materialized for this run — run_skill_script "
                        "is only available inside a governance review stage")
    contracts = ((_json.loads(manifest_path.read_text(encoding="utf-8")) or {}).get("scripts") or [])
    contract = next((c for c in contracts if c.get("path") == script), None)
    if contract is None:
        multi_slot = any(c.get("_subdir") for c in contracts)
        if multi_slot:
            # Multi-slot stages: require the QUALIFIED <slot>/scripts/… path. A bare
            # suffix match could run a DIFFERENT slot's script than the SKILL.md the
            # agent is following (cross-slot execution attributed to the wrong slot);
            # the exact-match above already accepts the qualified form.
            contract = None
        else:
            # Single-slot: accept the bare form the tool schema documents (there is
            # no slot ambiguity, and the contract path is unqualified anyway).
            matches = [c for c in contracts
                       if c.get("_orig_path") == script or c.get("path", "").endswith("/" + script)]
            contract = matches[0] if len(matches) == 1 else None
    if contract is None:
        names = ", ".join(c.get("path", "?") for c in contracts) or "(none)"
        raise ToolError(f"script {script!r} is not declared in the skill bundle's exec "
                        f"manifest. Declared scripts (use the exact qualified path): {names}")
    rid = repo_id or (ctx.selected_repo_ids[0] if ctx.selected_repo_ids else None)
    if rid is None or (ctx.selected_repo_ids and rid not in ctx.selected_repo_ids):
        raise ToolError(f"repo_id must be one of: {', '.join(ctx.selected_repo_ids or [])}")
    from app.agents.governance_sandbox import run_script as _run
    ws = getattr(ctx, "workspace_run_id", None) or ctx.run_id
    # VALIDATORS scan the same change-scoped sparse copy the deterministic floor
    # uses (built per review round under _floor_target/<rid>) — an agent-run
    # validator must agree with the floor, not re-block the change on pre-existing
    # repo debt in files the change never touched (a live stage did exactly that,
    # and its fixer then edited out-of-change files). Generators (SBOM et al.)
    # legitimately want the WHOLE repo. Sparse copy missing → full repo, same as
    # the floor's own fail-closed fallback.
    target = Path(_wl.repo_dir(ws, rid))
    scope = "full-repo"
    if (contract.get("role") == "validator"):
        # scope=change validators (change-level report-graders) read the merged
        # all-repos changed-files copy the floor built; repo-scoped ones read
        # their repo's sparse copy — both mirror the floor's own targets.
        sub_t = ("_change" if (contract.get("scope") or "repo") == "change" else rid)
        sparse = bundle_dir.parent / "_floor_target" / sub_t
        if sparse.is_dir():
            target, scope = sparse, ("change-files" if sub_t == "_change" else "changed-files")
    # Multi-slot bundles materialize under _skill_bundle/<slot>/ — the ORIGINAL
    # contract executes unchanged inside its own bundle root, so invocations and
    # bundle-relative data paths need no rewriting.
    exec_dir = (bundle_dir / contract["_subdir"]) if contract.get("_subdir") else bundle_dir
    run_contract = ({**contract, "path": contract["_orig_path"]}
                    if contract.get("_orig_path") else contract)
    r = _run(run_contract, bundle_dir=exec_dir, target_dir=target,
             scratch_dir=bundle_dir / "_scratch" / f"tool_{rid}")
    # Crash-shaped outcomes surface to the USER (banner + event), not just the
    # agent: harness error, or a nonzero exit that produced nothing parseable.
    # (A scanner whose contract parsed findings despite a nonzero exit is NOT a
    # failure — some tools exit 1 on findings by design.)
    if r.error or (not r.ran) or ((r.exit_code or 0) != 0 and r.findings_count is None):
        _note_script_failure(ctx.run_id, {
            "tool": "run_skill_script", "script": contract.get("path") or script,
            "exit_code": r.exit_code, "error": r.error,
            "stderr": (r.stderr or "")[:400]})
    out = {"script": contract.get("path") or script, "repo_id": rid, "ran": r.ran, "exit_code": r.exit_code,
           "scope": scope,
           "role": r.role, "findings_count": r.findings_count,
           "findings": (r.findings or [])[:50], "error": r.error,
           "duration_s": r.duration_s}
    if not r.findings and r.stdout:
        out["stdout_head"] = r.stdout[:4000]
    if r.error and r.stderr:
        out["stderr_head"] = r.stderr[:2000]
    return _json.dumps(out, ensure_ascii=False)[:60_000]


def gov_bash(ctx: RunContext, command: str, timeout_s: int | None = None) -> str:
    """Governance stages only: a Claude-Code-style shell inside the governance
    sandbox. Gated the same way as run_skill_script — refuses unless this run's
    review phase materialized a skill bundle, so no other agent kind can reach
    it. Unlike run_skill_script, the COMMAND is the agent's own (following
    SKILL.md's documented invocations verbatim) — the audit record is the
    transcript, exactly the Claude Code model. Network stays off (sandbox
    policy, design §4); repos are exposed for reading, outputs belong in the
    stage's output dir."""
    import json as _json
    from pathlib import Path

    from app.agents import workspace_local as _wl

    bundle_dir = Path(_wl.run_dir(ctx.run_id)) / "_skill_bundle"
    if not bundle_dir.is_dir():
        raise ToolError("no skill bundle is materialized for this run — bash is only "
                        "available inside a governance review stage")
    out_dir = bundle_dir.parent / "_skill_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    ws = getattr(ctx, "workspace_run_id", None) or ctx.run_id
    repo_dirs = [Path(_wl.repo_dir(ws, rid)) for rid in (ctx.selected_repo_ids or [])]
    from app.agents.governance_sandbox import run_shell as _sh
    r = _sh(command, cwd=bundle_dir, ro_dirs=repo_dirs,
            rw_dirs=[bundle_dir, out_dir], timeout_s=timeout_s or 300)
    if r.get("error"):
        _note_script_failure(ctx.run_id, {"tool": "bash", "command": command[:300],
                                          "exit_code": None, "error": r["error"],
                                          "stderr": ""})
        raise ToolError(f"bash: {r['error']}")
    # A failed SCRIPT invocation (not generic shell probing like grep-no-match)
    # is a user-visible signal: the skill's own tooling did not run.
    if r["exit_code"] != 0 and _invokes_a_script(command):
        _note_script_failure(ctx.run_id, {"tool": "bash", "command": command[:300],
                                          "exit_code": r["exit_code"], "error": None,
                                          "stderr": (r.get("stderr") or "")[:400]})
    payload = {"exit_code": r["exit_code"],
               "stdout": (r["stdout"] or "")[:30_000],
               "stderr": (r["stderr"] or "")[:10_000],
               "duration_s": r["duration_s"]}
    if len(r.get("stdout") or "") > 30_000:
        payload["stdout_truncated"] = True
    return _json.dumps(payload, ensure_ascii=False)[:60_000]


RUN_SKILL_SCRIPT_SCHEMA = {
    "name": "run_skill_script",
    "description": "Execute one of the governance skill bundle's declared scripts against a "
                   "repo in the sandbox and get its parsed result (findings for validators, "
                   "output for generators). Only scripts listed in the bundle's exec manifest "
                   "can run. Use when SKILL.md's procedure tells you to run a check. "
                   "Validators run against the CHANGE's files only (scope=changed-files in "
                   "the result) — the review gates the change, not pre-existing repo debt; "
                   "generators see the whole repo.",
    "input_schema": {"type": "object", "required": ["script"], "properties": {
        "script": {"type": "string", "description": "bundle-relative script path exactly as "
                                                    "declared, e.g. scripts/scan_secrets.py"},
        "repo_id": {"type": "string", "description": "which selected repo to run against "
                                                     "(defaults to the first)"},
    }},
}


GOV_BASH_SCHEMA = {
    "name": "bash",
    "description": "Run a shell command in the governance sandbox (bash -lc; network "
                   "DISABLED — no pip install / curl / git clone from remotes). Use it to "
                   "execute the skill's procedure exactly as SKILL.md documents it, e.g. "
                   "`python scripts/generate_sca.py <repo path> --out <output dir>/report.json`. "
                   "The working directory is the skill bundle root. Treat the change's repos "
                   "as READ-ONLY — write every artifact to the stage output directory the "
                   "preface names. Each call is an independent shell (use && to chain).",
    "input_schema": {"type": "object", "required": ["command"], "properties": {
        "command": {"type": "string", "description": "the bash command line to run"},
        "timeout_s": {"type": "integer", "description": "seconds before the command is "
                                                        "killed (default 300, max 1800)"},
    }},
}


_DISPATCH = {
    "run_skill_script": run_skill_script,
    "bash": gov_bash,
    "record_fact": record_fact,
    "read_file": read_file,
    "grep": grep,
    "glob": glob,
    "edit_file": edit_file,
    "create_file": create_file,
    "delete_file": delete_file,
    "submit_plan": submit_plan,
    "ask_decision": ask_decision,
    "propose_approach": propose_approach,
    "propose_revision": propose_revision,
    "flag_concern": flag_concern,
    "ask_clarifications": ask_clarifications,
    "propose_plan": propose_plan,
    "find_existing_xsd": find_existing_xsd,
    "schema_guardian": schema_guardian,
    "symbol_graph": symbol_graph,
    "callers": callers,
    "impact_analysis": impact_analysis,
    "jaxb_accessors": jaxb_accessors,
    "show_diff": show_diff,
    "git_history": git_history,
    "ast_query": ast_query,
    "run_command": run_command,
    "read_output": read_output,
    "module_context": module_context,
    "flow_context": flow_context,
    "read_doc": read_doc,
    "verify_change": verify_change,
    "code_search_semantic": code_search_semantic,
    "domain_docs_search": domain_docs_search,
    "lsp_diagnostics": lsp_diagnostics,
}


# Retry hints for tools whose REQUIRED arg the AiNxt OpenAI→Anthropic shim sometimes drops
# (§3.6). A dropped arg arrives as a TypeError; instead of a bare "bad arguments" we hand the
# model an actionable recovery line naming the arg and the correct call shape. Pure messaging
# — the model still re-issues the call (we never GUESS a path/pattern, which would feed it
# false evidence).
_DROPPED_ARG_HINTS = {
    "grep":            'grep(repo_id="<id>", pattern="<regex>") — `pattern` is required',
    "glob":            'glob(repo_id="<id>", pattern="<glob, e.g. **/*.java>") — `pattern` is required',
    "read_file":       'read_file(repo_id="<id>", path="<repo-relative path>") — `path` is required',
    "ast_query":       'ast_query(repo_id="<id>", path="<.java/.xsd path>") — `path` is required',
    "git_history":     'git_history(repo_id="<id>", path="<file path>") — `path` is required',
    "symbol_graph":    'symbol_graph(repo_id="<id>", symbol="<ClassOrMethod>") — `symbol` is required',
    "callers":         'callers(symbol="<ClassOrMethod>") — `symbol` is required',
    "impact_analysis": 'impact_analysis(symbol="<ClassOrMethod>") — `symbol` is required',
    "jaxb_accessors":  'jaxb_accessors(element="<XsdElementName>") — `element` is required',
    "code_search_semantic": 'code_search_semantic(query="<concept>") — `query` is required',
    "domain_docs_search":  'domain_docs_search(query="<topic>") — `query` is required',
}


def _args_echo(tool_input: dict | None, cap: int = 500) -> str:
    """Compact JSON echo of a tool call's received arguments for corrective error feedback.
    Bounded so a huge new_string can't bloat the error result."""
    try:
        s = json.dumps(tool_input or {}, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001 — echo is best-effort
        s = str(tool_input)
    return s[:cap] + ("… (truncated)" if len(s) > cap else "")


def execute_tool(ctx: RunContext, name: str, tool_input: dict) -> tuple[str, bool]:
    """Run a tool by name. Returns (result_text, is_error). All recoverable
    failures (ToolError, scoping, bad path) come back as is_error so the model
    can adjust — only a truly unexpected exception propagates.

    Non-error POLICY outcomes are machine-readable by STABLE leading token: a write a
    phase guard declined starts with "[SKIPPED]" or "[REFUSED]" — the write did NOT
    happen. is_error stays False for these because retrying the identical call cannot
    succeed (the guard is policy, not a transient fault)."""
    fn = _DISPATCH.get(name)
    if fn is None:
        return (f"unknown tool: {name}. Available tools: {', '.join(sorted(_DISPATCH))}", True)
    try:
        return fn(ctx, **(tool_input or {})), False
    except (ToolError, RepoSelectionError) as e:
        return str(e), True
    except TypeError as e:                    # missing/dropped arguments (often the AiNxt shim)
        # Echo back what actually ARRIVED (grok-build-style corrective feedback): the model
        # can only fix a malformed call it can see — "bad arguments" alone leaves it guessing
        # whether an argument was dropped, misnamed, or truncated.
        received = _args_echo(tool_input)
        hint = _DROPPED_ARG_HINTS.get(name)
        if hint:
            return (f"{name}: a required argument is missing — {e}. Arguments received: "
                    f"{received}. This is usually the gateway dropping an argument; re-issue "
                    f"the call with ALL arguments: {hint}.", True)
        return f"bad arguments for {name}: {e}. Arguments received: {received}", True
    except Exception as e:                    # noqa: BLE001 — ANY tool failure becomes a
        # self-healable result, never a run-killer. A bad byte in git/mvn output, a
        # transient index miss, etc. is handed back to the agent (is_error=True) so it
        # can diagnose and route around it — instead of escaping the loop and FAILing the run.
        # Addressee matters: this branch is the HARNESS/backend failing, not the agent's
        # call or the code under change — say so and echo the received args (the TypeError
        # branch's lesson), else the agent burns rounds "fixing" a call that was fine.
        logger.exception("tool %s internal error", name)
        return (f"tool {name} hit an INTERNAL error (a platform/backend failure — NOT a "
                f"problem with your call or with the code under change): "
                f"{type(e).__name__}: {e}. Arguments received: {_args_echo(tool_input)}. "
                f"Retry once; if it persists, get this information another way and note "
                f"the gap in your final summary.", True)


def _str(desc: str) -> dict:
    return {"type": "string", "description": desc}


TOOL_SCHEMAS: list[dict] = [
    {
        "name": "submit_plan",
        "description": "Turn 1: submit your plan before any edit, and RE-SUBMIT it whenever your "
                       "understanding changes so it stays accurate. List EVERY file the change needs — "
                       "you are held to this list: a planned file you don't touch is flagged as "
                       "incomplete. Include reuse_decisions (prefer extending existing code/XSDs over new).",
        "input_schema": {"type": "object", "required": ["summary"], "properties": {
            "summary": _str("what you will change and why"),
            "files": {"type": "array", "items": {"type": "object"},
                      "description": "ONE object per file as {path, action: create|modify|delete, "
                                     "intent: what changes in this file and why}. Cover every file the "
                                     "change touches end-to-end (parse → map → act → persist), not just one."},
            "reuse_decisions": {"type": "array", "items": {"type": "object"},
                                "description": "for each new thing: reuse|extend|new + why"},
            "reconciliation": {"type": "array", "items": {"type": "object"},
                               "description": "when you implement a RATIFIED-PLAN file's logic under a "
                                              "different file/architecture, declare it here as "
                                              "{planned_path, actual_path, why} — the fidelity gate then "
                                              "verifies the behaviour at actual_path instead of demanding "
                                              "an edit to planned_path. Integration points in EXISTING "
                                              "files are binding and may NOT be reconciled away."},
        }},
    },
    {
        "name": "ask_decision",
        "description": "Surface a decision a human must make: a BINDING DIRECTIVE conflicts with "
                       "what the code actually does, or a decision your work needs is missing from "
                       "the plan/directives and cannot be derived from the requirement or the code. "
                       "NEVER silently choose an interpretation for money movement, atomicity, "
                       "ordering, or settlement; NEVER use this to reopen or re-litigate a ratified "
                       "directive, and never ask about pure code mechanics you can decide yourself. "
                       "The run pauses; the human's answer comes back as a binding decision.",
        "input_schema": {"type": "object", "required": ["question", "blocked_item"], "properties": {
            "question": _str("the decision needed, with the code evidence that raised it"),
            "blocked_item": _str("the directive / plan item this blocks (quote it)"),
            "options": {"type": "array", "items": {"type": "object"},
                        "description": "2-4 options as {id, label, consequence}, one recommended"},
        }},
    },
    {
        "name": "propose_approach",
        "description": "Reuse-first decision gate. AFTER you've READ the existing flow in the code, "
                       "call this to present the human 2-3 concrete ways to accommodate the "
                       "requirement — reuse/extend an existing API vs a new one — and mark one "
                       "recommended. You MUST pass `evidence`: ≥2 citations to files you actually "
                       "read (the transaction-carrying flow + a consumer). Citations to files you "
                       f"didn't open are rejected and the proposal is NOT recorded — {_DOMAIN_NAME}'s flows are "
                       "not guessable, so the decision must be grounded in real code. ENDS your pass.",
        "input_schema": {"type": "object", "required": ["summary", "options", "evidence"], "properties": {
            "evidence": {"type": "array",
                "description": "≥2 items grounding the reuse-vs-new call in code you READ. Each: "
                               "{claim, file, line?}. `file` MUST be a path you opened with read_file this run "
                               "(e.g. the handler carrying the debit/credit leg, a message/XSD, a consumer). "
                               "For a file you read only in RANGES, `line` (the line the claim is about) is "
                               "required and must fall inside a range you read.",
                "items": {"type": "object", "properties": {
                    "claim": _str(f"what this file proves, e.g. '{_PB('primary_txn_message', 'the transaction message')} carries the transaction leg via X'"),
                    "file": _str("repo-relative path you read_file'd this run"),
                    "line": {"type": "integer", "description": "line number the claim is about — "
                             "required when you read the file only in ranges"},
                }}},
            "summary": _str("1-3 SHORT plain-language sentences a business user understands — what "
                            f"the system already supports and what that means here. {_DOMAIN_NAME} message names "
                            "are fine; NO class/file/infra names (put technical evidence in how_it_fits)."),
            "options": {"type": "array", "description": "2-3 options", "items": {"type": "object",
                "properties": {
                    "id": _str("short id, e.g. 'reuse-txn'"),
                    "title": _str("short PLAIN-LANGUAGE title a non-developer understands — no "
                                  "class/file/infra names; never propose a new service"),
                    "approach": _str("one of: reuse | extend | new"),
                    "target_api": _str("the specific existing API/message/XSD it fits into (or 'new')"),
                    "how_it_fits": _str("how this requirement maps onto that API/flow — technical "
                                        "detail belongs HERE (shown only when the user expands Details)"),
                    "tradeoffs": _str("risks/cost of this option"),
                    "diverges_from_plan": _str("'yes' if this option CONTRADICTS the ratified plan's "
                                               "recommended direction (e.g. plan recommended a NEW schema but "
                                               "this reuses/extends, or vice-versa); else 'no'. 'no' if no "
                                               "plan was provided."),
                    "divergence_note": _str("when diverges_from_plan='yes': ONE plain sentence — what the "
                                            "plan recommended vs what this option does, and why this option "
                                            "is still worth choosing. Empty when it does not diverge."),
                }}},
            "recommended": _str("the id of the option you recommend"),
        }},
    },
    {
        "name": "propose_revision",
        "description": "Refine-loop conversation gate — LAST RESORT, one round per request. Use ONLY "
                       "when the human's requested XSD change would genuinely BREAK something that "
                       "exists (removes/renames an in-use element, changes a JAXB-bound type, "
                       "violates the transaction-flow contract). NEVER for ADDITIVE requests (a new "
                       "optional element/attribute, a new enum value, a NEW message schema/file — "
                       "those break nothing: apply them), and NEVER because the request diverges "
                       "from the plan or an earlier approach decision — an explicit human request "
                       "supersedes those. Explains what breaks and offers 2-3 SAFER alternatives. "
                       "Each alternative MUST be implementable immediately with what you already "
                       "know — no 'first supply names/field lists/confirmation' preconditions; pick "
                       "sensible defaults and state them. ENDS your pass; whatever the human picks "
                       "is final.",
        "input_schema": {"type": "object", "required": ["summary", "options"], "properties": {
            "summary": _str("what the human asked for + exactly why it is disruptive (what breaks)"),
            "options": {"type": "array", "description": "2-3 safer alternatives, each self-contained "
                                                        "and immediately implementable", "items": {"type": "object",
                "properties": {
                    "id": _str("short id, e.g. 'deprecate-not-delete'"),
                    "title": _str("short title shown to the human"),
                    "how_it_fits": _str("how this still achieves what the human wants, safely — "
                                        "including the concrete defaults you chose for any detail "
                                        "the human did not specify"),
                    "tradeoffs": _str("what they give up vs their original ask"),
                }}},
            "recommended": _str("the id of the option you recommend"),
        }},
    },
    {
        "name": "flag_concern",
        "description": "Record a concern about a requested change. Two uses: (1) record-and-APPLY — "
                       "you disagree with a NON-breaking request but the human's ask wins: omit "
                       "declined_change, state your objection, and STILL apply the request; "
                       "(2) record-and-DECLINE — the change would genuinely break existing "
                       "consumers (drops/renames a required in-use element, changes a JAXB-bound "
                       "type, violates the transaction-flow contract): set declined_change and do "
                       "not apply that part, applying only the safe parts.",
        "input_schema": {"type": "object", "required": ["message"], "properties": {
            "message": _str("your concern; for a decline, why it breaks + a safe alternative"),
            "severity": _str("'warning' or 'blocker' (default warning)"),
            "declined_change": _str("ONLY for genuine breakage: the specific requested change you "
                                    "are NOT applying. Omit when recording an objection while "
                                    "still applying the request."),
        }},
    },
    {
        "name": "ask_clarifications",
        "description": "Change-Analysis gate (kind='analysis'): ask the PM ONE batch of "
                       "implementation-shaping questions, grounded in the code you read. Each "
                       f"question is functional ({_AUTHORITY_NAME} PMs know {_DOMAIN_NAME} messages/flows but are not "
                       "developers); attach 2-4 options and a per-option consequence in plain "
                       "language. NEVER invent candidate values: an option proposing a concrete "
                       "NEW identifier/value MUST set proposed_value (the platform verifies each "
                       "against the code and attaches the occupancy evidence; such options are "
                       "refused without verified `evidence`, and a recommendation cannot ride on "
                       "a value found occupied). When the true value is for an authority/PM to "
                       "assign, offer a defer option instead of minting candidates. Recommend an "
                       "option only when you have verified grounds for it — omitting `recommended` "
                       "is always acceptable. Defer pure code-MECHANISM questions to Phase B. "
                       "ENDS your pass — the run stops for the PM's answers.",
        "input_schema": {"type": "object", "required": ["questions"], "properties": {
            "questions": {"type": "array", "description": "the batch (1-7 questions)", "items": {
                "type": "object", "properties": {
                    "id": _str("short id, e.g. 'invalid-code-behaviour'"),
                    "text": _str("the functional question in plain language"),
                    "options": {"type": "array", "items": {"type": "object", "properties": {
                        "id": _str("short option id"),
                        "label": _str("plain-language choice the PM picks"),
                        "consequence": _str("what this choice implies, sourced from code you read"),
                        "proposed_value": _str(
                            "REQUIRED if this option proposes a concrete NEW identifier/value to "
                            "allocate (enum value, code, constant, API/element name): the exact "
                            "literal, e.g. 'BT' or '80'. Omit for options that propose no new "
                            "value (behavioural choices, reuse-existing, defer-to-authority)."),
                    }}},
                    "recommended": _str("id of the recommended option — only with verified "
                                        "grounds; omit when unsure"),
                    "evidence": {"type": "array", "items": {"type": "object"},
                                 "description": "{claim, file} citations grounding the question — "
                                                "REQUIRED (files you actually read this run) when "
                                                "any option carries proposed_value; optional "
                                                "otherwise"},
                }}},
        }},
    },
    {
        "name": "propose_plan",
        "description": "Change-Analysis final gate: present the implementation PLAN for "
                       "ratification with TWO views — functional_plan (PM-facing) and "
                       "technical_analysis (full fidelity, tech-lead-ratified) — plus a "
                       "machine-readable flow_spec that owns step IDs. You MUST pass `evidence`: "
                       "≥2 citations to files you actually read. Ungrounded plans are rejected. ENDS your pass.",
        "input_schema": {"type": "object",
            "required": ["summary", "functional_plan", "technical_analysis", "evidence"],
            "properties": {
                "summary": _str("1-3 plain sentences a business user understands"),
                "functional_plan": {"type": "object",
                    "description": "PM-facing: {overview, steps:[...], affected_flows, compatibility, "
                                   "open_questions} — every statement derived from a technical finding"},
                "technical_analysis": {"type": "object",
                    "description": "full fidelity: {impacted_repos, modules, flows, "
                                   "schema_inventory:[{repo,path,namespace}], data_model_changes, "
                                   "reuse_findings, constraints, risks, user_rectifications:"
                                   "[{requested, applied, feasibility:'verified'|'adjusted', "
                                   "repercussions}] — one entry per plan direction the PM "
                                   "rectified (binding functional choices, repercussions stated)}"},
                "flow_spec": {"type": "object",
                    "description": "actors/steps/messages/states; steps carry stable ids the BRD/TSD "
                                   "render from. MUST include party_flows: one entry per touched API "
                                   "(existing or new) — {api, classification:'new'|'existing_modified', "
                                   "parties:[only those actually involved], hops:[{from, to, message, "
                                   "evidence:<code file read or domain_docs_search source>, "
                                   "confidence:'confirmed'|'assumed', note?:<short user-visible detail "
                                   "for that hop, shown under the arrow>}]}. Docs may confirm a hop, "
                                   "only code may originate one; a hop backed by neither is 'assumed'"},
                "evidence": {"type": "array",
                    "description": "≥2 items {claim, file, line?} citing files you read this run "
                                   "(`line` required for a file you read only in ranges)",
                    "items": {"type": "object", "properties": {
                        "claim": _str("what this file proves"),
                        "file": _str("repo-relative path you read_file'd this run"),
                        "line": {"type": "integer", "description": "line the claim is about"},
                    }}},
            }},
    },
    {
        "name": "read_file",
        "description": "Read a file from a selected repo's working tree (required before editing it). "
                       "repo_id is optional — omit it and it's auto-resolved when there's one "
                       "selected repo or the path exists in exactly one.",
        "input_schema": {"type": "object", "required": ["path"], "properties": {
            "repo_id": _str("optional selected repo id; omit to auto-resolve"),
            "path": _str("repo-relative path"),
            "start_line": {"type": "integer"}, "end_line": {"type": "integer"},
        }},
    },
    {
        "name": "grep",
        "description": "Search files with git grep. OMIT repo_id to search ALL selected repos "
                       "(the discovery default — schema definitions and their consumers often "
                       "live in DIFFERENT repos; a single-repo search silently misses the rest).",
        "input_schema": {"type": "object", "required": ["pattern"], "properties": {
            "repo_id": _str("optional: one repo id to scope to; omit to search every selected repo"),
            "pattern": _str("regex/string to find"),
            "path": _str("optional path filter (single-repo mode only)"),
        }},
    },
    {
        "name": "glob",
        "description": "List files matching a glob pattern (e.g. **/*.java). OMIT repo_id to "
                       "search ALL selected repos.",
        "input_schema": {"type": "object", "required": ["pattern"], "properties": {
            "repo_id": _str("optional: one repo id to scope to; omit to search every selected repo"),
            "pattern": _str("glob pattern"),
        }},
    },
    {
        "name": "edit_file",
        "description": "Replace an exact old_string with new_string. old_string must uniquely "
                       "identify one location (include surrounding context).",
        "input_schema": {"type": "object",
                         "required": ["repo_id", "path", "old_string", "new_string"], "properties": {
            "repo_id": _str("selected repo id"), "path": _str("repo-relative path"),
            "old_string": _str("exact text to replace"), "new_string": _str("replacement"),
        }},
    },
    {
        "name": "create_file",
        "description": "Create a new file (fails if it exists).",
        "input_schema": {"type": "object", "required": ["repo_id", "path", "content"], "properties": {
            "repo_id": _str("selected repo id"), "path": _str("repo-relative path"),
            "content": _str("file contents"),
        }},
    },
    {
        "name": "delete_file",
        "description": "Delete a file from the working tree.",
        "input_schema": {"type": "object", "required": ["repo_id", "path"], "properties": {
            "repo_id": _str("selected repo id"), "path": _str("repo-relative path"),
        }},
    },
    {
        "name": "find_existing_xsd",
        "description": "Search indexed XSD schemas by path/namespace BEFORE creating a new one "
                       "(prefer extending an existing schema). OMIT repo_id to search ALL "
                       "selected repos — schemas usually live in the core/framework repo.",
        "input_schema": {"type": "object", "required": ["query"], "properties": {
            "repo_id": _str("optional: one repo id to scope to; omit to search every selected repo"),
            "query": _str("name/namespace fragment to search"),
        }},
    },
    {
        "name": "schema_guardian",
        "description": "Deterministic reuse-vs-create CHECK on a proposed .xsd: per element, is it "
                       "redundant (already defined identically elsewhere → REUSE), conflict (defined "
                       "differently → ESCALATE, don't fork), or novel (new is justified)? Run it after "
                       "writing/editing a schema and BEFORE finalizing reuse/extend/new — it catches "
                       "both 'created new when one existed' and 'reused into a conflict'. Escalates "
                       "breaking/ambiguous cases instead of deciding.",
        "input_schema": {"type": "object", "required": ["path"], "properties": {
            "path": _str("repo-relative path to the proposed/edited .xsd in the working tree"),
            "repo_id": _str("optional selected repo id; omit to auto-resolve from the path"),
        }},
    },
    {
        "name": "module_context",
        "description": "Fetch index-time MODULE-WISE context (summary, functional flow, key types, "
                       "entry points, Java version, intra-repo deps) for a Maven module BY NAME or "
                       "path. Omit `module` to list all modules first. Use to orient before diving "
                       "into a module. Low-authority — confirm specifics with read_file/ast_query.",
        "input_schema": {"type": "object", "required": [], "properties": {
            "repo_id": _str("optional selected repo id; omit to auto-resolve when one repo is selected"),
            "module": _str("module name or repo-relative path, e.g. 'api-gateway' or "
                           f"{_MODULE_PATH_EXAMPLE}; omit to list all modules"),
        }},
    },
    {
        "name": "flow_context",
        "description": "Index-time API FLOW MAP for a repo, with key-based retrieval: omit `flow` "
                       "to get the INDEX (summary + which APIs carry the actual transaction/debit-"
                       "credit leg vs the meta APIs + the list of flow NAMES); call again with "
                       "`flow=<name>` to pull one flow's step sequence. Use it FIRST to orient "
                       "before deciding reuse-vs-new — but treat it as a LOOKUP AID, NOT a source "
                       "of truth: it is generated at index time and may be stale, wrong, or "
                       "INCOMPLETE. Verify what it says against the code, and actively check for "
                       "flows it does NOT mention before concluding a flow doesn't exist.",
        "input_schema": {"type": "object", "required": [], "properties": {
            "repo_id": _str("optional selected repo id; omit to auto-resolve when one repo is selected"),
            "flow": _str("flow name (or fragment) to retrieve; omit to list the index"),
        }},
    },
    {
        "name": "read_doc",
        "description": "Pull BRD / Tech-Spec / ratified-plan / prior-XSD-assessment content on "
                       "demand. The prompt carries only a section OUTLINE + compliance seed — call "
                       "this to fetch a section's full body by `heading`, or search bodies by "
                       "`query`. Omit both for the outline. `doc` is 'brd', 'tsd', 'plan' (the "
                       "FULL ratified Change-Analysis plan — use it whenever a plan block in your "
                       "prompt says CLIPPED/OMITTED), or 'assessment' (a prior DOCUMENT-ONLY XSD "
                       "verdict, when one exists — ADVISORY: verify against code, never inherit "
                       "its conclusion). Omit `doc` to span all.",
        "input_schema": {"type": "object", "properties": {
            "doc": _str("'brd', 'tsd', or 'assessment'; omit to search all"),
            "heading": _str("exact-ish section heading to fetch (e.g. '10. Regulatory & Compliance')"),
            "query": _str("keywords to search section bodies when you don't know the heading"),
        }},
    },
    {
        "name": "code_search_semantic",
        "description": "ADVISORY semantic (RAG/embedding) search over the indexed code — discovers "
                       "where a CONCEPT lives when you don't know the exact keywords. Use your "
                       "judgement: it's a fuzzy match, can be noisy or incomplete, and is NOT a "
                       "substitute for grep (exact strings) or ast_query (a file's structure). "
                       "Skip it when grep/glob already pinpoints what you need. Confirm hits with read_file.",
        "input_schema": {"type": "object", "required": ["query"], "properties": {
            "query": _str("the concept you're looking for, in natural language"),
            "top_k": {"type": "integer", "description": "max results (default 8, max 20)"},
        }},
    },
    {
        "name": "domain_docs_search",
        "description": f"Semantic search over the OFFICIAL {_DOCS_LABEL} documents in the knowledge base "
                       "(circulars, product docs, API specs, error-code tables, wire-format samples). "
                       "Use it to CONFIRM party/flow/field claims and to name the parties behind an "
                       "endpoint the code forwards to. SUPPORTING evidence only: a doc may confirm a "
                       "hop the code shows, it must NEVER originate a hop or party the code does not "
                       "show — if neither code nor docs cover it, mark the hop 'assumed', do not fill "
                       "the gap from memory. Cite the returned source name next to every claim you use.",
        "input_schema": {"type": "object", "required": ["query"], "properties": {
            "query": _str(f"the {_DOMAIN_NAME} topic/flow/API you need official documentation on"),
            "top_k": {"type": "integer", "description": "max results (default 8, max 20)"},
        }},
    },
    {
        "name": "verify_change",
        "description": "Build + check your changes: compiles the touched modules IN DEPENDENCY ORDER "
                       "(installing changed dependency modules first, and regenerating JAXB from any "
                       "changed XSD) and returns the real file:line errors. Because it installs deps "
                       "first, it reflects new generated classes that a single-module `mvn compile` "
                       "would miss. Diagnostic — the runtime re-verifies authoritatively afterwards; "
                       "if no local build toolchain exists it tells you CI will verify.",
        "input_schema": {"type": "object", "properties": {
            "repo_id": _str("optional selected repo id to scope the check"),
        }},
    },
    {
        "name": "symbol_graph",
        "description": "CHEAP structural lookup: where a symbol/method is defined + which methods "
                       "call it, from the code index. Prefer this over grep for 'who calls this'. "
                       "Results are advisory (index may lag the clone) — confirm with ast_query/read_file.",
        "input_schema": {"type": "object", "required": ["symbol"], "properties": {
            "repo_id": _str("optional selected repo id; omit to auto-resolve"),
            "symbol": _str("method/type name"),
        }},
    },
    {
        "name": "callers",
        "description": "Who calls a method/symbol, across ALL selected repos (the consumer list to "
                       "update before changing a signature). Broader than symbol_graph (one repo) — "
                       "use it for multi-repo blast radius. Advisory (index may lag); confirm with grep.",
        "input_schema": {"type": "object", "required": ["symbol"], "properties": {
            "symbol": _str("method/function name to find callers of"),
            "repo_id": _str("optional: limit to one selected repo; omit to search all"),
        }},
    },
    {
        "name": "impact_analysis",
        "description": "Blast radius of changing a symbol: who CALLS it, who SUBCLASSES/IMPLEMENTS it, "
                       "and the SCOPE FENCE — the set of files your change must stay within. Call this "
                       "BEFORE editing a shared method/type: it tells you what downstream to update "
                       "(don't miss them) AND which files are out of scope (don't drift into them). "
                       "Richer than callers (adds inheritance). Advisory (index may lag) — confirm with grep.",
        "input_schema": {"type": "object", "required": ["symbol"], "properties": {
            "symbol": _str("method/class/interface name whose blast radius you need"),
            "repo_id": _str("optional: limit to one selected repo; omit to search all"),
        }},
    },
    {
        "name": "jaxb_accessors",
        "description": "The Java class bound to an XSD element (JAXB element→Java link index), so you "
                       "open the right generated/consumer class instead of guessing getter names. "
                       "Advisory — confirm exact getX()/setX() with read_file/ast_query.",
        "input_schema": {"type": "object", "required": ["element"], "properties": {
            "element": _str(f"XSD element name, {_ELEMENT_EXAMPLES}"),
            "repo_id": _str("optional: limit to one selected repo; omit to search all"),
        }},
    },
    {
        "name": "show_diff",
        "description": "Show YOUR accumulated change-set so far as a unified diff (all files you've "
                       "added/modified/deleted this run). SELF-CHECK with it before verify_change and "
                       "before finishing — confirm you changed every file the plan needs and nothing else.",
        "input_schema": {"type": "object", "properties": {
            "repo_id": _str("optional: limit to one selected repo; omit for all"),
        }},
    },
    {
        "name": "git_history",
        "description": "git blame/log for a file — pass a line range to BLAME those lines (who/when/"
                       "which commit), or omit it for the file's recent commits. Read it BEFORE changing "
                       "code you don't understand (a workaround, an odd constant) so you don't undo intent.",
        "input_schema": {"type": "object", "required": ["path"], "properties": {
            "path": _str("repo-relative file path"),
            "repo_id": _str("optional selected repo id; omit to auto-resolve"),
            "start_line": {"type": "integer", "description": "optional: first line to blame"},
            "end_line": {"type": "integer", "description": "optional: last line to blame"},
        }},
    },
    {
        "name": "ast_query",
        "description": "Parse a .java file in the working tree (ground truth) and list its classes, "
                       "inheritance, methods, and imports. Use for precise structure of one file.",
        "input_schema": {"type": "object", "required": ["path"], "properties": {
            "repo_id": _str("optional selected repo id; omit to auto-resolve"),
            "path": _str("repo-relative .java path"),
        }},
    },
    {
        "name": "run_command",
        "description": "Run an allowlisted command in the clone to DIAGNOSE — build/vcs "
                       "(git, mvn, mvnw, javac, java) and read-only inspection (grep, cat, head, tail, "
                       "ls, wc, sort), e.g. 'mvn compile', 'git diff', 'grep -rn ReqCircle target/'. "
                       "Use grep here to search generated/gitignored files the grep tool can't. "
                       "Diagnosis only: it does NOT decide verification, and a non-zero exit is "
                       "information, not a failure. Mutation is not allowed here — use the edit tools.",
        "input_schema": {"type": "object", "required": ["argv"], "properties": {
            "repo_id": _str("optional selected repo id; omit to auto-resolve when one repo is selected"),
            "argv": {"type": "array", "items": {"type": "string"},
                     "description": "argv list, e.g. [\"mvn\",\"compile\"]"},
            "timeout_s": {"type": "integer", "description": "optional per-command timeout"},
        }},
    },
    {
        "name": "read_output",
        "description": "Page through the FULL output of an earlier run_command whose result was "
                       "truncated (its marker names the id, e.g. \"out1\"). Use this to read the "
                       "part of a long build log the excerpt omitted instead of re-running the "
                       "build. Line-ranged: pass start_line/end_line to page.",
        "input_schema": {"type": "object", "required": ["id"], "properties": {
            "id": _str("the id from the [long output …] truncation marker, e.g. \"out1\""),
            "start_line": {"type": "integer", "description": "first line to return (1-based)"},
            "end_line": {"type": "integer", "description": "last line to return (inclusive)"},
        }},
    },
    {
        "name": "record_fact",
        "description": "Pin ONE load-bearing fact to your FACT SHEET the moment you learn it — "
                       "an existing value/constant binding (e.g. a code value already in use, "
                       "with its file), a human decision, or a verified constraint. The sheet "
                       "stays pinned in your context all run and travels to later phases, so "
                       "record facts your final answer will depend on — never re-derive them "
                       "from memory. A file source must be one you actually read this run; "
                       "otherwise use 'human_decision' or 'assumption'. Keep it to genuinely "
                       "load-bearing facts (cap 30) — this is a sheet, not a journal.",
        "input_schema": {"type": "object", "required": ["fact", "source"], "properties": {
            "fact": _str("ONE sentence, concrete and self-contained, e.g. \"'BT' is bound to "
                         "P2M_DEEMED_RESP_CODE (response code, in use)\""),
            "source": _str("the exact file you read that establishes it, or 'human_decision' "
                           "or 'assumption'"),
            "supersedes": _str("optional id (e.g. 'F3') of a sheet entry this replaces"),
        }},
    },
]

# Offered to the model ONLY when the LSP is enabled (high-RAM host) — otherwise the
# model never sees it and won't waste a turn on a degraded call.
if settings.agentic_lsp_enabled:
    TOOL_SCHEMAS.append({
        "name": "lsp_diagnostics",
        "description": "Type-aware INLINE diagnostics (errors/warnings) for one .java file via the "
                       "Java language server — a fast spot-check while editing, complementing "
                       "verify_change (the authoritative mvn gate). Advisory; index/LSP may lag.",
        "input_schema": {"type": "object", "required": ["repo_id", "path"], "properties": {
            "repo_id": _str("selected repo id"), "path": _str("repo-relative .java path"),
        }},
    })
