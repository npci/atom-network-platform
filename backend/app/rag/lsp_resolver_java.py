# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Java LSP cross-file call resolver (Sub-slice 24a — refactored facade).

Thin facade over `lsp_resolver_common`. eclipse-jdt-language-server
underneath via `code_language: "java"` — JVM-backed, slow to warm up,
hence the 90s default timeout.

Java-specific quirk: eclipse-jdt's `request_definition` may return
`jdt://contents/...` URIs for JDK / library classes. Those would be
useless cross-file edges, so the profile's `is_callee_dropped` filter
buckets them as `same_file`.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.rag.lsp_resolver_common import (
    CallSiteRequest as JavaCallSiteRequest,
    CrossFileCall as _SharedCrossFileCall,
    LspLanguageProfile,
    ResolutionReport as JavaResolutionReport,
    ask_lsp_for_definitions as _shared_ask,
    attach_cross_file_calls,
    collect_call_sites as _shared_collect,
    find_first_occurrence as _find_first_occurrence,
    is_multilspy_available,
    is_supported_file as _is_supported_file,
    materialise_repo as _shared_materialise_repo,
    resolve_cross_file_calls_for_profile,
)


# Sub-slice 24a tests expect `JavaCrossFileCall(...)` to default
# `language="java"`.
@dataclass
class JavaCrossFileCall(_SharedCrossFileCall):
    language: str = "java"


def _is_jdt_internal_path(callee_path: str) -> bool:
    """eclipse-jdt represents JDK / library classes as `jdt://...` URIs.
    Treat as same_file (dropped) since they're not project files."""
    if not callee_path:
        return True
    return "://" in callee_path or callee_path.startswith("jdt")


_JAVA_PROFILE = LspLanguageProfile(
    name="Java",
    multilspy_code_language="java",
    file_extensions=(".java",),
    chunk_languages=frozenset({"java"}),
    # Java has no top-level functions; methods + constructors only.
    callsite_kinds=frozenset({"method", "constructor"}),
    tempdir_prefix="lsp_java_repo_",
    default_timeout_seconds=90,
    is_callee_dropped=_is_jdt_internal_path,
)


# ──────────────────────────────────────────────────────────────────────────────
# Backward-compat helpers
# ──────────────────────────────────────────────────────────────────────────────

def _is_java_file(path: str) -> bool:
    return _is_supported_file(_JAVA_PROFILE, path)


def _materialise_repo(files, *, parent_dir=None):
    return _shared_materialise_repo(files, profile=_JAVA_PROFILE, parent_dir=parent_dir)


def _collect_call_sites(file_chunks, file_content_by_path):
    return _shared_collect(file_chunks, file_content_by_path, profile=_JAVA_PROFILE)


async def _ask_lsp_for_definitions(repo_root, requests, *, timeout_seconds=90):
    """Backward-compat wrapper. Default callee language is "java" since
    Java doesn't carry per-request language variants."""
    return await _shared_ask(
        repo_root, requests,
        profile=_JAVA_PROFILE,
        timeout_seconds=timeout_seconds,
        default_callee_language="java",
    )


def _reset_parser_for_tests():
    pass


# ──────────────────────────────────────────────────────────────────────────────
# Public entry — Sub-slice 24a signature preserved
# ──────────────────────────────────────────────────────────────────────────────

def resolve_cross_file_calls(
    files: list[dict],
    file_chunks: list[dict],
    *,
    timeout_seconds: int = 90,
    repo_root: str | None = None,
) -> JavaResolutionReport:
    """Drive Java LSP. Never raises."""
    import sys
    _self = sys.modules[__name__]
    return resolve_cross_file_calls_for_profile(
        files, file_chunks,
        profile=_JAVA_PROFILE,
        timeout_seconds=timeout_seconds,
        repo_root=repo_root,
        default_callee_language="java",
        ask_fn=_ask_lsp_for_definitions,
        is_multilspy_available_fn=lambda: _self.is_multilspy_available(),
    )


__all__ = [
    "JavaCallSiteRequest", "JavaCrossFileCall", "JavaResolutionReport",
    "is_multilspy_available", "attach_cross_file_calls",
    "resolve_cross_file_calls",
    "_is_java_file", "_materialise_repo", "_find_first_occurrence",
    "_collect_call_sites", "_ask_lsp_for_definitions",
    "_reset_parser_for_tests",
]
