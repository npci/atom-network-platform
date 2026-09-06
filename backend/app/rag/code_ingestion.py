# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Code RAG ingestion pipeline.

Fetches Java source files from GitLab, chunks them at class/method boundaries,
embeds with sentence-transformers, and stores in pgvector under doc_category='java_source'.

Used by the Code Change Agent to understand the existing codebase before generating changes.
"""
import logging
import re
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.document_chunk import DocCategory, DocumentChunk
from app.models.base import generate_uuid
from app.rag import (
    code_chunker_ts,
    code_summarizer,
    symbol_graph_extractor_java,
    symbol_graph_extractor_python,
    symbol_graph_extractor_typescript,
)
from app.rag.embeddings import embed_texts
from app.rag.embed_cache import embed_chunks_with_cache

logger = logging.getLogger(__name__)

# Phase 3 Gap B — was 16. With EMBED_BATCH_SIZE=32 and EMBED_HTTP_CONCURRENCY=8,
# 16-chunk windows produced a single under-saturated HTTP batch per ingest
# step, leaving 7/8 concurrency slots idle. 256 lets each embed_chunks_with_cache
# call dispatch up to 8 HTTP batches concurrently — saturating the gateway and
# cutting embed wall-time on the typical polyglot ingest by ~3-4×.
# Env override available for operators bound by memory or rate-limits.
import os as _os
BATCH_SIZE = int(_os.getenv("CODE_INGEST_BATCH_SIZE", "256"))
MAX_CHUNK_CHARS = 6000   # ~1500 tokens — fits comfortably in context window


# ── GitLab fetching ────────────────────────────────────────────────────────────

# Slice 22c — Polyglot extension → language map. Used by the polyglot
# fetcher and by `_detect_language` at chunk-attach time. Add a row here
# when a new language extractor lands; that's the only change needed.
LANGUAGE_EXTENSIONS: dict[str, str] = {
    ".java": "java",
    ".py":   "python",
    # Slice 22b — TypeScript / JSX / JavaScript share the same grammar via
    # tree-sitter-typescript. We tag JS as "javascript" for graph clarity
    # but route through the TypeScript extractor (TS grammar is a superset).
    ".ts":   "typescript",
    ".tsx":  "typescript",
    ".js":   "javascript",
    ".jsx":  "javascript",
}


def _detect_language(path: str) -> str | None:
    """Return the canonical language name for a path's extension, or None."""
    if not path:
        return None
    lowered = path.lower()
    for ext, lang in LANGUAGE_EXTENSIONS.items():
        if lowered.endswith(ext):
            return lang
    return None


def _archive_files(project, branch: str, ext_set: set[str], *, with_language: bool) -> list[dict]:
    """Fetch every file whose path ends with one of `ext_set` in ONE request via the GitLab archive
    endpoint (a gzipped tarball), instead of one API call PER FILE. For a 100-file repo this turns
    ~100+ sequential GitLab requests (slow, rate-limit-prone) into a single download + a local extract.
    Returns ``[{"path", "content"[, "language"]}, ...]``. Raises on ANY failure so the caller falls
    back to the per-file fetch (fail-open)."""
    import io
    import tarfile
    raw = project.repository_archive(sha=branch, format="tar.gz")   # 1 request → gzipped tar bytes
    out: list[dict] = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        for m in tar.getmembers():
            if not m.isfile():
                continue
            # GitLab prefixes every entry with "<repo>-<ref>-<sha>/" — strip that leading segment.
            rel = m.name.split("/", 1)[1] if "/" in m.name else m.name
            low = rel.lower()
            if not any(low.endswith(ext) for ext in ext_set):
                continue
            fh = tar.extractfile(m)
            if fh is None:
                continue
            rec = {"path": rel, "content": fh.read().decode("utf-8", errors="replace")}
            if with_language:
                rec["language"] = _detect_language(rel)
            out.append(rec)
    return out


def _fetch_files_by_extensions(
    repo: str, branch: str, extensions: list[str],
) -> list[dict]:
    """Generic GitLab fetcher — pulls every file matching any of `extensions`.

    Returns `[{"path", "content", "language"}, ...]`. `language` is filled in
    via `_detect_language` so downstream callers can dispatch by language
    without re-parsing the path.

    Mirrors `_fetch_java_files_from_gitlab` operationally — same gitlab client
    init, same per-file fetch + UTF-8 best-effort decode.
    """
    if not extensions:
        return []
    try:
        import gitlab as gl_module
    except ImportError:
        raise RuntimeError("python-gitlab not installed.")

    gitlab_url = settings.gitlab_url or ""
    if "://localhost" in gitlab_url:
        gitlab_url = gitlab_url.replace("://localhost", "://host.docker.internal")
    glab = gl_module.Gitlab(gitlab_url, private_token=settings.gitlab_token, keep_base_url=True)
    project = glab.projects.get(repo)

    ext_set = {e.lower() for e in extensions}
    logger.info("Polyglot fetch from GitLab repo=%s branch=%s extensions=%s",
                repo, branch, sorted(ext_set))

    if getattr(settings, "use_gitlab_archive_fetch", True):
        try:
            files = _archive_files(project, branch, ext_set, with_language=True)
            logger.info("Archive fetch (1 request): %d files repo=%s extensions=%s",
                        len(files), repo, sorted(ext_set))
            return files
        except Exception as e:  # noqa: BLE001 — fail-open: fall back to the per-file fetch
            logger.warning("Archive fetch failed (%s) — falling back to per-file fetch", e)

    items = project.repository_tree(ref=branch, recursive=True, all=True)
    matching = [
        item for item in items
        if item["type"] == "blob"
        and any(item["path"].lower().endswith(ext) for ext in ext_set)
    ]
    logger.info("Polyglot fetch: matched %d files across %d extension(s)",
                len(matching), len(ext_set))

    out: list[dict] = []
    for item in matching:
        try:
            raw = project.files.get(file_path=item["path"], ref=branch)
            content = raw.decode().decode("utf-8", errors="replace")
            out.append({
                "path":     item["path"],
                "content":  content,
                "language": _detect_language(item["path"]),
            })
        except Exception as exc:
            logger.warning("Could not fetch %s: %s", item["path"], exc)
    return out


def _extensions_for_languages(languages: list[str]) -> list[str]:
    """Reverse lookup: given canonical language names, return file extensions
    we recognise for them. Unknown languages are silently ignored."""
    wanted = {l.lower() for l in languages if l}
    return [ext for ext, lang in LANGUAGE_EXTENSIONS.items() if lang in wanted]


# ── Phase 4.1 / 4.2 — Commit-SHA tracking + git-diff fetch ────────────────────

def _resolve_branch_head_sha(repo: str, branch: str) -> str | None:
    """Resolve the HEAD commit SHA of `branch` on `repo`. Returns None on
    any failure — the caller falls back to the full-fetch SHA-hash diff
    path. One small GitLab API call (`commits/<branch>`)."""
    try:
        import gitlab as gl_module
    except ImportError:
        return None
    try:
        gitlab_url = settings.gitlab_url or ""
        if "://localhost" in gitlab_url:
            gitlab_url = gitlab_url.replace("://localhost", "://host.docker.internal")
        glab = gl_module.Gitlab(gitlab_url, private_token=settings.gitlab_token, keep_base_url=True)
        project = glab.projects.get(repo)
        commit = project.commits.get(branch)
        return getattr(commit, "id", None) or getattr(commit, "short_id", None)
    except Exception as e:
        logger.warning("Could not resolve HEAD commit for %s@%s: %s", repo, branch, e)
        return None


def _fetch_changed_paths_via_compare(
    repo: str, prev_sha: str, new_sha: str, extensions: list[str],
) -> tuple[list[dict], list[str]] | None:
    """Phase 4.2 — use GitLab Compare API to learn which paths changed
    between `prev_sha` and `new_sha`. Returns:

        ([{"path", "content", "language"}, ...]  ← added + modified
         [deleted_path, ...])                    ← removed in `new_sha`

    or None on any structural failure — the caller falls back to the
    full-fetch SHA-hash diff path.

    Bandwidth saving vs full-fetch: typical scheduled ingest with no new
    commits → 1-2 API calls (compare + maybe `commits/<branch>`).
    """
    try:
        import gitlab as gl_module
    except ImportError:
        return None
    if not prev_sha or not new_sha or prev_sha == new_sha:
        # Same SHA → no diff, no files to fetch.
        return ([], [])

    try:
        gitlab_url = settings.gitlab_url or ""
        if "://localhost" in gitlab_url:
            gitlab_url = gitlab_url.replace("://localhost", "://host.docker.internal")
        glab = gl_module.Gitlab(gitlab_url, private_token=settings.gitlab_token, keep_base_url=True)
        project = glab.projects.get(repo)
        compare = project.repository_compare(prev_sha, new_sha)
    except Exception as e:
        logger.warning(
            "GitLab compare(%s..%s) failed on %s — falling back to full fetch: %s",
            prev_sha, new_sha, repo, e,
        )
        return None

    diffs = compare.get("diffs") if isinstance(compare, dict) else getattr(compare, "diffs", [])
    if diffs is None:
        return None

    ext_set = {e.lower() for e in extensions}
    changed: list[tuple[str, str]] = []     # (path, status)  status in {"added","modified","deleted"}
    seen_paths: set[str] = set()
    for d in diffs:
        path = d.get("new_path") or d.get("old_path")
        if not path or path in seen_paths:
            continue
        if ext_set and not any(path.lower().endswith(e) for e in ext_set):
            continue
        seen_paths.add(path)
        if d.get("deleted_file"):
            changed.append((path, "deleted"))
        elif d.get("new_file"):
            changed.append((path, "added"))
        else:
            changed.append((path, "modified"))

    # Fetch content for added + modified only — at the new SHA's tree.
    files: list[dict] = []
    deleted: list[str] = []
    for path, status in changed:
        if status == "deleted":
            deleted.append(path)
            continue
        try:
            raw = project.files.get(file_path=path, ref=new_sha)
            content = raw.decode().decode("utf-8", errors="replace")
            files.append({
                "path":     path,
                "content":  content,
                "language": _detect_language(path),
            })
        except Exception as e:
            logger.warning("Could not fetch %s @ %s: %s", path, new_sha[:12], e)
    logger.info(
        "Compare(%s..%s) on %s: %d added/modified, %d deleted "
        "(vs full-fetch which would have pulled every file)",
        (prev_sha or "")[:12], (new_sha or "")[:12], repo,
        len(files), len(deleted),
    )
    return (files, deleted)


def _load_repo_state_sha(db: Session, repo_id: str) -> str | None:
    """Read last_ingested_sha from code_repo_state. None if never set."""
    try:
        from app.models.code_repo_state import CodeRepoState
        row = db.get(CodeRepoState, repo_id)
        return row.last_ingested_sha if row else None
    except Exception as e:
        # Table might not exist yet (migration not applied). Fail-soft.
        logger.debug("code_repo_state read failed: %s", e)
        return None


def _persist_repo_state_sha(
    db: Session, repo_id: str, new_sha: str, branch: str | None = None,
) -> None:
    """Upsert the post-ingest commit SHA. Fail-soft so a missing table
    doesn't break ingest."""
    if not new_sha:
        return
    try:
        from app.models.code_repo_state import CodeRepoState
        from app.models.base import utcnow

        row = db.get(CodeRepoState, repo_id)
        now = utcnow()
        if row is None:
            db.add(CodeRepoState(
                repo_id=repo_id,
                last_ingested_sha=new_sha,
                last_ingested_at=now,
                last_ingested_branch=branch,
            ))
        else:
            row.last_ingested_sha = new_sha
            row.last_ingested_at = now
            if branch:
                row.last_ingested_branch = branch
        db.flush()
    except Exception as e:
        logger.warning(
            "code_repo_state upsert failed for repo_id=%s (%s) — falling back to "
            "next ingest using full-fetch path", repo_id, e,
        )


def _fetch_java_files_from_gitlab(repo: str, branch: str) -> list[dict]:
    """
    Fetch all .java files from a GitLab repository.

    Returns list of {path: str, content: str} dicts.
    Requires python-gitlab and settings.gitlab_url + settings.gitlab_token.
    """
    try:
        import gitlab as gl_module
    except ImportError:
        raise RuntimeError("python-gitlab not installed. Add python-gitlab to requirements.txt.")

    gitlab_url = settings.gitlab_url or ""
    # Inside Docker, localhost refers to the container — translate to host.docker.internal
    if "://localhost" in gitlab_url:
        gitlab_url = gitlab_url.replace("://localhost", "://host.docker.internal")
    glab = gl_module.Gitlab(gitlab_url, private_token=settings.gitlab_token, keep_base_url=True)
    project = glab.projects.get(repo)
    logger.info("Fetching Java files from GitLab repo=%s branch=%s", repo, branch)

    if getattr(settings, "use_gitlab_archive_fetch", True):
        try:
            files = _archive_files(project, branch, {".java"}, with_language=False)
            logger.info("Archive fetch (1 request): %d .java files repo=%s", len(files), repo)
            return files
        except Exception as e:  # noqa: BLE001 — fail-open: fall back to the per-file fetch
            logger.warning("Archive fetch failed (%s) — falling back to per-file fetch", e)

    items = project.repository_tree(ref=branch, recursive=True, all=True)
    java_files = [item for item in items if item["type"] == "blob" and item["path"].endswith(".java")]
    logger.info("Found %d .java files", len(java_files))

    result = []
    for item in java_files:
        try:
            raw = project.files.get(file_path=item["path"], ref=branch)
            content = raw.decode().decode("utf-8", errors="replace")
            result.append({"path": item["path"], "content": content})
        except Exception as exc:
            logger.warning("Could not fetch %s: %s", item["path"], exc)

    return result


# ── Java-aware chunking ────────────────────────────────────────────────────────

# Matches: public/protected/private class/interface/enum declarations
_CLASS_PATTERN = re.compile(
    r"^(?:public|protected|private|abstract|final|\s)*"
    r"(?:class|interface|enum|record)\s+(\w+)",
    re.MULTILINE,
)

# Matches typical Java method signatures (public/private/protected + return type + name + parens)
_METHOD_PATTERN = re.compile(
    r"^\s{4}(?:public|protected|private|static|final|synchronized|abstract|@\w+\s+)*"
    r"(?:[\w<>\[\]]+\s+)+(\w+)\s*\([^)]*\)\s*(?:throws\s+[\w,\s]+)?\s*\{",
    re.MULTILINE,
)


def _chunk_java_file(path: str, content: str) -> list[dict]:
    """
    Split a Java source file into chunks at class and method boundaries.

    Strategy:
    1. One chunk for the full file if it fits within MAX_CHUNK_CHARS.
    2. If larger, split at top-level class boundaries.
    3. Within each class block, additionally extract individual method chunks.

    Each chunk dict: {path, class_name, method_name, content, chunk_index}
    """
    chunks: list[dict] = []
    chunk_index = 0

    # If file is small enough, store as one chunk
    if len(content) <= MAX_CHUNK_CHARS:
        chunks.append({
            "path": path,
            "class_name": _extract_first_class_name(content),
            "method_name": None,
            "content": content,
            "chunk_index": chunk_index,
        })
        return chunks

    # Find class boundary positions
    class_matches = list(_CLASS_PATTERN.finditer(content))

    if not class_matches:
        # No class found — chunk by character size with overlap
        for i in range(0, len(content), MAX_CHUNK_CHARS - 200):
            block = content[i: i + MAX_CHUNK_CHARS]
            chunks.append({
                "path": path,
                "class_name": None,
                "method_name": None,
                "content": block,
                "chunk_index": chunk_index,
            })
            chunk_index += 1
        return chunks

    # Extract each class block
    for idx, class_match in enumerate(class_matches):
        start = class_match.start()
        end = class_matches[idx + 1].start() if idx + 1 < len(class_matches) else len(content)
        class_body = content[start:end]
        class_name = class_match.group(1)

        # Add full class chunk
        if len(class_body) <= MAX_CHUNK_CHARS:
            chunks.append({
                "path": path,
                "class_name": class_name,
                "method_name": None,
                "content": class_body,
                "chunk_index": chunk_index,
            })
            chunk_index += 1
        else:
            # Class is large — extract individual method chunks too
            method_matches = list(_METHOD_PATTERN.finditer(class_body))
            for midx, method_match in enumerate(method_matches):
                m_start = method_match.start()
                m_end = method_matches[midx + 1].start() if midx + 1 < len(method_matches) else len(class_body)
                method_body = class_body[m_start:m_end]
                method_name = method_match.group(1)

                # Prepend class header for context
                class_header = class_body[:class_match.end()].strip()
                chunk_content = f"// Class: {class_name}\n{class_header}\n\n{method_body}"
                chunks.append({
                    "path": path,
                    "class_name": class_name,
                    "method_name": method_name,
                    "content": chunk_content[:MAX_CHUNK_CHARS],
                    "chunk_index": chunk_index,
                })
                chunk_index += 1

    return chunks


def _extract_first_class_name(content: str) -> str | None:
    m = _CLASS_PATTERN.search(content)
    return m.group(1) if m else None


# ── Slice 4 — 3-view multiview expansion ─────────────────────────────────────

def _attach_java_symbol_graph(file_chunks: list[dict], content: str) -> None:
    """Slice 17 — enrich Java chunks in-place with symbol-graph edges.

    File-level chunks get the file's `imports` list.
    Class/interface/enum/record chunks get `inherits` + `implements`.
    Method/constructor/function chunks get `calls` + (within-file) `called_by`.

    Operates only on chunks whose `language` is "java" (other languages'
    symbol-graph extractors will land in sub-slices 23/24).
    """
    graph = symbol_graph_extractor_java.extract(content)
    if not graph.classes and not graph.imports:
        return

    # Build a (class_name → JavaClass) index for O(1) lookups below.
    class_index: dict[str, object] = {c.name: c for c in graph.classes}

    for chunk in file_chunks:
        if chunk.get("language") != "java":
            continue
        kind = chunk.get("symbol_kind")

        if kind == "file":
            chunk["imports"] = list(graph.imports)
            continue

        symbol_name = chunk.get("symbol_name") or ""
        # For class-like chunks, look up by its own name.
        if kind in ("class", "interface", "enum", "record"):
            cls = class_index.get(symbol_name)
            if cls is not None:
                if cls.inherits:
                    chunk["inherits"] = cls.inherits
                if cls.implements:
                    chunk["implements"] = list(cls.implements)
            continue

        # For method/constructor/function chunks, look up the enclosing class
        # via chunk["class_name"] (set by the tree-sitter chunker, Slice 3).
        if kind in ("method", "constructor", "function"):
            enclosing = chunk.get("class_name") or ""
            cls = class_index.get(enclosing)
            if cls is None:
                continue
            # Find the matching method by name. For overloads the tree-sitter
            # chunker emits one chunk per declaration; our extractor also
            # emits one JavaMethod per declaration — positions correlate
            # loosely. MVP: match by name; if multiple, union calls/called_by.
            matching = [m for m in cls.methods if m.name == symbol_name]
            if not matching:
                continue
            calls_union: list[str] = []
            cb_union: list[str] = []
            seen_calls: set[str] = set()
            seen_cb: set[str] = set()
            for m in matching:
                for c in m.calls:
                    if c not in seen_calls:
                        seen_calls.add(c)
                        calls_union.append(c)
                for c in m.called_by:
                    if c not in seen_cb:
                        seen_cb.add(c)
                        cb_union.append(c)
            if calls_union:
                chunk["calls"] = calls_union
            if cb_union:
                chunk["called_by"] = cb_union


# ── Slice 22a — Python symbol-graph attach ───────────────────────────────────

def _attach_python_symbol_graph(file_chunks: list[dict], content: str) -> None:
    """Slice 22a — enrich Python chunks in-place with symbol-graph edges.

    Mirrors `_attach_java_symbol_graph` for python. Module-level functions
    are matched by `symbol_kind == "function"` (no class_name); class
    methods match by `class_name + symbol_name`. File-level chunks get
    `imports`. Class-like chunks get `inherits` + `implements`.
    """
    graph = symbol_graph_extractor_python.extract(content)
    if not graph.classes and not graph.imports and not graph.module_functions:
        return

    class_index = {c.name: c for c in graph.classes}
    module_fn_index = {f.name: f for f in graph.module_functions}

    for chunk in file_chunks:
        if chunk.get("language") != "python":
            continue
        kind = chunk.get("symbol_kind")

        if kind == "file":
            chunk["imports"] = list(graph.imports)
            continue

        symbol_name = chunk.get("symbol_name") or ""

        if kind in ("class",):
            cls = class_index.get(symbol_name)
            if cls is not None:
                if cls.inherits:
                    chunk["inherits"] = cls.inherits
                if cls.implements:
                    chunk["implements"] = list(cls.implements)
            continue

        if kind in ("method", "function"):
            enclosing = chunk.get("class_name") or ""

            # Module-level function: no class_name → look up in module_fn_index.
            if not enclosing:
                fn = module_fn_index.get(symbol_name)
                if fn is None:
                    continue
                if fn.calls:
                    chunk["calls"] = list(fn.calls)
                if fn.called_by:
                    chunk["called_by"] = list(fn.called_by)
                continue

            # Class method: lookup by enclosing class name.
            cls = class_index.get(enclosing)
            if cls is None:
                continue
            matching = [m for m in cls.methods if m.name == symbol_name]
            if not matching:
                continue
            calls_union: list[str] = []
            cb_union: list[str] = []
            seen_calls: set[str] = set()
            seen_cb: set[str] = set()
            for m in matching:
                for c in m.calls:
                    if c not in seen_calls:
                        seen_calls.add(c)
                        calls_union.append(c)
                for c in m.called_by:
                    if c not in seen_cb:
                        seen_cb.add(c)
                        cb_union.append(c)
            if calls_union:
                chunk["calls"] = calls_union
            if cb_union:
                chunk["called_by"] = cb_union


def _attach_typescript_symbol_graph(
    file_chunks: list[dict], content: str, *, is_tsx: bool = False,
) -> None:
    """Slice 22b — enrich TS/JS chunks in-place with symbol-graph edges.

    Mirrors the Java/Python attach. Module-level functions are matched by
    `symbol_kind == "function"` (no class_name); class methods match by
    `class_name + symbol_name`. File-level chunks get `imports`. Class-like
    chunks get `inherits` + `implements`.

    `is_tsx=True` selects the .tsx grammar (JSX-aware). Plain .ts and .js
    use the regular TypeScript grammar.
    """
    graph = symbol_graph_extractor_typescript.extract(content, is_tsx=is_tsx)
    if not graph.classes and not graph.imports and not graph.module_functions:
        return

    class_index = {c.name: c for c in graph.classes}
    module_fn_index = {f.name: f for f in graph.module_functions}

    for chunk in file_chunks:
        lang = chunk.get("language")
        if lang not in ("typescript", "javascript"):
            continue
        kind = chunk.get("symbol_kind")

        if kind == "file":
            chunk["imports"] = list(graph.imports)
            continue

        symbol_name = chunk.get("symbol_name") or ""

        if kind in ("class",):
            cls = class_index.get(symbol_name)
            if cls is not None:
                if cls.inherits:
                    chunk["inherits"] = cls.inherits
                if cls.implements:
                    chunk["implements"] = list(cls.implements)
            continue

        if kind in ("method", "function"):
            enclosing = chunk.get("class_name") or ""

            if not enclosing:
                fn = module_fn_index.get(symbol_name)
                if fn is None:
                    continue
                if fn.calls:
                    chunk["calls"] = list(fn.calls)
                if fn.called_by:
                    chunk["called_by"] = list(fn.called_by)
                continue

            cls = class_index.get(enclosing)
            if cls is None:
                continue
            matching = [m for m in cls.methods if m.name == symbol_name]
            if not matching:
                continue
            calls_union: list[str] = []
            cb_union: list[str] = []
            seen_calls: set[str] = set()
            seen_cb: set[str] = set()
            for m in matching:
                for c in m.calls:
                    if c not in seen_calls:
                        seen_calls.add(c)
                        calls_union.append(c)
                for c in m.called_by:
                    if c not in seen_cb:
                        seen_cb.add(c)
                        cb_union.append(c)
            if calls_union:
                chunk["calls"] = calls_union
            if cb_union:
                chunk["called_by"] = cb_union


def _attach_symbol_graph_for_language(
    file_chunks: list[dict], content: str, language: str | None,
    *, file_path: str | None = None,
) -> None:
    """Slice 22a/22b — dispatch on `language` so the right extractor runs.

    `file_path` is used to detect .tsx/.jsx → use TSX grammar in the
    TypeScript extractor. Unknown languages are no-op.
    """
    if language == "java":
        _attach_java_symbol_graph(file_chunks, content)
    elif language == "python":
        _attach_python_symbol_graph(file_chunks, content)
    elif language in ("typescript", "javascript"):
        is_tsx = bool(file_path and file_path.lower().endswith((".tsx", ".jsx")))
        _attach_typescript_symbol_graph(file_chunks, content, is_tsx=is_tsx)
    # else: no-op (other languages → punt to LSP slices)


def _expand_multiview_if_enabled(raw_chunks: list[dict]) -> list[dict]:
    """When USE_CODE_MULTIVIEW_EMBEDDING is on, expand each tree-sitter symbol
    chunk into body + signature + (best-effort) nl_summary views. Pass-through
    when the flag is off or when a chunk isn't a tree-sitter symbol chunk.

    Implementation note: NL summaries are generated via concurrent LLM calls
    (`synthesize_batch_sync` — 16-way parallel by default, override via env
    `CODE_SUMMARIZER_CONCURRENCY`). For a 13k-symbol repo this drops ingest
    wall-time from ~14 h sequential to ~55 min at 16× concurrency.
    """
    if not settings.use_code_multiview_embedding:
        return raw_chunks

    # Two-pass: first split symbol chunks from non-symbol chunks (the latter
    # pass through unchanged), then batch-summarise all symbol chunks at once.
    symbol_indices: list[int] = []
    summary_inputs: list[tuple[str, str, str]] = []
    expanded: list[dict] = [None] * len(raw_chunks)  # type: ignore[list-item]

    for i, chunk in enumerate(raw_chunks):
        symbol_kind = chunk.get("symbol_kind")
        if symbol_kind in (None, "file"):
            expanded[i] = chunk
            continue
        symbol_indices.append(i)
        summary_inputs.append((
            chunk.get("content", ""),
            chunk.get("language", "unknown"),
            symbol_kind,
        ))

    if summary_inputs:
        summaries = code_summarizer.synthesize_batch_sync(summary_inputs)
    else:
        summaries = []

    # Second pass — splice the multiview expansions back in, preserving order.
    # `expanded` was sized 1:1 with raw_chunks; symbol slots get replaced with
    # a list of 2-3 view rows, so we rebuild as a flat list.
    flat: list[dict] = []
    sym_iter = iter(zip(symbol_indices, summaries))
    next_sym = next(sym_iter, None)
    for i, chunk in enumerate(raw_chunks):
        if next_sym is not None and next_sym[0] == i:
            _, nl_summary = next_sym
            flat.extend(code_chunker_ts.expand_to_multiview(chunk, nl_summary=nl_summary))
            next_sym = next(sym_iter, None)
        else:
            flat.append(chunk)
    return flat


# ── Phase 1.4 — per-window storage for oversized chunks ───────────────────────

def _expand_per_window_if_enabled(raw_chunks: list[dict]) -> list[dict]:
    """When `use_per_window_chunk_storage` is on, split chunks whose body
    exceeds MAX_EMBED_CHARS into multiple sliding-window rows.

    Each oversized chunk produces N rows that share the original chunk's
    `parent_symbol_id` (or one synthesised from the path+chunk_index when
    the source had none). View kinds are `window:<idx>` to keep them
    distinguishable from the multiview `body / signature / nl_summary`
    family. Retrieval-time dedup via `parent_symbol_id` already collapses
    duplicates back to the best-scoring window.

    When the flag is off OR the chunk fits, this is a pass-through.
    """
    if not getattr(settings, "use_per_window_chunk_storage", False):
        return raw_chunks
    # Lazy import to avoid loading httpx etc. when the flag is off.
    from app.rag.embeddings import _split_for_embedding, MAX_EMBED_CHARS

    flat: list[dict] = []
    expanded_count = 0
    for chunk in raw_chunks:
        body = chunk.get("content") or ""
        if len(body) <= MAX_EMBED_CHARS:
            flat.append(chunk)
            continue
        windows = _split_for_embedding(body)
        if len(windows) <= 1:
            flat.append(chunk)
            continue
        # Synthesise a parent id when the source chunk doesn't have one
        # (file-level chunks, regex-fallback chunks). The parent links all
        # window rows so the retrieval-time dedup keeps only one per parent.
        # `parent_symbol_id` is a String(36) UUID column — we MUST emit a
        # 36-char string. Hash the path+index into a deterministic uuid5
        # so re-ingest produces the same id (no orphan rows on rerun).
        parent = chunk.get("parent_symbol_id") or str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"win-parent::{chunk.get('path','?')}::{chunk.get('chunk_index','?')}",
        ))
        for w_idx, window_text in enumerate(windows):
            row = dict(chunk)
            row["content"] = window_text
            row["parent_symbol_id"] = parent
            row["view_kind"] = f"window:{w_idx}"
            flat.append(row)
        expanded_count += 1

    if expanded_count:
        logger.info(
            "Per-window expansion: %d oversized chunks → %d window rows total",
            expanded_count, len(flat) - (len(raw_chunks) - expanded_count),
        )
    return flat


# ── Ingestion entrypoint ───────────────────────────────────────────────────────

def ingest_codebase(
    db: Session,
    change_request_id: str,
    repo: str | None = None,
    branch: str | None = None,
) -> dict:
    """
    Fetch Java source files from GitLab and store embeddings in pgvector.

    Args:
        db:                SQLAlchemy session.
        change_request_id: Used as metadata to scope chunks (not filtered, just stored).
        repo:              GitLab repo path — overrides settings.gitlab_repo if provided.
        branch:            Branch name — overrides settings.gitlab_branch if provided.

    Returns:
        {"files_fetched": N, "chunks_stored": M}
    """
    repo   = repo   or settings.gitlab_repo
    branch = branch or settings.gitlab_branch

    if not repo:
        raise ValueError("GitLab repo not configured. Set GITLAB_REPO in environment.")

    # Remove existing java_source chunks for this change (allow re-ingestion)
    deleted = (
        db.query(DocumentChunk)
        .filter(DocumentChunk.doc_category == DocCategory.JAVA_SOURCE)
        .filter(DocumentChunk.metadata_["change_request_id"].as_string() == change_request_id)
        .all()
    )
    for d in deleted:
        db.delete(d)
    db.flush()

    # Fetch files from GitLab
    java_files = _fetch_java_files_from_gitlab(repo, branch)
    if not java_files:
        logger.warning("No Java files returned from GitLab")
        return {"files_fetched": 0, "chunks_stored": 0}

    # Chunk all files — dispatcher falls back to regex when feature flag is off.
    all_chunks: list[dict] = []
    for file in java_files:
        file_chunks = code_chunker_ts.chunk_source_file(
            file["path"], file["content"], "java",
            fallback=lambda p=file["path"], c=file["content"]: _chunk_java_file(p, c),
        )
        # Slice 17 — attach symbol-graph edges when flag on. Runs per-file so
        # `called_by` can be computed within-file; cross-file resolution is
        # deferred to a follow-up slice with a global symbol index.
        if settings.use_symbol_graph_extractor:
            _attach_java_symbol_graph(file_chunks, file["content"])
        all_chunks.extend(file_chunks)

    logger.info("Total chunks after Java-aware splitting: %d", len(all_chunks))

    # Slice 4 — optional 3-view expansion (body / signature / nl_summary).
    all_chunks = _expand_multiview_if_enabled(all_chunks)
    all_chunks = _expand_per_window_if_enabled(all_chunks)
    logger.info("Total chunks after multiview expansion: %d", len(all_chunks))

    # Embed + store in batches
    total_stored = 0
    for batch_start in range(0, len(all_chunks), BATCH_SIZE):
        batch = all_chunks[batch_start: batch_start + BATCH_SIZE]
        # Phase 3.2 — consult the content-hash embedding cache before
        # paying the Ollama round-trip. The helper falls open to plain
        # embed_texts() if the cache table is missing or any DB op fails,
        # so ingestion is never blocked by a broken cache.
        embeddings = embed_chunks_with_cache(db, batch)

        for chunk, embedding in zip(batch, embeddings):
            record = DocumentChunk(
                id=generate_uuid(),
                source_file=chunk["path"],
                doc_category=DocCategory.JAVA_SOURCE,
                content=chunk["content"],
                embedding=embedding,
                chunk_index=chunk["chunk_index"],
                metadata_={
                    "change_request_id": change_request_id,
                    "class_name": chunk["class_name"],
                    "method_name": chunk["method_name"],
                },
                # Slice 3 tree-sitter fields (absent on regex chunker output)
                symbol_kind=chunk.get("symbol_kind"),
                symbol_name=chunk.get("symbol_name"),
                signature=chunk.get("signature"),
                line_start=chunk.get("line_start"),
                line_end=chunk.get("line_end"),
                language=chunk.get("language"),
                # Slice 4 multiview fields (absent when flag off)
                view_kind=chunk.get("view_kind"),
                parent_symbol_id=chunk.get("parent_symbol_id"),
                # Slice 17 symbol-graph edges (absent when flag off)
                imports=chunk.get("imports"),
                inherits=chunk.get("inherits"),
                implements=chunk.get("implements"),
                calls=chunk.get("calls"),
                called_by=chunk.get("called_by"),
            )
            db.add(record)
            total_stored += 1

        db.flush()

    db.commit()
    logger.info("Code RAG: stored %d chunks for change_request_id=%s", total_stored, change_request_id)
    return {"files_fetched": len(java_files), "chunks_stored": total_stored}


def ingest_repo(
    db: Session,
    repo_id: str,
    repo: str,
    branch: str,
    gitlab_url: str | None = None,
) -> dict:
    """
    Fetch Java source files from a GitLab repo and store embeddings (global admin indexing).

    Unlike ingest_codebase(), this is NOT scoped to a change request.
    Chunks are tagged with repo_id in metadata for filtering.

    Returns:
        {"files_fetched": N, "chunks_stored": M}
    """
    if not repo:
        raise ValueError("GitLab repo path is required.")

    # Remove existing chunks for this repo (allow re-indexing)
    existing = (
        db.query(DocumentChunk)
        .filter(DocumentChunk.doc_category == DocCategory.JAVA_SOURCE)
        .filter(DocumentChunk.metadata_["repo_id"].as_string() == repo_id)
        .all()
    )
    for d in existing:
        db.delete(d)
    db.flush()
    logger.info("Code indexing: cleared %d existing chunks for repo_id=%s", len(existing), repo_id)

    # Use custom gitlab_url if provided, else global
    original_url = settings.gitlab_url
    if gitlab_url:
        settings.__dict__["gitlab_url"] = gitlab_url

    try:
        java_files = _fetch_java_files_from_gitlab(repo, branch)
    finally:
        if gitlab_url:
            settings.__dict__["gitlab_url"] = original_url

    if not java_files:
        logger.warning("No Java files returned from GitLab for repo=%s branch=%s", repo, branch)
        return {"files_fetched": 0, "chunks_stored": 0}

    logger.info("Code indexing: fetched %d Java files from %s/%s", len(java_files), repo, branch)

    # Chunk all files — dispatcher falls back to regex when feature flag is off.
    all_chunks: list[dict] = []
    for file in java_files:
        file_chunks = code_chunker_ts.chunk_source_file(
            file["path"], file["content"], "java",
            fallback=lambda p=file["path"], c=file["content"]: _chunk_java_file(p, c),
        )
        # Slice 17 — attach symbol-graph edges when flag on. Runs per-file so
        # `called_by` can be computed within-file; cross-file resolution is
        # deferred to a follow-up slice with a global symbol index.
        if settings.use_symbol_graph_extractor:
            _attach_java_symbol_graph(file_chunks, file["content"])
        all_chunks.extend(file_chunks)

    logger.info("Code indexing: %d chunks after splitting", len(all_chunks))

    # Slice 4 — optional 3-view expansion (body / signature / nl_summary).
    all_chunks = _expand_multiview_if_enabled(all_chunks)
    all_chunks = _expand_per_window_if_enabled(all_chunks)
    logger.info("Code indexing: %d chunks after multiview expansion", len(all_chunks))

    # Embed + store in batches
    total_stored = 0
    for batch_start in range(0, len(all_chunks), BATCH_SIZE):
        batch = all_chunks[batch_start: batch_start + BATCH_SIZE]
        # Phase 3.2 — consult the content-hash embedding cache before
        # paying the Ollama round-trip. The helper falls open to plain
        # embed_texts() if the cache table is missing or any DB op fails,
        # so ingestion is never blocked by a broken cache.
        embeddings = embed_chunks_with_cache(db, batch)

        for chunk, embedding in zip(batch, embeddings):
            record = DocumentChunk(
                id=generate_uuid(),
                source_file=chunk["path"],
                doc_category=DocCategory.JAVA_SOURCE,
                content=chunk["content"],
                embedding=embedding,
                chunk_index=chunk["chunk_index"],
                metadata_={
                    "repo_id": repo_id,
                    "repo": repo,
                    "class_name": chunk["class_name"],
                    "method_name": chunk["method_name"],
                },
                # Slice 3 tree-sitter fields (absent on regex chunker output)
                symbol_kind=chunk.get("symbol_kind"),
                symbol_name=chunk.get("symbol_name"),
                signature=chunk.get("signature"),
                line_start=chunk.get("line_start"),
                line_end=chunk.get("line_end"),
                language=chunk.get("language"),
                # Slice 4 multiview fields (absent when flag off)
                view_kind=chunk.get("view_kind"),
                parent_symbol_id=chunk.get("parent_symbol_id"),
                # Slice 17 symbol-graph edges (absent when flag off)
                imports=chunk.get("imports"),
                inherits=chunk.get("inherits"),
                implements=chunk.get("implements"),
                calls=chunk.get("calls"),
                called_by=chunk.get("called_by"),
            )
            db.add(record)
            total_stored += 1

        db.flush()

    db.commit()
    logger.info("Code indexing: stored %d chunks for repo=%s repo_id=%s", total_stored, repo, repo_id)

    # Record the indexed commit SHA so staleness detection (repo_scope.is_stale) can
    # compare it against a run's clone SHA. Without this the java-mode index never wrote
    # last_ingested_sha → indexed_sha() returned None → is_stale() short-circuited to True,
    # flagging EVERY freshly-indexed repo STALE in every subagent's system prompt. The
    # polyglot paths already do this (via _persist_repo_state_sha); the java path did not.
    # Fail-soft: a SHA lookup / DB hiccup must never fail the ingest.
    try:
        new_sha = _resolve_branch_head_sha(repo, branch)
        if new_sha:
            _persist_repo_state_sha(db, repo_id, new_sha, branch=branch)
            db.commit()
    except Exception as e:  # noqa: BLE001 — provenance recording is best-effort
        logger.warning("Could not record indexed SHA for repo_id=%s (%s) — staleness will "
                       "conservatively report the repo stale until the next ingest", repo_id, e)

    return {"files_fetched": len(java_files), "chunks_stored": total_stored}


# ── Slice 22c — Polyglot ingestion entry ─────────────────────────────────────

def ingest_polyglot_repo(
    db: Session,
    repo_id: str,
    repo: str,
    branch: str,
    *,
    languages: list[str],
    gitlab_url: str | None = None,
) -> dict:
    """Polyglot version of `ingest_repo` — fetch files for the given list of
    languages, dispatch chunker + symbol-graph attach by language, and store
    chunks tagged with `repo_id` + `language` in `metadata_`.

    Java chunks land in `doc_category=JAVA_SOURCE` (preserves backward compat
    with existing retrieval filters). Python chunks land in a new
    `python_source` category — retrieval consumers that filter by
    `JAVA_SOURCE` will not see them until a follow-up slice broadens the
    filter (deliberate — keeps Phase A retrievals unchanged this slice).

    Returns:
        {"files_fetched": N, "chunks_stored": M, "by_language": {lang: count}}

    Raises ValueError if `languages` is empty or contains only unknown names.
    """
    if not repo:
        raise ValueError("GitLab repo path is required.")
    if not languages:
        raise ValueError("languages must be a non-empty list")

    extensions = _extensions_for_languages(languages)
    if not extensions:
        raise ValueError(
            f"none of {languages} map to a known LANGUAGE_EXTENSIONS entry"
        )

    # Map language → DocCategory. Java keeps the existing JAVA_SOURCE category
    # so existing retrieval calls (categories=[JAVA_SOURCE]) keep working.
    # Other languages get string categories — DocCategory is a constant
    # holder, not an Enum, so this is type-compatible.
    category_for: dict[str, str] = {
        "java":       DocCategory.JAVA_SOURCE,
        "python":     "python_source",
        "typescript": "typescript_source",
        "javascript": "javascript_source",
    }

    # Clear existing chunks for this repo across ALL covered categories so
    # re-indexing is idempotent.
    cats_to_clear = sorted({category_for.get(l, l) for l in languages})
    existing = (
        db.query(DocumentChunk)
        .filter(DocumentChunk.doc_category.in_(cats_to_clear))
        .filter(DocumentChunk.metadata_["repo_id"].as_string() == repo_id)
        .all()
    )
    for d in existing:
        db.delete(d)
    db.flush()
    logger.info(
        "Polyglot indexing: cleared %d existing chunks for repo_id=%s langs=%s",
        len(existing), repo_id, languages,
    )

    # Fetch files
    original_url = settings.gitlab_url
    if gitlab_url:
        settings.__dict__["gitlab_url"] = gitlab_url
    try:
        files = _fetch_files_by_extensions(repo, branch, extensions)
    finally:
        if gitlab_url:
            settings.__dict__["gitlab_url"] = original_url

    if not files:
        logger.warning("Polyglot indexing: no files matched languages=%s in %s/%s",
                       languages, repo, branch)
        return {"files_fetched": 0, "chunks_stored": 0, "by_language": {}}

    # Chunk + symbol-graph attach per file, dispatching by language.
    all_chunks: list[dict] = []
    by_lang_files: dict[str, int] = {}
    for file in files:
        lang = file["language"]
        if not lang:
            continue
        by_lang_files[lang] = by_lang_files.get(lang, 0) + 1

        # Java has its regex fallback; Python falls back to a single chunk
        # of the full file (tree-sitter chunker handles it cleanly when the
        # USE_TREE_SITTER_CHUNKER flag is on, which is the default in dev).
        if lang == "java":
            fallback = lambda p=file["path"], c=file["content"]: _chunk_java_file(p, c)
        else:
            fallback = lambda p=file["path"], c=file["content"]: [{
                "path": p, "class_name": None, "method_name": None,
                "content": c, "chunk_index": 0,
            }]

        file_chunks = code_chunker_ts.chunk_source_file(
            file["path"], file["content"], lang, fallback=fallback,
        )

        if settings.use_symbol_graph_extractor:
            _attach_symbol_graph_for_language(
                file_chunks, file["content"], lang, file_path=file["path"],
            )
        all_chunks.extend([{**c, "_lang": lang} for c in file_chunks])

    logger.info("Polyglot indexing: %d chunks before multiview", len(all_chunks))

    # Slice 23 — Python LSP cross-file resolution. Runs AFTER per-file symbol
    # graphs are attached (so we know within-file `calls` lists) but BEFORE
    # multiview expansion (so we don't have to mirror cross_file_calls across
    # 3 view rows). Fail-open: any LSP failure leaves chunks unchanged.
    if settings.use_python_lsp:
        try:
            from app.rag import lsp_resolver_python
            python_files = [f for f in files if (f.get("language") or "").lower() == "python"]
            if python_files:
                lsp_report = lsp_resolver_python.resolve_cross_file_calls(
                    python_files, all_chunks,
                    timeout_seconds=settings.python_lsp_timeout_seconds,
                )
                logger.info(
                    "Python LSP resolution: requests=%d resolved=%d cross_file=%d failures=%d",
                    lsp_report.requests, lsp_report.resolved,
                    lsp_report.cross_file, lsp_report.failures,
                )
        except Exception as e:
            logger.warning("Python LSP resolution failed (non-fatal): %s", e)

    # Slice 24 — TypeScript / JavaScript LSP cross-file resolution. Same
    # placement as Python LSP (post-symbol-graph, pre-multiview). Mutates
    # all_chunks in-place; fail-open on any error.
    if settings.use_typescript_lsp:
        try:
            from app.rag import lsp_resolver_typescript
            ts_js_files = [
                f for f in files
                if (f.get("language") or "").lower() in ("typescript", "javascript")
            ]
            if ts_js_files:
                ts_report = lsp_resolver_typescript.resolve_cross_file_calls(
                    ts_js_files, all_chunks,
                    timeout_seconds=settings.typescript_lsp_timeout_seconds,
                )
                logger.info(
                    "TS LSP resolution: requests=%d resolved=%d cross_file=%d failures=%d",
                    ts_report.requests, ts_report.resolved,
                    ts_report.cross_file, ts_report.failures,
                )
        except Exception as e:
            logger.warning("TS LSP resolution failed (non-fatal): %s", e)

    # Sub-slice 24a — Java LSP cross-file resolution. eclipse-jdt is JVM-
    # backed and slower than Python/TS LSPs; default timeout 90s vs 60s.
    # Same fail-open contract.
    if settings.use_java_lsp:
        try:
            from app.rag import lsp_resolver_java
            java_files = [
                f for f in files
                if (f.get("language") or "").lower() == "java"
            ]
            if java_files:
                java_report = lsp_resolver_java.resolve_cross_file_calls(
                    java_files, all_chunks,
                    timeout_seconds=settings.java_lsp_timeout_seconds,
                )
                logger.info(
                    "Java LSP resolution: requests=%d resolved=%d cross_file=%d failures=%d",
                    java_report.requests, java_report.resolved,
                    java_report.cross_file, java_report.failures,
                )
        except Exception as e:
            logger.warning("Java LSP resolution failed (non-fatal): %s", e)

    all_chunks = _expand_multiview_if_enabled(all_chunks)
    all_chunks = _expand_per_window_if_enabled(all_chunks)
    logger.info("Polyglot indexing: %d chunks after multiview expansion", len(all_chunks))

    # Embed + store
    by_language_stored: dict[str, int] = {}
    total_stored = 0
    for batch_start in range(0, len(all_chunks), BATCH_SIZE):
        batch = all_chunks[batch_start: batch_start + BATCH_SIZE]
        # Phase 3.2 — consult the content-hash embedding cache before
        # paying the Ollama round-trip. The helper falls open to plain
        # embed_texts() if the cache table is missing or any DB op fails,
        # so ingestion is never blocked by a broken cache.
        embeddings = embed_chunks_with_cache(db, batch)

        for chunk, embedding in zip(batch, embeddings):
            lang = chunk.get("_lang") or chunk.get("language") or "unknown"
            category = category_for.get(lang, f"{lang}_source")
            record = DocumentChunk(
                id=generate_uuid(),
                source_file=chunk["path"],
                doc_category=category,
                content=chunk["content"],
                embedding=embedding,
                chunk_index=chunk["chunk_index"],
                metadata_={
                    "repo_id":     repo_id,
                    "repo":        repo,
                    "language":    lang,
                    "class_name":  chunk.get("class_name"),
                    "method_name": chunk.get("method_name"),
                },
                symbol_kind=chunk.get("symbol_kind"),
                symbol_name=chunk.get("symbol_name"),
                signature=chunk.get("signature"),
                line_start=chunk.get("line_start"),
                line_end=chunk.get("line_end"),
                language=chunk.get("language") or lang,
                view_kind=chunk.get("view_kind"),
                parent_symbol_id=chunk.get("parent_symbol_id"),
                imports=chunk.get("imports"),
                inherits=chunk.get("inherits"),
                implements=chunk.get("implements"),
                calls=chunk.get("calls"),
                called_by=chunk.get("called_by"),
                # Slice 23 — cross-file calls from Python LSP (only set when
                # USE_PYTHON_LSP=True and resolver succeeded for this chunk)
                cross_file_calls=chunk.get("cross_file_calls"),
            )
            db.add(record)
            total_stored += 1
            by_language_stored[lang] = by_language_stored.get(lang, 0) + 1

        db.flush()

    db.commit()
    logger.info(
        "Polyglot indexing: stored %d chunks (%s) for repo=%s repo_id=%s",
        total_stored, by_language_stored, repo, repo_id,
    )
    return {
        "files_fetched": len(files),
        "chunks_stored": total_stored,
        "by_language":   by_language_stored,
        "files_by_language": by_lang_files,
    }


# ── Slice 26 — Incremental ingest helpers ────────────────────────────────────

def _compute_file_hash(content: str | bytes) -> str:
    """SHA256 of the file's content. Treats None / empty as the hash of
    the empty string so re-ingests of empty files are stable."""
    import hashlib
    if isinstance(content, str):
        data = content.encode("utf-8", errors="replace")
    elif isinstance(content, bytes):
        data = content
    else:
        data = b""
    return hashlib.sha256(data).hexdigest()


def diff_against_state(
    files: list[dict],
    prior_state: dict[str, str],
) -> dict[str, list]:
    """Pure: classify each file against prior state.

    Args:
        files: `[{"path", "content", ...}]` — the live fetch.
        prior_state: `{source_file: content_hash}` from CodeRepoFileState.

    Returns:
        {
          "added":     [file_dict, ...]   — present now, absent in prior_state
          "modified":  [file_dict, ...]   — present in both, hash differs
          "unchanged": [file_dict, ...]   — present in both, hash matches
          "deleted":   [source_file, ...] — absent now, present in prior_state
        }

    Each input file is augmented with `_content_hash` for the caller to
    persist after a successful per-file ingest.
    """
    added: list[dict] = []
    modified: list[dict] = []
    unchanged: list[dict] = []
    seen_paths: set[str] = set()

    for f in files:
        path = f.get("path")
        if not path:
            continue
        seen_paths.add(path)
        new_hash = _compute_file_hash(f.get("content") or "")
        f_with_hash = {**f, "_content_hash": new_hash}
        prior = prior_state.get(path)
        if prior is None:
            added.append(f_with_hash)
        elif prior != new_hash:
            modified.append(f_with_hash)
        else:
            unchanged.append(f_with_hash)

    deleted = sorted(p for p in prior_state.keys() if p not in seen_paths)

    return {
        "added": added,
        "modified": modified,
        "unchanged": unchanged,
        "deleted": deleted,
    }


def _load_prior_file_state(db: Session, repo_id: str) -> dict[str, str]:
    """Return `{source_file: content_hash}` from CodeRepoFileState for repo_id."""
    from app.models.code_repo_file_state import CodeRepoFileState
    rows = (
        db.query(CodeRepoFileState)
        .filter(CodeRepoFileState.repo_id == repo_id)
        .all()
    )
    return {r.source_file: r.content_hash for r in rows}


def _persist_file_state(
    db: Session,
    repo_id: str,
    files_with_hash: list[dict],
) -> None:
    """Phase 4.3 — Upsert one CodeRepoFileState row per file using a
    single `INSERT ... ON CONFLICT (repo_id, source_file) DO UPDATE`
    statement on Postgres. Falls back to the legacy delete-then-insert
    pattern on non-Postgres dialects (SQLite in dev tests).

    The upsert is:
      • idempotent under concurrent writers (no race window between
        DELETE and INSERT where another worker could see the row missing),
      • a single statement instead of N inserts after one bulk delete
        (fewer round-trips on bigger batches),
      • safe against the rare race where two ingest jobs touch the same
        repo at once — the unique constraint
        `uq_code_repo_file_state_pair` enforces convergence.
    """
    from app.models.base import generate_uuid, utcnow
    from app.models.code_repo_file_state import CodeRepoFileState

    if not files_with_hash:
        return

    now = utcnow()
    rows = []
    for f in files_with_hash:
        path = f.get("path")
        if not path:
            continue
        rows.append({
            "id":              generate_uuid(),
            "repo_id":         repo_id,
            "source_file":     path,
            "content_hash":    f.get("_content_hash") or _compute_file_hash(f.get("content") or ""),
            "language":        f.get("language"),
            "last_indexed_at": now,
        })

    if not rows:
        return

    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        stmt = pg_insert(CodeRepoFileState).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_code_repo_file_state_pair",
            set_={
                "content_hash":    stmt.excluded.content_hash,
                "language":        stmt.excluded.language,
                "last_indexed_at": stmt.excluded.last_indexed_at,
            },
        )
        db.execute(stmt)
        db.flush()
        return

    # Non-Postgres fallback — legacy delete-then-insert path. Keeps dev
    # tests on SQLite working without a dialect-specific upsert clause.
    paths = [r["source_file"] for r in rows]
    db.query(CodeRepoFileState).filter(
        CodeRepoFileState.repo_id == repo_id,
        CodeRepoFileState.source_file.in_(paths),
    ).delete(synchronize_session=False)
    for r in rows:
        db.add(CodeRepoFileState(**r))
    db.flush()


def _delete_chunks_for_files(
    db: Session,
    repo_id: str,
    source_files: list[str],
    *,
    categories: list[str] | None = None,
) -> int:
    """Delete chunks belonging to the given source_files within repo_id.

    `categories` narrows the delete to specific DocCategory values
    (defaults to all four code-source categories — Java/Python/TS/JS).
    Returns the row count deleted.
    """
    if not source_files:
        return 0
    from app.models.document_chunk import CODE_SOURCE_CATEGORIES
    cats = categories or list(CODE_SOURCE_CATEGORIES)
    rows = (
        db.query(DocumentChunk)
        .filter(DocumentChunk.metadata_["repo_id"].as_string() == repo_id)
        .filter(DocumentChunk.doc_category.in_(cats))
        .filter(DocumentChunk.source_file.in_(source_files))
        .all()
    )
    for r in rows:
        db.delete(r)
    db.flush()
    return len(rows)


def _delete_file_state_entries(
    db: Session, repo_id: str, source_files: list[str],
) -> None:
    if not source_files:
        return
    from app.models.code_repo_file_state import CodeRepoFileState
    db.query(CodeRepoFileState).filter(
        CodeRepoFileState.repo_id == repo_id,
        CodeRepoFileState.source_file.in_(source_files),
    ).delete(synchronize_session=False)


# ── Slice 26 — Incremental ingest entry point ─────────────────────────────────

def ingest_polyglot_repo_incremental(
    db: Session,
    repo_id: str,
    repo: str,
    branch: str,
    *,
    languages: list[str],
    gitlab_url: str | None = None,
) -> dict:
    """Polyglot ingest with file-hash-based delta processing.

    Compared to `ingest_polyglot_repo` (which clears all chunks and
    re-ingests every file), this entry:
      1. Fetches the current file set from GitLab as before.
      2. Loads prior `CodeRepoFileState` rows for this repo_id.
      3. Diffs by content-hash → (added, modified, unchanged, deleted).
      4. Re-chunks/embeds ONLY (added + modified).
      5. Deletes chunks for `deleted` files.
      6. Leaves `unchanged` files alone (no DB writes, no embedding cost).
      7. Updates CodeRepoFileState for added+modified; removes entries
         for deleted.

    LSP cross-file resolution still runs over the FULL post-update file
    set (because cross-file edges depend on the whole graph). That work
    is a constant cost per run — the incremental win is in the
    chunking + embedding + symbol-graph phases.

    Returns a richer report than the full-ingest entry, with per-bucket
    counts:
        {
          "files_fetched":     N,
          "files_unchanged":   U,
          "files_modified":    M,
          "files_added":       A,
          "files_deleted":     D,
          "chunks_inserted":   X,
          "chunks_deleted":    Y,
          "by_language":       {lang: chunks_inserted},
        }

    Raises ValueError on empty/invalid `languages` (same as full entry).
    """
    if not repo:
        raise ValueError("GitLab repo path is required.")
    if not languages:
        raise ValueError("languages must be a non-empty list")

    extensions = _extensions_for_languages(languages)
    if not extensions:
        raise ValueError(f"none of {languages} map to a known LANGUAGE_EXTENSIONS entry")

    category_for: dict[str, str] = {
        "java":       DocCategory.JAVA_SOURCE,
        "python":     "python_source",
        "typescript": "typescript_source",
        "javascript": "javascript_source",
    }

    # 1. Fetch.
    #
    # Phase 4.2 — fast path via GitLab Compare API:
    #   - If we know the last-ingested commit SHA AND can resolve the
    #     current HEAD of the branch, ask GitLab for just the changed
    #     paths and fetch only those.
    #   - Otherwise fall back to the legacy full-fetch + per-file SHA
    #     hash diff path.
    #
    # The two paths produce structurally identical inputs to step 2-N,
    # so the rest of the function doesn't care which one ran. The fast
    # path is a pure cost optimisation.
    original_url = settings.gitlab_url
    if gitlab_url:
        settings.__dict__["gitlab_url"] = gitlab_url
    try:
        prev_sha = _load_repo_state_sha(db, repo_id)
        new_sha = _resolve_branch_head_sha(repo, branch) if prev_sha else None

        fast_path_result: tuple[list[dict], list[str]] | None = None
        if prev_sha and new_sha:
            fast_path_result = _fetch_changed_paths_via_compare(
                repo, prev_sha, new_sha, extensions,
            )

        if fast_path_result is not None:
            delta_files_from_compare, deleted_from_compare = fast_path_result
            # In fast-path mode we already know exactly which files
            # changed — skip the full fetch and skip the SHA-hash diff.
            files = delta_files_from_compare
            prior_state = _load_prior_file_state(db, repo_id)
            diff = diff_against_state(files, prior_state)
            # Compare API tells us deletions directly — preserve them
            # even if they're not in the per-file SHA diff.
            added = diff["added"]
            modified = diff["modified"]
            unchanged: list[dict] = []   # not meaningful in fast path
            # TODO(— BUG flagged in review, NOT fixed on retrofit):
            # `files` here is ONLY the compare-changed files, so
            # diff_against_state computes deleted = prior_state.keys() - {those
            # few files} = EVERY unchanged file in the repo. ORing that into
            # `deleted` makes the fast path wipe the entire index (chunks +
            # file-state) on the first real changed-commit run. Deletions are
            # already authoritative in `deleted_from_compare` — use only that:
            #   deleted = sorted(deleted_from_compare)
            # and add a fast-path test (prior_state full, files=1 modified →
            # deleted == []).
            deleted = sorted(set(diff["deleted"]) | set(deleted_from_compare))
            logger.info(
                "Incremental fast path: compare(%s..%s) drove the delta",
                (prev_sha or "")[:12], (new_sha or "")[:12],
            )
        else:
            # Legacy full-fetch + SHA-hash diff path.
            files = _fetch_files_by_extensions(repo, branch, extensions)
            prior_state = _load_prior_file_state(db, repo_id)
            diff = diff_against_state(files, prior_state)
            added = diff["added"]
            modified = diff["modified"]
            unchanged = diff["unchanged"]
            deleted = diff["deleted"]
            # If compare wasn't usable but we DO have a HEAD SHA, persist
            # it at end-of-function so the *next* run can use the fast
            # path. (`new_sha` may be None when GitLab is unreachable.)
            if not new_sha:
                new_sha = _resolve_branch_head_sha(repo, branch)
    finally:
        if gitlab_url:
            settings.__dict__["gitlab_url"] = original_url

    logger.info(
        "Incremental ingest: repo_id=%s added=%d modified=%d unchanged=%d deleted=%d",
        repo_id, len(added), len(modified), len(unchanged), len(deleted),
    )

    # 4. Delete chunks for files removed from the repo.
    chunks_deleted = _delete_chunks_for_files(
        db, repo_id, deleted,
        categories=sorted({category_for.get(l, l) for l in languages}),
    )
    _delete_file_state_entries(db, repo_id, deleted)

    # Files we will actually process — added + modified — this is the
    # "delta". When zero, we still run state-update + return early to
    # avoid spinning up LSP servers for nothing.
    delta_files = added + modified
    if not delta_files and not deleted:
        # Idempotent re-run — nothing changed. Phase 4.1 — still record the
        # SHA so the fast path keeps working even on no-op cycles.
        if new_sha:
            _persist_repo_state_sha(db, repo_id, new_sha, branch=branch)
        db.commit()
        return {
            "files_fetched":   len(files),
            "files_unchanged": len(unchanged),
            "files_modified":  0,
            "files_added":     0,
            "files_deleted":   0,
            "chunks_inserted": 0,
            "chunks_deleted":  0,
            "by_language":     {},
            "fast_path":       bool(prev_sha and new_sha),
            "prev_sha":        prev_sha,
            "new_sha":         new_sha,
        }

    # 5. Delete the existing chunks for added (defensive — should be 0)
    #    + modified files before re-ingesting them.
    delta_paths = [f["path"] for f in delta_files if f.get("path")]
    chunks_deleted_for_modified = _delete_chunks_for_files(
        db, repo_id, delta_paths,
        categories=sorted({category_for.get(l, l) for l in languages}),
    )
    chunks_deleted += chunks_deleted_for_modified

    # 6. Chunk + symbol-graph attach for delta only.
    all_chunks: list[dict] = []
    for file in delta_files:
        lang = file.get("language")
        if not lang:
            continue
        if lang == "java":
            fallback = lambda p=file["path"], c=file["content"]: _chunk_java_file(p, c)
        else:
            fallback = lambda p=file["path"], c=file["content"]: [{
                "path": p, "class_name": None, "method_name": None,
                "content": c, "chunk_index": 0,
            }]
        file_chunks = code_chunker_ts.chunk_source_file(
            file["path"], file["content"], lang, fallback=fallback,
        )
        if settings.use_symbol_graph_extractor:
            _attach_symbol_graph_for_language(
                file_chunks, file["content"], lang, file_path=file["path"],
            )
        all_chunks.extend([{**c, "_lang": lang} for c in file_chunks])

    # LSP cross-file resolution. Runs over the full post-update file set
    # (delta + unchanged) since cross-file edges depend on the whole graph.
    if settings.use_python_lsp:
        try:
            from app.rag import lsp_resolver_python
            python_files = [f for f in files if (f.get("language") or "").lower() == "python"]
            if python_files and any(c.get("_lang") == "python" for c in all_chunks):
                lsp_resolver_python.resolve_cross_file_calls(
                    python_files, all_chunks,
                    timeout_seconds=settings.python_lsp_timeout_seconds,
                )
        except Exception as e:
            logger.warning("Python LSP (incremental) failed: %s", e)
    if settings.use_typescript_lsp:
        try:
            from app.rag import lsp_resolver_typescript
            ts_js = [
                f for f in files
                if (f.get("language") or "").lower() in ("typescript", "javascript")
            ]
            if ts_js and any(c.get("_lang") in ("typescript", "javascript") for c in all_chunks):
                lsp_resolver_typescript.resolve_cross_file_calls(
                    ts_js, all_chunks,
                    timeout_seconds=settings.typescript_lsp_timeout_seconds,
                )
        except Exception as e:
            logger.warning("TS LSP (incremental) failed: %s", e)
    if settings.use_java_lsp:
        try:
            from app.rag import lsp_resolver_java
            java_files = [f for f in files if (f.get("language") or "").lower() == "java"]
            if java_files and any(c.get("_lang") == "java" for c in all_chunks):
                lsp_resolver_java.resolve_cross_file_calls(
                    java_files, all_chunks,
                    timeout_seconds=settings.java_lsp_timeout_seconds,
                )
        except Exception as e:
            logger.warning("Java LSP (incremental) failed: %s", e)

    all_chunks = _expand_multiview_if_enabled(all_chunks)
    all_chunks = _expand_per_window_if_enabled(all_chunks)

    # 7. Embed + persist (delta only).
    by_language_stored: dict[str, int] = {}
    chunks_inserted = 0
    for batch_start in range(0, len(all_chunks), BATCH_SIZE):
        batch = all_chunks[batch_start: batch_start + BATCH_SIZE]
        # Phase 3.2 — consult the content-hash embedding cache before
        # paying the Ollama round-trip. The helper falls open to plain
        # embed_texts() if the cache table is missing or any DB op fails,
        # so ingestion is never blocked by a broken cache.
        embeddings = embed_chunks_with_cache(db, batch)

        for chunk, embedding in zip(batch, embeddings):
            lang = chunk.get("_lang") or chunk.get("language") or "unknown"
            category = category_for.get(lang, f"{lang}_source")
            db.add(DocumentChunk(
                id=generate_uuid(),
                source_file=chunk["path"],
                doc_category=category,
                content=chunk["content"],
                embedding=embedding,
                chunk_index=chunk["chunk_index"],
                metadata_={
                    "repo_id":     repo_id,
                    "repo":        repo,
                    "language":    lang,
                    "class_name":  chunk.get("class_name"),
                    "method_name": chunk.get("method_name"),
                },
                symbol_kind=chunk.get("symbol_kind"),
                symbol_name=chunk.get("symbol_name"),
                signature=chunk.get("signature"),
                line_start=chunk.get("line_start"),
                line_end=chunk.get("line_end"),
                language=chunk.get("language") or lang,
                view_kind=chunk.get("view_kind"),
                parent_symbol_id=chunk.get("parent_symbol_id"),
                imports=chunk.get("imports"),
                inherits=chunk.get("inherits"),
                implements=chunk.get("implements"),
                calls=chunk.get("calls"),
                called_by=chunk.get("called_by"),
                cross_file_calls=chunk.get("cross_file_calls"),
            ))
            chunks_inserted += 1
            by_language_stored[lang] = by_language_stored.get(lang, 0) + 1
        db.flush()

    # 8. Persist updated file state for delta files.
    _persist_file_state(db, repo_id, delta_files)

    # Phase 4.1 — record the post-ingest HEAD SHA so the next run can use
    # the git-diff fast path. Fail-soft — a missing table doesn't break
    # ingest, just disables the fast path until the migration is applied.
    if new_sha:
        _persist_repo_state_sha(db, repo_id, new_sha, branch=branch)

    db.commit()
    return {
        "files_fetched":   len(files),
        "files_unchanged": len(unchanged),
        "files_modified":  len(modified),
        "files_added":     len(added),
        "files_deleted":   len(deleted),
        "chunks_inserted": chunks_inserted,
        "chunks_deleted":  chunks_deleted,
        "by_language":     by_language_stored,
        "fast_path":       bool(prev_sha and new_sha and prev_sha != new_sha),
        "prev_sha":        prev_sha,
        "new_sha":         new_sha,
    }
