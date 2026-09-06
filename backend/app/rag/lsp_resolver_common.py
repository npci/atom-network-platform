# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Shared core for the LSP cross-file call resolvers (refactor of
Slices 23 + 24 + 24a).

Three near-identical resolver modules existed before this refactor —
`lsp_resolver_python.py`, `_typescript.py`, `_java.py` — each with its
own copy of:

  - dataclasses (CallSiteRequest / CrossFileCall / ResolutionReport)
  - `is_multilspy_available()` probe
  - `_materialise_repo` (with security guards on absolute / `..` paths)
  - `_find_first_occurrence` (word-boundary regex scan)
  - `_collect_call_sites` (per-language filter)
  - `_ask_lsp_for_definitions` (multilspy session orchestration)
  - `attach_cross_file_calls` (chunk mutator)
  - `resolve_cross_file_calls` (top-level wrapper with nested-event-loop
    fallback + tempdir + fail-open contract)

All of that is now here. Each language-specific module collapses to a
thin facade: a `LspLanguageProfile` describing language-specific
choices (file extensions, accepted `language` strings on chunks,
which `symbol_kind` values produce call sites, post-resolution path
filter for things like Java's `jdt://` URIs, the `code_language`
argument multilspy expects), plus public functions that delegate to
this module's `_resolve_for_profile`.

Public APIs of the 3 language modules are preserved exactly:
`is_multilspy_available()`, dataclass names, `attach_cross_file_calls`,
`resolve_cross_file_calls`. Existing tests run unchanged.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Generic data shapes (the per-language modules expose Type-prefixed aliases
# of these for backward-compat with existing test imports).
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class CallSiteRequest:
    """One call site to resolve via the LSP."""
    caller_chunk_id: str
    file_path: str
    line: int             # 0-indexed
    character: int        # 0-indexed
    callee_symbol: str
    language: str = ""    # populated by _collect_call_sites for TS-vs-JS distinction etc.


@dataclass
class CrossFileCall:
    """One resolved cross-file call edge."""
    caller_chunk_id: str
    callee_symbol:   str
    callee_path:     str
    line:            int | None = None
    language:        str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "callee_symbol": self.callee_symbol,
            "callee_path":   self.callee_path,
            "line":          self.line,
            "language":      self.language,
        }


@dataclass
class ResolutionReport:
    requests:        int = 0
    resolved:        int = 0
    cross_file:      int = 0
    same_file:       int = 0
    failures:        int = 0
    failure_reasons: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Multilspy probe (shared)
# ──────────────────────────────────────────────────────────────────────────────

def is_multilspy_available() -> bool:
    """True iff the `multilspy` package imports cleanly."""
    try:
        import multilspy  # noqa: F401
        return True
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Language profile — declarative spec of per-language choices
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class LspLanguageProfile:
    """Declarative description of a target language for the LSP resolver.

    Attributes
    ----------
    name:
        Display name used in `CrossFileCall.language` defaults and in log
        prefixes (e.g. "Python LSP", "TS LSP", "Java LSP").
    multilspy_code_language:
        The string passed to `MultilspyConfig.from_dict({"code_language": ...})`.
        Routes through the appropriate language server underneath.
    file_extensions:
        Tuple of lowercase extensions (with leading dot) that this resolver
        treats as in-scope. Used by `is_supported_file` and the file filter
        in `resolve_cross_file_calls`.
    chunk_languages:
        Set of `language` strings on chunks that this resolver accepts.
        Most resolvers accept exactly one language; TypeScript accepts both
        "typescript" and "javascript" (same TS server handles both).
    callsite_kinds:
        Set of `symbol_kind` values whose chunks produce call sites. Java
        accepts {method, constructor}; Python/TS accept {method, function}.
    tempdir_prefix:
        Used as `tempfile.mkdtemp(prefix=...)` so multiple resolver runs
        are easy to identify in `/tmp`.
    default_timeout_seconds:
        Default per-call LSP timeout. Java's eclipse-jdt is slow to warm
        up so Java's profile sets 90s; Python/TS use 60s.
    is_callee_dropped:
        Optional callable `(callee_path: str) → bool`. If True, the
        resolution is bucketed as `same_file` (i.e. dropped) rather than
        emitted as a cross-file edge. Java uses this to filter out
        eclipse-jdt's `jdt://contents/...` URIs for JDK / library classes.
    detect_grammar_variant:
        Optional `(file_path: str) → bool` returning True for the "tsx"
        variant when multilspy supports per-file grammar dispatch. Currently
        unused at the resolver layer (multilspy handles dispatch via the
        single typescript server), but kept for future flexibility.
    """
    name: str
    multilspy_code_language: str
    file_extensions: tuple[str, ...]
    chunk_languages: frozenset[str]
    callsite_kinds: frozenset[str]
    tempdir_prefix: str = "lsp_repo_"
    default_timeout_seconds: int = 60
    is_callee_dropped: Callable[[str], bool] | None = None
    detect_grammar_variant: Callable[[str], bool] | None = None


# ──────────────────────────────────────────────────────────────────────────────
# Pure helpers
# ──────────────────────────────────────────────────────────────────────────────

def is_supported_file(profile: LspLanguageProfile, path: str) -> bool:
    """Case-insensitive extension match."""
    if not path:
        return False
    return path.lower().endswith(profile.file_extensions)


def materialise_repo(
    files: Iterable[dict],
    *,
    profile: LspLanguageProfile,
    parent_dir: str | None = None,
) -> str:
    """Write each file to a tempdir at its declared path. Returns the root.

    Security guard: silently drops files with absolute paths or `..`-traversal
    components so the LSP never sees paths outside the tempdir.
    """
    root = tempfile.mkdtemp(prefix=profile.tempdir_prefix, dir=parent_dir)
    for f in files:
        rel = f.get("path") or ""
        if not rel or rel.startswith(("/", "\\")) or ".." in rel.split("/"):
            continue
        dest = Path(root) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f.get("content") or "", encoding="utf-8", errors="replace")
    return root


def find_first_occurrence(
    lines: list[str], needle: str, *,
    line_start: int | None = None,
    line_end: int | None = None,
) -> tuple[int, int] | None:
    """Locate `needle` as a word-boundaried token in the given line range.

    Returns (line_index_0based, char_index_0based) or None. Tree-sitter's
    line ranges are 1-indexed; callers passing them get the conversion
    here.
    """
    if not needle:
        return None
    pattern = re.compile(r"\b" + re.escape(needle) + r"\b")
    start = max(0, (line_start or 1) - 1)
    end = (line_end - 1) if line_end else len(lines) - 1
    end = min(end, len(lines) - 1)
    for li in range(start, end + 1):
        if li < 0 or li >= len(lines):
            continue
        m = pattern.search(lines[li])
        if m:
            return li, m.start()
    return None


def collect_call_sites(
    file_chunks: list[dict],
    file_content_by_path: dict[str, str],
    *,
    profile: LspLanguageProfile,
) -> list[CallSiteRequest]:
    """Walk chunks of the given language, emit one CallSiteRequest per
    within-file `calls` entry. Heuristic location uses the word-boundary
    regex scan."""
    requests: list[CallSiteRequest] = []
    for chunk in file_chunks:
        lang = (chunk.get("language") or "").lower()
        if lang not in profile.chunk_languages:
            continue
        kind = chunk.get("symbol_kind")
        if kind not in profile.callsite_kinds:
            continue
        calls = chunk.get("calls") or []
        if not calls:
            continue
        path = chunk.get("path") or chunk.get("source_file")
        if not path or not is_supported_file(profile, path):
            continue
        line_start = chunk.get("line_start")
        line_end = chunk.get("line_end")
        body_lines = (file_content_by_path.get(path) or "").splitlines()
        for callee_name in calls:
            if not callee_name or not isinstance(callee_name, str):
                continue
            site = find_first_occurrence(
                body_lines, callee_name,
                line_start=line_start, line_end=line_end,
            )
            if site is None:
                continue
            line, char = site
            requests.append(CallSiteRequest(
                caller_chunk_id=chunk.get("id") or chunk.get("chunk_id") or "",
                file_path=path,
                line=line,
                character=char,
                callee_symbol=callee_name,
                language=lang,
            ))
    return requests


def attach_cross_file_calls(
    file_chunks: list[dict],
    by_caller: dict[str, list[CrossFileCall]],
) -> int:
    """Pure mutator. Merges with any existing `cross_file_calls` entries
    on a chunk (e.g. from a prior resolver run on a multi-language file)."""
    by_id: dict[str, dict] = {}
    for c in file_chunks:
        cid = c.get("id") or c.get("chunk_id")
        if cid:
            by_id[cid] = c
    enriched = 0
    for caller_id, cf_calls in by_caller.items():
        chunk = by_id.get(caller_id)
        if chunk is None:
            continue
        existing = chunk.get("cross_file_calls") or []
        new_entries = [c.to_dict() for c in cf_calls]
        chunk["cross_file_calls"] = existing + new_entries
        enriched += 1
    return enriched


# ──────────────────────────────────────────────────────────────────────────────
# Async LSP session
# ──────────────────────────────────────────────────────────────────────────────

async def ask_lsp_for_definitions(
    repo_root: str,
    requests: list[CallSiteRequest],
    *,
    profile: LspLanguageProfile,
    timeout_seconds: int,
    default_callee_language: str | None = None,
) -> tuple[dict[str, list[CrossFileCall]], ResolutionReport]:
    """Spawn a multilspy session for `profile.multilspy_code_language`,
    resolve each request, return (by_caller, report).

    Per-request failures captured in the report; outer LSP failure
    surfaces as a single report.failure entry + empty result map. Never
    raises.
    """
    report = ResolutionReport()

    try:
        from multilspy import LanguageServer
        from multilspy.multilspy_config import MultilspyConfig
        from multilspy.multilspy_logger import MultilspyLogger
    except Exception as e:
        report.failures += 1
        report.failure_reasons.append(f"multilspy import: {e}")
        logger.warning("multilspy unavailable — skipping %s LSP resolution: %s",
                       profile.name, e)
        return {}, report

    config = MultilspyConfig.from_dict({"code_language": profile.multilspy_code_language})
    ml_logger = MultilspyLogger()
    by_caller: dict[str, list[CrossFileCall]] = {}

    try:
        lsp = LanguageServer.create(config, ml_logger, repo_root)
        async with lsp.start_server():
            for req in requests:
                report.requests += 1
                try:
                    defs = await asyncio.wait_for(
                        lsp.request_definition(
                            req.file_path, req.line, req.character,
                        ),
                        timeout=timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    report.failures += 1
                    report.failure_reasons.append(
                        f"timeout {req.file_path}:{req.line}:{req.callee_symbol}"
                    )
                    continue
                except Exception as e:
                    report.failures += 1
                    report.failure_reasons.append(
                        f"definition {req.file_path}:{req.callee_symbol}: {e}"
                    )
                    continue

                if not defs:
                    continue
                report.resolved += 1

                first = defs[0]
                callee_path = (
                    first.get("relativePath")
                    or first.get("absolutePath")
                    or ""
                )
                if callee_path.startswith(repo_root):
                    callee_path = os.path.relpath(callee_path, repo_root)
                if not callee_path:
                    continue
                if callee_path == req.file_path:
                    report.same_file += 1
                    continue
                # Per-language drop filter (e.g. Java's `jdt://...` URIs).
                if profile.is_callee_dropped is not None and profile.is_callee_dropped(callee_path):
                    report.same_file += 1
                    continue

                rng = first.get("range") or {}
                start_line = (rng.get("start") or {}).get("line")
                report.cross_file += 1
                # Prefer the request's resolved language (preserves TS-vs-JS
                # distinction); fall back to the profile-supplied default
                # (e.g. "java") for resolvers that don't track per-request lang.
                lang_for_edge = req.language or default_callee_language or profile.name.lower()
                by_caller.setdefault(req.caller_chunk_id, []).append(
                    CrossFileCall(
                        caller_chunk_id=req.caller_chunk_id,
                        callee_symbol=req.callee_symbol,
                        callee_path=callee_path,
                        line=start_line,
                        language=lang_for_edge,
                    ),
                )
    except Exception as e:
        report.failures += 1
        report.failure_reasons.append(f"lsp session: {e}")
        logger.warning("%s LSP session failure: %s", profile.name, e)
        return {}, report

    return by_caller, report


# ──────────────────────────────────────────────────────────────────────────────
# Top-level orchestrator
# ──────────────────────────────────────────────────────────────────────────────

def resolve_cross_file_calls_for_profile(
    files: list[dict],
    file_chunks: list[dict],
    *,
    profile: LspLanguageProfile,
    timeout_seconds: int | None = None,
    repo_root: str | None = None,
    default_callee_language: str | None = None,
    ask_fn: Callable[..., Any] | None = None,
    is_multilspy_available_fn: Callable[[], bool] | None = None,
) -> ResolutionReport:
    """Top-level orchestrator. Filters `files` to the profile's languages,
    materialises a tempdir if `repo_root` is None, drives multilspy,
    attaches resolved cross-file calls in-place. Never raises.

    Args mirror the per-language `resolve_cross_file_calls` signatures
    that pre-existed in Python/TS/Java modules.
    """
    timeout_seconds = timeout_seconds or profile.default_timeout_seconds
    report = ResolutionReport()

    matching_files = [
        f for f in files
        if (f.get("language") or "").lower() in profile.chunk_languages
        and is_supported_file(profile, f.get("path") or "")
    ]
    if not matching_files:
        return report

    # Use the injected probe so per-facade monkeypatching of
    # `is_multilspy_available` takes effect (tests patch it on the
    # language module, not on `lsp_resolver_common`).
    probe = is_multilspy_available_fn or is_multilspy_available
    if not probe():
        report.failures += 1
        report.failure_reasons.append("multilspy package not importable")
        return report

    file_content_by_path = {f["path"]: f.get("content") or "" for f in matching_files}
    requests = collect_call_sites(file_chunks, file_content_by_path, profile=profile)
    if not requests:
        return report

    own_root = repo_root is None
    root = repo_root or materialise_repo(matching_files, profile=profile)

    # `ask_fn` is the injectable async-coroutine that resolves a list of
    # CallSiteRequests. Per-language facades pass their own
    # `_ask_lsp_for_definitions` wrapper so test monkeypatching of that
    # name on the facade module still routes here. When None, fall back
    # to the shared `ask_lsp_for_definitions` directly.
    async def _default_ask(repo_root_arg, requests_arg, *, timeout_seconds=timeout_seconds):
        return await ask_lsp_for_definitions(
            repo_root_arg, requests_arg,
            profile=profile,
            timeout_seconds=timeout_seconds,
            default_callee_language=default_callee_language,
        )
    effective_ask = ask_fn if ask_fn is not None else _default_ask

    try:
        by_caller: dict[str, list[CrossFileCall]] = {}
        try:
            by_caller, report = asyncio.run(effective_ask(
                root, requests, timeout_seconds=timeout_seconds,
            ))
        except RuntimeError as e:
            msg = str(e)
            if "asyncio.run" in msg or "running event loop" in msg:
                logger.warning("%s LSP nested event loop — running via fresh loop",
                               profile.name)
                import threading
                box: dict = {"by_caller": {}, "report": report}
                def _runner():
                    try:
                        loop = asyncio.new_event_loop()
                        box["by_caller"], box["report"] = loop.run_until_complete(
                            effective_ask(root, requests, timeout_seconds=timeout_seconds),
                        )
                    except Exception as inner:
                        box["report"].failures += 1
                        box["report"].failure_reasons.append(f"thread runner: {inner}")
                t = threading.Thread(target=_runner, daemon=True)
                t.start()
                t.join(timeout=timeout_seconds + 30)
                by_caller = box["by_caller"]
                report = box["report"]
            else:
                logger.warning("%s LSP RuntimeError: %s", profile.name, e)
                report.failures += 1
                report.failure_reasons.append(f"runtime: {e}")
        except Exception as e:
            logger.warning("%s LSP outer failure: %s", profile.name, e)
            report.failures += 1
            report.failure_reasons.append(str(e))

        if by_caller:
            attach_cross_file_calls(file_chunks, by_caller)
    finally:
        if own_root:
            try:
                shutil.rmtree(root, ignore_errors=True)
            except Exception:
                pass

    return report
