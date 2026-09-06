# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Code Change Agent.

Given a Tech Spec + BRD + Code RAG context, generates Java/Spring Boot code changes
as structured output with file markers. Supports iterative feedback loops.

Output format:
    ## Analysis
    <what and why>

    <<FILE: com/npci/upi/service/SomeService.java>>
    <full file content>
    <<END_FILE>>

    ## Summary
    <summary of changes>
"""
import logging
from app.core.prompts import render_prompt
import re
from collections.abc import AsyncGenerator

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.llm import stream_llm
from app.rag.retrieval import retrieve, build_context
from app.models.document_chunk import CODE_SOURCE_CATEGORIES, DocCategory
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE, safe_format

logger = logging.getLogger(__name__)

FILE_MARKER_START = "<<FILE:"
FILE_MARKER_END   = "<<END_FILE>>"

# Domain vocabulary from the active pack (genericisation sweep). The Java /
# Spring Boot scaffold is platform truth (both reference fixtures are Maven /
# Spring); the org name, feature domain, path examples and domain coding rules
# are pack content. For UPI the assembled prompt is byte-identical to the
# previous hardcoded text (prompt-snapshot-verified).
from app.core.domain.registry import prompt_block

_ORG = prompt_block("authority", "the platform operator")
_ORG_FULL = prompt_block("authority_full_name", "")
_DEVELOPER_ORG = f"{_ORG} ({_ORG_FULL})" if _ORG_FULL else _ORG
_DOMAIN = prompt_block("domain_name", "platform")
_PATH_EXAMPLE = prompt_block("code_path_example",
                             "src/main/java/com/example/app/SomeClass.java")
_REUSE_NOTES = prompt_block(
    "code_change_reuse_notes",
    "   - Reuse existing utility, entity, and enum types instead of introducing parallel ones",
)
_DOMAIN_CODING_NOTES = prompt_block(
    "code_change_domain_notes",
    "- Handle quantities the domain treats as exact (amounts, counts, durations) "
    "as integers, never floating-point",
)

SYSTEM_PROMPT_TEMPLATE = ("""You are an expert Java/Spring Boot developer at """
                          + _DEVELOPER_ORG + """.

Your task is to implement a """ + _DOMAIN + """ feature by MODIFYING the existing codebase. You must work with the real
source files from the repository — add new files where needed and modify existing files to integrate
the new feature.
{multi_repo_section}
## Existing Codebase — Relevant Source Files
{code_rag_context}
{impact_block}
## Existing Codebase — Directory Tree (top directories by file count)
{file_tree}

The directory tree above shows WHERE source lives and at what density — use it
to pick the right package for any NEW file you create. EXACT file paths for
files you can read and modify are in the "Relevant Source Files" section above.

## Instructions

1. Analyse the Tech Spec and BRD carefully. Identify which EXISTING files need to be modified
   and what NEW files need to be created.

2. For EXISTING files being modified:
   - Use the EXACT file path from the "Relevant Source Files" section
     (e.g. """ + _PATH_EXAMPLE + """). The directory
     tree shows package structure but not individual file paths — only files
     listed in "Relevant Source Files" are guaranteed to exist.
   - Include the COMPLETE modified file content — not just the changed parts
   - Preserve all existing code that doesn't need to change
   - Add imports, fields, methods as needed

3. For NEW files:
   - Use the directory tree below to pick the right package directory
     (matches existing code's package layout)
   - Follow existing naming conventions visible in the "Relevant Source Files"

4. Use the existing codebase patterns:
   - Follow existing class naming, annotation styles, and design patterns
""" + _REUSE_NOTES + """

## Required Output Format

Start with a ## Analysis section explaining:
- Which existing files will be modified (and why)
- Which new files will be created (and why)
{output_format_directive}

End with a ## Summary section listing each file (modified/new) and a one-line description.

## Coding Standards
- Package: MUST match the existing package structure from the file tree
- Annotations: use existing patterns (@Service, @RestController, @Repository, etc.)
- Error handling: use existing exception classes from the codebase
- Logging: use SLF4J with @Slf4j annotation
- No hardcoded secrets, URLs, or credentials — use @Value injection
- Input validation: add @Valid and @NotNull where appropriate
- Follow OWASP secure coding guidelines for Java
""" + _DOMAIN_CODING_NOTES + """

If the user provides feedback, regenerate the affected files incorporating all feedback points.
Keep files not mentioned in feedback unchanged.

""" + ANTI_INJECTION_CLAUSE)


# Single-repo output format directive (legacy / single-repo runs). The file
# text is domain-neutral; the illustrative path comes from the pack.
_SINGLE_REPO_OUTPUT_DIRECTIVE = render_prompt(
    "agents/code_change/single_repo_output_directive.md",
    FILE_PATH_EXAMPLE=_PATH_EXAMPLE,
)

# Multi-repo output format directive — operators must prefix every file with
# its [repo-label] so the deployment pipeline routes to the correct repo.
# Example labels/paths are illustrative pack content; real labels come from
# the run's repo summaries at request time.
_MULTI_REPO_OUTPUT_DIRECTIVE = render_prompt(
    "agents/code_change/multi_repo_output_directive.md",
    REPO_LABEL_EXAMPLE_CORE=prompt_block("repo_label_example_core", "Core Library"),
    REPO_PATH_EXAMPLE_CORE=prompt_block(
        "repo_path_example_core", "src/main/java/com/example/core/SomeDto.java"),
    REPO_LABEL_EXAMPLE_APP=prompt_block("repo_label_example_app", "Application"),
    REPO_PATH_EXAMPLE_APP=prompt_block(
        "repo_path_example_app", "src/main/java/com/example/app/SomeHandler.java"),
)


def _build_multi_repo_section(repo_summaries: list[dict]) -> tuple[str, str]:
    """Return (multi_repo_section, output_format_directive) for the prompt template.

    When >=2 repos are scoped to this Phase B run, emit the multi-repo
    declaration + the [label]-prefixed output directive. Otherwise return
    empty string + the legacy single-repo directive.
    """
    if len(repo_summaries) < 2:
        return "", _SINGLE_REPO_OUTPUT_DIRECTIVE

    lines = [
        "",
        "## Available Repositories (this Phase B run)",
        "",
        "This change spans MULTIPLE repositories. Each file in the tree below "
        "belongs to exactly one repo, shown by the `## Repo:` heading it sits under.",
        "",
    ]
    for r in repo_summaries:
        lines.append(
            f"- **{r['label']}** ({r['gitlab_repo']}) — {r['file_count']} files"
        )
    lines.append("")
    lines.append(
        "When generating files, you MUST prefix every file marker with the "
        "target repo label in square brackets (see Output Format below)."
    )
    lines.append("")
    return "\n".join(lines), _MULTI_REPO_OUTPUT_DIRECTIVE


# ── File parsing ───────────────────────────────────────────────────────────────

_SRC_EXT_GROUP = r"(?:java|py|ts|tsx|js|jsx|kt|go|rs|sql|xml|yaml|yml|json|properties|gradle|sh)"


def _dump_failed_output(output: str) -> str | None:
    """When parse extracted 0 files, dump the raw LLM output to disk so we
    can examine the actual format the model emitted. Returns the dump path
    or None on any error (best-effort, never raises)."""
    try:
        import os
        import time as _t
        from app.agents.agentic_events import redact
        dump_dir = os.environ.get(
            "CODE_CHANGE_FAILURE_DUMP_DIR",
            "/appdata/agentapp/artifacts/code_change_failures",
        )
        os.makedirs(dump_dir, exist_ok=True)
        ts = _t.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(dump_dir, f"parse_failed_{ts}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(redact(output))
        return path
    except Exception as e:
        logger.warning("Failed to dump parse-failed output: %s", e)
        return None


def parse_files_from_output(
    output: str,
    path_to_repo: dict[str, str] | None = None,
    repo_label_to_id: dict[str, str] | None = None,
    primary_repo_id: str | None = None,
) -> list[dict]:
    """
    Extract [{path, content, repo_id}] from the agent's output.

    Tolerates four output formats — all support an optional `[repo-label]`
    prefix BEFORE the path so multi-repo runs can route each file to the
    correct CodeRepo:

      1. Strict marker form (system-prompt-prescribed):
         <<FILE: [UPI Core] src/main/.../Foo.java>>
         <content>
         <<END_FILE>>

      2. Fenced-code form with path comment as first line:
         ```java
         // [UPI Core] src/main/.../Foo.java
         package ...
         ```

      3. Path-comment header, content runs until the next header.

      4. Markdown header + bare fenced code block:
            ### File: [UPI Core] src/main/.../Foo.java
            ```java
            ...
            ```

    Repo resolution per file (in priority order):
      a. Explicit `[label]` prefix on the marker — looked up in `repo_label_to_id`.
      b. If no prefix AND `path` matches an indexed file, use `path_to_repo[path]`.
      c. Fall back to `primary_repo_id` (first repo in the run).
      d. If none of the above resolve, `repo_id` is None and a WARN is logged.

    Single-repo runs (passing only path_to_repo with one repo, primary_repo_id
    set) still work — the prefix is optional in that case.
    """
    files: list[dict] = []
    seen_paths: set[str] = set()

    path_to_repo = path_to_repo or {}
    repo_label_to_id = repo_label_to_id or {}

    def _resolve_repo(label: str | None, path: str) -> tuple[str | None, str]:
        """Return (repo_id, source) where source is 'label'|'path-match'|'primary'|'none'.
        Used purely for diagnostics/logging."""
        if label:
            label_norm = label.strip().lower()
            for k, v in repo_label_to_id.items():
                if k.lower() == label_norm:
                    return v, "label"
            logger.warning(
                "code_change: unrecognised repo label %r on path %s; "
                "available labels=%s",
                label, path, list(repo_label_to_id.keys()),
            )
        if path in path_to_repo:
            return path_to_repo[path], "path-match"
        if primary_repo_id:
            return primary_repo_id, "primary"
        return None, "none"

    # Optional `[label]` prefix on the path. Captured into a separate group
    # so a missing prefix still parses cleanly (single-repo runs).
    LABEL_PREFIX = r"(?:\[(?P<label>[^\]]+)\]\s+)?"

    # Form 1 — strict markers, with optional [label] prefix.
    strict_re = re.compile(
        r"<<FILE:\s*" + LABEL_PREFIX + r"(?P<path>.+?)>>\n(?P<content>.*?)<<END_FILE>>",
        re.DOTALL,
    )
    for m in strict_re.finditer(output):
        path = m.group("path").strip()
        if path and path not in seen_paths:
            repo_id, _ = _resolve_repo(m.group("label"), path)
            files.append({
                "path": path, "content": m.group("content").rstrip(),
                "repo_id": repo_id,
            })
            seen_paths.add(path)

    # Form 2 — fenced code with leading path comment, optional [label].
    # The `FILE:` / `PATH:` prefix is optional — frontier Claude models
    # like to write `// FILE: src/main/Foo.java` instead of bare
    # `// src/main/Foo.java`. Both shapes accepted.
    fenced_re = re.compile(
        r"```[A-Za-z0-9_+-]*\s*\n"
        r"\s*(?://|#)\s*"
        r"(?:(?:FILE|File|file|PATH|Path|path)\s*[:\-]\s*)?"
        + LABEL_PREFIX +
        r"(?P<path>[\w./\-]+\." + _SRC_EXT_GROUP + r")\s*\n"
        r"(?P<content>.*?)\n```",
        re.DOTALL,
    )
    for m in fenced_re.finditer(output):
        path = m.group("path").strip()
        if path and "/" in path and path not in seen_paths:
            repo_id, _ = _resolve_repo(m.group("label"), path)
            files.append({
                "path": path, "content": m.group("content").rstrip(),
                "repo_id": repo_id,
            })
            seen_paths.add(path)

    # Form 3 — bare path-comment header, content runs until next header/EOF.
    # `FILE:` / `PATH:` prefix optional (matches frontier Claude phrasing).
    if not files:
        path_header_re = re.compile(
            r"(?:^|\n)\s*(?://|#)\s*"
            r"(?:(?:FILE|File|file|PATH|Path|path)\s*[:\-]\s*)?"
            + LABEL_PREFIX +
            r"(?P<path>[\w./\-]+\." + _SRC_EXT_GROUP + r")\s*\n",
        )
        matches = list(path_header_re.finditer(output))
        for i, m in enumerate(matches):
            path = m.group("path").strip()
            if "/" not in path or path in seen_paths:
                continue
            content_start = m.end()
            content_end = matches[i + 1].start() if i + 1 < len(matches) else len(output)
            content = output[content_start:content_end].strip()
            content = re.sub(r"\n\s*\d+\.\s+[A-Z].*$", "", content, flags=re.DOTALL).rstrip()
            if content:
                repo_id, _ = _resolve_repo(m.group("label"), path)
                files.append({"path": path, "content": content, "repo_id": repo_id})
                seen_paths.add(path)

    # Form 4 — markdown header announcing the path + bare fenced code block.
    # Always run (not gated on `if not files`) — this format is specific
    # enough (header line + immediately-following fence with optional
    # blank line) that it doesn't collide with Form 1/2, and the
    # `seen_paths` dedup prevents double-extraction when both styles
    # appear in the same response.
    if True:
        md_header_re = re.compile(
            r"""(?xms)
            ^[\ \t]*
            \#{0,6}\s*
            \*{0,2}\s*
            (?:File\s*[:\-]\s*|Path\s*[:\-]\s*)?
            \*{0,2}\s*
            (?:\[(?P<label>[^\]]+)\]\s+)?     # optional [repo-label]
            (?P<path>[A-Za-z_][\w./\-]*\.""" + _SRC_EXT_GROUP + r""")
            \s*\*{0,2}\s*$
            \s*
            ```[A-Za-z0-9_+-]*\s*\n
            (?P<content>.*?)
            \n```
            """,
        )
        for m in md_header_re.finditer(output):
            path = m.group("path").strip()
            if path and "/" in path and path not in seen_paths:
                content = m.group("content").rstrip()
                content = re.sub(
                    r"^\s*(?://|#)\s*[\w./\-]+\." + _SRC_EXT_GROUP + r"\s*\n",
                    "",
                    content,
                    count=1,
                )
                if content.strip():
                    repo_id, _ = _resolve_repo(m.group("label"), path)
                    files.append({"path": path, "content": content, "repo_id": repo_id})
                    seen_paths.add(path)

    if not files:
        # Diagnostic: dump full output so we can examine WHAT format the model used.
        dump_path = _dump_failed_output(output)
        logger.warning(
            "CodeChangeAgent — parsed 0 files (output_len=%d). Raw output dumped to %s "
            "for inspection. None of the 4 heuristics matched — model likely used a "
            "new format variant.",
            len(output), dump_path or "<dump-failed>",
        )

    logger.info(
        "CodeChangeAgent — parsed %d files from output (len=%d)",
        len(files), len(output),
    )
    return files


# ── Agent ──────────────────────────────────────────────────────────────────────

# Max directories rendered per repo in the file tree. With UPI's two big
# Spring repos (1,300 + 3,100 files), the full rolled-up list is ~1,000+
# directories ≈ 90KB — large enough to push the BRD/TSD past the AiNxt
# gateway's truncation point. Picking the top-N by file count keeps the
# most-populated (= most-relevant) packages and drops the long tail of
# sparse leaf directories. Operators can override via env var without a
# rebuild. Set to 0 to disable the cap and emit every directory.
import os as _os
_DEFAULT_MAX_DIRS_PER_REPO = 80
_MAX_DIRS_PER_REPO = int(_os.getenv("CODE_CHANGE_MAX_DIRS_PER_REPO", _DEFAULT_MAX_DIRS_PER_REPO))


# Per-doc char budget for BRD/TSD compression. Production-grade BRDs from
# Pranathi's blueprint expansion run 40-60KB each; combined that's ~100KB
# of mostly-narrative content. The LLM's code-generation path needs only
# the actionable subset — API contracts, field-level tables, state
# machines, error codes — not the executive summary, regulatory mapping,
# glossary, risk catalogue, etc. Compressor keeps relevant sections,
# drops irrelevant ones, and truncates if still over budget. 0 disables.
_DEFAULT_DOC_COMPRESS_BUDGET = 20000
_DOC_COMPRESS_BUDGET = int(_os.getenv("CODE_CHANGE_DOC_COMPRESS_BUDGET", _DEFAULT_DOC_COMPRESS_BUDGET))

# Section-heading keywords (case-insensitive substring match). Tuned for
# the NPCI BRD blueprint Pranathi documented in document_guides.py +
# typical TSD layouts. KEEP sections are critical context for codegen;
# SKIP sections are narrative/process/audit-trail noise the code agent
# doesn't need. Anything not matching either list defaults to UNKNOWN
# (kept opportunistically while budget allows).
_DOC_KEEP_KEYWORDS = (
    "envisaged change", "api change", "api change inventory",
    "data model", "core entities", "core entity",
    "state machine", "transaction lifecycle", "transaction state",
    "field-level", "field contract", "field tab", "schema",
    "annexure a", "annexure b", "annexure c",
    "error code", "error categor",
    "failure handling", "failure / reversal", "reversal", "idempotency",
    "configuration parameter",
    "envisaged change", "implementation",
    "request", "response", "payload", "endpoint", "interface",
    "tech spec", "technical spec",
    "xsd",
)
_DOC_SKIP_KEYWORDS = (
    "executive summary",
    "background", "current process", "current state",
    "out of scope", "future enhancement",
    "risk", "fraud", "misuse",
    "compliance", "regulatory", "rbi", "dpdp", "npci circular",
    "sla matrix", "operational readiness", "monitoring", "alert",
    "runbook", "capacity",
    "assumption", "dependency", "constraint",
    "glossary", "abbreviation",
    "open question", "decision log",
    "objective", "scope", "approval",
)


def _compress_doc_for_code_change(
    text: str,
    doc_label: str,
    budget: int = _DOC_COMPRESS_BUDGET,
) -> str:
    """Trim BRD/TSD to the subset that code-generation actually needs.

    Heuristic, no LLM call. Splits on H2 markdown headings (`## `), scores
    each section by keyword match against KEEP / SKIP lists, then assembles
    a budget-bounded output:

      pass 1 — include every KEEP section in full (up to budget)
      pass 2 — fill remaining budget with UNKNOWN sections in original order
      drop  — SKIP sections never emitted
      tail  — if budget exhausted mid-section, last section gets truncated
              and a marker line tells the LLM more was elided

    Returns text unchanged if `budget <= 0` or `len(text) <= budget`.
    """
    if budget <= 0 or len(text) <= budget:
        return text

    # Split on H2 headings. Anything before the first H2 is the preamble
    # (gets included as if it were a KEEP section — usually contains the
    # doc title + metadata + summary line).
    import re
    parts = re.split(r"(?m)^## (?!#)", text)
    if len(parts) == 1:
        # No H2 structure — just truncate from the top.
        return text[:budget] + f"\n\n... [{doc_label} truncated at {budget} chars — full doc available via the docgen API]"

    preamble = parts[0]
    sections: list[tuple[str, str]] = []  # (heading, body)
    for chunk in parts[1:]:
        if "\n" in chunk:
            heading, rest = chunk.split("\n", 1)
        else:
            heading, rest = chunk, ""
        sections.append((heading.strip(), rest))

    def _classify(heading: str) -> str:
        h = heading.lower()
        if any(kw in h for kw in _DOC_KEEP_KEYWORDS):
            return "KEEP"
        if any(kw in h for kw in _DOC_SKIP_KEYWORDS):
            return "SKIP"
        return "UNKNOWN"

    classified = [(h, b, _classify(h)) for h, b in sections]

    out_parts: list[str] = [preamble]
    remaining = budget - len(preamble)
    skipped_count = 0
    truncated_in_section = False

    # Pass 1 — KEEP sections in their original document order.
    for h, body, cls in classified:
        if cls != "KEEP":
            continue
        block = f"## {h}\n{body}"
        if remaining <= 0:
            break
        if len(block) <= remaining:
            out_parts.append(block)
            remaining -= len(block)
        else:
            out_parts.append(block[:remaining] + f"\n\n... [section truncated for prompt budget]")
            remaining = 0
            truncated_in_section = True

    # Pass 2 — UNKNOWN sections fill remaining budget.
    for h, body, cls in classified:
        if cls != "UNKNOWN" or remaining <= 0:
            continue
        block = f"## {h}\n{body}"
        if len(block) <= remaining:
            out_parts.append(block)
            remaining -= len(block)
        else:
            # Don't truncate UNKNOWN sections mid-content; just stop.
            break

    skipped_count = sum(1 for _, _, c in classified if c == "SKIP")
    omitted_count = sum(
        1 for h, _, c in classified
        if c == "UNKNOWN" and f"## {h}\n" not in "\n".join(out_parts[1:])
    )

    if skipped_count or omitted_count or truncated_in_section:
        out_parts.append(
            f"\n... [{doc_label} compressed for prompt budget: "
            f"{skipped_count} non-code-relevant section(s) dropped"
            + (f", {omitted_count} other section(s) omitted to fit budget" if omitted_count else "")
            + (", last kept section truncated mid-content" if truncated_in_section else "")
            + "; full doc available via the docgen API]"
        )

    return "\n\n".join(out_parts)


def _rollup_paths_by_dir(paths: list[str]) -> list[tuple[str, int]]:
    """Group source paths by parent directory and return [(dir, count), ...]
    sorted alphabetically by directory path.

    For a 5,000-file Java repo the rolled-up list is typically 10–30× smaller
    than the flat file list. Files at the repo root (no slash) bucket under '.'.

    Does NOT cap — callers should pass through `_cap_dirs_by_count` if they
    want a bounded result.
    """
    from collections import Counter
    counts: Counter[str] = Counter()
    for p in paths:
        parent = p.rsplit("/", 1)[0] if "/" in p else "."
        counts[parent] += 1
    return sorted(counts.items())


def _cap_dirs_by_count(
    dirs: list[tuple[str, int]],
    max_dirs: int = _MAX_DIRS_PER_REPO,
) -> tuple[list[tuple[str, int]], int]:
    """Pick the top `max_dirs` directories by file count, then re-sort the
    selected subset alphabetically for predictable display.

    Returns (top_alpha_sorted, hidden_count). `hidden_count` is the number
    of directories trimmed off — the caller renders an "... and N more
    directories" tail. `max_dirs <= 0` disables the cap and returns
    the input untouched.
    """
    if max_dirs <= 0 or len(dirs) <= max_dirs:
        return dirs, 0
    # Sort by count desc for selection, then alphabetically for display.
    top_by_count = sorted(dirs, key=lambda dn: (-dn[1], dn[0]))[:max_dirs]
    return sorted(top_by_count), len(dirs) - max_dirs


def _get_file_tree(
    db: Session,
    phase_b_run_id: str | None = None,
) -> tuple[str, dict[str, str], dict[str, str], list[dict]]:
    """Build the LLM-facing file tree, scoped to this run's repos when known.

    Returns:
      tree_text         — markdown-formatted file tree, grouped by repo when
                          multiple repos are in scope, flat otherwise
      path_to_repo      — {source_file: repo_id} for parser fallback
      repo_label_to_id  — {label: repo_id} so the parser can resolve
                          `[label]` prefixes to repo_id
      repo_summaries    — [{repo_id, label, gitlab_repo, file_count}] for the
                          system prompt's "Available Repositories" section

    When phase_b_run_id is None or the run has no phase_b_run_repos rows,
    falls back to ALL indexed code files (legacy single-repo behaviour).
    """
    from sqlalchemy import select, text as sql_text
    from app.models.document_chunk import DocumentChunk
    from app.models.code_repo import CodeRepo
    from app.models.phase_b import PhaseBRunRepo

    # Resolve which repos are in scope for this run.
    scoped_repos: list[CodeRepo] = []
    if phase_b_run_id:
        scoped_repos = (
            db.query(CodeRepo)
            .join(PhaseBRunRepo, PhaseBRunRepo.repo_id == CodeRepo.id)
            .filter(PhaseBRunRepo.run_id == phase_b_run_id)
            .order_by(CodeRepo.label)
            .all()
        )

    repo_summaries: list[dict] = []
    repo_label_to_id: dict[str, str] = {}
    path_to_repo: dict[str, str] = {}

    if scoped_repos:
        # Multi-repo (or single-repo via M-2): list files per repo.
        scoped_ids = [r.id for r in scoped_repos]
        # DocCategory is a plain class of string constants (not an Enum),
        # so DocCategory.JAVA_SOURCE IS the string "java_source" — don't
        # call .value on it. See models/document_chunk.py:DocCategory.
        rows = db.execute(sql_text("""
            SELECT
              dc.source_file,
              dc.metadata->>'repo_id' AS repo_id
            FROM document_chunks dc
            WHERE dc.doc_category = :cat
              AND dc.metadata->>'repo_id' = ANY(:repo_ids)
            GROUP BY dc.source_file, dc.metadata->>'repo_id'
            ORDER BY dc.metadata->>'repo_id', dc.source_file
        """), {"cat": DocCategory.JAVA_SOURCE, "repo_ids": scoped_ids}).all()

        files_by_repo: dict[str, list[str]] = {r.id: [] for r in scoped_repos}
        for row in rows:
            files_by_repo.setdefault(row.repo_id, []).append(row.source_file)
            path_to_repo[row.source_file] = row.repo_id

        parts: list[str] = []
        for repo in scoped_repos:
            paths = files_by_repo.get(repo.id, [])
            repo_label_to_id[repo.label] = repo.id
            repo_summaries.append({
                "repo_id":     repo.id,
                "label":       repo.label,
                "gitlab_repo": repo.gitlab_repo,
                "file_count":  len(paths),
            })
            parts.append(f"## Repo: {repo.label}  ({repo.gitlab_repo}, {len(paths)} files)")
            if not paths:
                parts.append("  (no files indexed yet for this repo)")
            else:
                all_dirs = _rollup_paths_by_dir(paths)
                shown_dirs, hidden = _cap_dirs_by_count(all_dirs)
                for d, n in shown_dirs:
                    parts.append(f"  {d}/  ({n} file{'s' if n != 1 else ''})")
                if hidden > 0:
                    parts.append(
                        f"  ... and {hidden} more director{'ies' if hidden != 1 else 'y'} "
                        "with fewer files (use 'Relevant Source Files' for full paths)"
                    )
            parts.append("")
        return "\n".join(parts) or "(No codebase indexed yet)", path_to_repo, repo_label_to_id, repo_summaries

    # Legacy fallback — flat list across ALL indexed code chunks.
    rows = db.execute(
        select(DocumentChunk.source_file, DocumentChunk.metadata_)
        .where(DocumentChunk.doc_category == DocCategory.JAVA_SOURCE)
        .order_by(DocumentChunk.source_file)
    ).all()
    seen: set[str] = set()
    flat: list[str] = []
    for src, meta in rows:
        if src in seen:
            continue
        seen.add(src)
        flat.append(src)
        if meta and isinstance(meta, dict) and meta.get("repo_id"):
            path_to_repo[src] = meta["repo_id"]
    if not flat:
        return "(No codebase indexed yet)", {}, {}, []
    all_dirs = _rollup_paths_by_dir(flat)
    shown_dirs, hidden = _cap_dirs_by_count(all_dirs)
    lines = [f"  {d}/  ({n} file{'s' if n != 1 else ''})" for d, n in shown_dirs]
    if hidden > 0:
        lines.append(
            f"  ... and {hidden} more director{'ies' if hidden != 1 else 'y'} "
            "with fewer files (use 'Relevant Source Files' for full paths)"
        )
    return (
        "\n".join(lines),
        path_to_repo, repo_label_to_id, repo_summaries,
    )


def _build_impact_block(db: Session, tech_spec: str, brd: str) -> str:
    """Sub-slice 20c — Thin compatibility wrapper around the shared helper.

    Kept under its original signature so `_build_system_prompt` (and any
    direct test callers) don't break. New code should import
    `app.agents.impact_block.build_impact_block` directly.
    """
    from app.agents.impact_block import build_impact_block
    return build_impact_block(db, [tech_spec, brd])


def _build_system_prompt(
    db: Session,
    change_request_id: str,
    tech_spec: str,
    brd: str,
    phase_b_run_id: str | None = None,
) -> tuple[str, dict[str, str], dict[str, str], str | None, str]:
    """Retrieve Code RAG context and build the multi-repo-aware system prompt.

    Returns:
      system_prompt        — assembled prompt string
      path_to_repo         — {source_file: repo_id} for parser fallback
      repo_label_to_id     — {label: repo_id} for parser prefix resolution
      primary_repo_id      — first repo in this run (or None)
      doc_context_block    — BRD + Tech Spec wrapped for prepending to user message
    """
    file_tree, path_to_repo, repo_label_to_id, repo_summaries = _get_file_tree(
        db, phase_b_run_id=phase_b_run_id,
    )

    # Retrieve relevant Java source chunks via semantic search
    queries = [
        tech_spec[:500],
        "Spring Boot service controller repository entity",
    ]
    all_chunks: list[dict] = []
    seen_ids: set[str] = set()
    for q in queries:
        # Slice 22d — broaden retrieval beyond Java to all code-source categories.
        # min_score deliberately omitted — see code_change.py:170 history.
        chunks = retrieve(
            q, db, top_k=12,
            categories=list(CODE_SOURCE_CATEGORIES),
        )
        for c in chunks:
            if c["id"] not in seen_ids:
                all_chunks.append(c)
                seen_ids.add(c["id"])

    code_context = (
        build_context(all_chunks, max_tokens=10000)
        if all_chunks
        else "(No codebase indexed — generate from scratch following standard Spring Boot patterns)"
    )

    multi_repo_section, output_format_directive = _build_multi_repo_section(repo_summaries)

    logger.info(
        "CodeChangeAgent — RAG context: %d files in tree, %d chunks retrieved, context_len=%d, repos=%d (run_id=%s)",
        file_tree.count("\n") + 1, len(all_chunks), len(code_context),
        len(repo_summaries), phase_b_run_id or "<none>",
    )

    impact_block = _build_impact_block(db, tech_spec, brd)

    # Compress BRD/TSD to their code-relevant subset before assembling.
    # Production BRDs from Pranathi's blueprint expansion can run 40-60KB
    # each (~89KB combined). The compressor keeps API / schema / state
    # machine / error code sections and drops glossary / regulatory /
    # SLA / risk narrative — none of which the code agent uses. Compression
    # ratio + decisions show up in the PROMPT_SIZE log below.
    brd_raw, tech_spec_raw = brd or "", tech_spec or ""
    brd_for_prompt = _compress_doc_for_code_change(brd_raw, "BRD") if brd_raw else "(BRD not available)"
    tech_spec_for_prompt = _compress_doc_for_code_change(tech_spec_raw, "Tech Spec") if tech_spec_raw else "(Tech Spec not available)"

    system_prompt = safe_format(
        SYSTEM_PROMPT_TEMPLATE,
        multi_repo_section=multi_repo_section,
        file_tree=file_tree,
        code_rag_context=code_context,
        impact_block=impact_block,
        output_format_directive=output_format_directive,
    )

    doc_context_block = (
        f"## Context — Business Requirements (BRD)\n"
        f"{wrap_untrusted(brd_for_prompt, 'BRD')}\n\n"
        f"## Context — Tech Specification\n"
        f"{wrap_untrusted(tech_spec_for_prompt, 'TECH_SPEC')}"
    )

    # PROMPT_SIZE diag — paste this line when a code-gen run produces a
    # truncated / "please share the actual feature request" response from
    # the LLM, so we can see WHICH section is bloating the prompt past the
    # gateway's body cap. tech_spec / brd values are the COMPRESSED sizes
    # (post-_compress_doc_for_code_change); the raw sizes precede them
    # in parens so the compression ratio is visible at a glance.
    logger.info(
        "CodeChangeAgent PROMPT_SIZE total=%d "
        "(file_tree=%d code_rag=%d impact_block=%d "
        "tech_spec=%d[raw=%d] brd=%d[raw=%d] "
        "multi_repo_section=%d output_format_directive=%d) "
        "run_id=%s",
        len(system_prompt),
        len(file_tree), len(code_context), len(impact_block),
        len(tech_spec_for_prompt), len(tech_spec_raw),
        len(brd_for_prompt), len(brd_raw),
        len(multi_repo_section), len(output_format_directive),
        phase_b_run_id or "<none>",
    )

    primary_repo_id = repo_summaries[0]["repo_id"] if repo_summaries else None
    return system_prompt, path_to_repo, repo_label_to_id, primary_repo_id, doc_context_block


async def stream_code_change_turn(
    db: Session,
    change_request_id: str,
    tech_spec: str,
    brd: str,
    conversation_history: list[dict],
    new_user_message: str,
    phase_b_run_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """
    Stream the Code Change Agent's response token by token.

    Args:
        db:                   SQLAlchemy session (for Code RAG retrieval).
        change_request_id:    Used to scope Code RAG chunk retrieval.
        tech_spec:            Tech Spec text from Phase A.
        brd:                  BRD text from Phase A.
        conversation_history: Prior turns as [{"role": "user"|"assistant", "content": "..."}].
        new_user_message:     The user's latest message ("start" for first turn, feedback thereafter).
        phase_b_run_id:       Optional — when provided, scopes the file tree
                              and repo-label resolution to this run's
                              registered repos (multi-repo mode).

    Yields:
        str — text tokens from the LLM stream.
    """
    system_prompt, _, _, _, doc_context_block = _build_system_prompt(
        db, change_request_id, tech_spec, brd,
        phase_b_run_id=phase_b_run_id,
    )
    # Prepend BRD/TSD context (wrapped as untrusted) to the first user message
    if len(conversation_history) == 0:
        user_content = f"{doc_context_block}\n\n---\n{new_user_message}"
    else:
        user_content = new_user_message
    messages = conversation_history + [{"role": "user", "content": user_content}]

    logger.info(
        "CodeChangeAgent — streaming turn, change=%s history_len=%d run_id=%s",
        change_request_id, len(messages), phase_b_run_id or "<none>",
    )

    # max_tokens 32000 — Sonnet 4.6 supports 64K output; multi-repo runs need
    # the headroom for fan-out across files.
    async for chunk in stream_llm(system=system_prompt, messages=messages, max_tokens=32000, agent_name="code_change"):
        yield chunk


def build_parser_context(
    db: Session,
    phase_b_run_id: str | None,
) -> tuple[dict[str, str], dict[str, str], str | None]:
    """Helper for the WS handler — returns the same maps that
    `_build_system_prompt` produces, without rebuilding the full prompt.

    Used so that `parse_files_from_output` after the stream completes can
    resolve `[label]` prefixes and path matches without a duplicate LLM
    call. Cheap (no LLM) — just one DB query + dict construction.
    """
    _, path_to_repo, repo_label_to_id, repo_summaries = _get_file_tree(
        db, phase_b_run_id=phase_b_run_id,
    )
    primary_repo_id = repo_summaries[0]["repo_id"] if repo_summaries else None
    return path_to_repo, repo_label_to_id, primary_repo_id
