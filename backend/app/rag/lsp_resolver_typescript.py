# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""TypeScript / JavaScript LSP cross-file call resolver
(Slice 24 — refactored facade).

Thin facade over `lsp_resolver_common`. TS and JS share the same TS
language server underneath; plain `.js`/`.jsx` files parse cleanly
through it.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.rag.lsp_resolver_common import (
    CallSiteRequest as TsCallSiteRequest,
    CrossFileCall as _SharedCrossFileCall,
    LspLanguageProfile,
    ResolutionReport as TsResolutionReport,
    ask_lsp_for_definitions as _shared_ask,
    attach_cross_file_calls,
    collect_call_sites as _shared_collect,
    find_first_occurrence as _find_first_occurrence,
    is_multilspy_available,
    is_supported_file as _is_supported_file,
    materialise_repo as _shared_materialise_repo,
    resolve_cross_file_calls_for_profile,
)


# Slice 24 tests expect `TsCrossFileCall(...)` to default `language="typescript"`.
@dataclass
class TsCrossFileCall(_SharedCrossFileCall):
    language: str = "typescript"


_TS_PROFILE = LspLanguageProfile(
    name="TypeScript",
    multilspy_code_language="typescript",
    file_extensions=(".ts", ".tsx", ".js", ".jsx"),
    chunk_languages=frozenset({"typescript", "javascript"}),
    callsite_kinds=frozenset({"method", "function"}),
    tempdir_prefix="lsp_ts_repo_",
    default_timeout_seconds=60,
)


# ──────────────────────────────────────────────────────────────────────────────
# Backward-compat helpers
# ──────────────────────────────────────────────────────────────────────────────

def _is_ts_or_js_file(path: str) -> bool:
    return _is_supported_file(_TS_PROFILE, path)


def _materialise_repo(files, *, parent_dir=None):
    return _shared_materialise_repo(files, profile=_TS_PROFILE, parent_dir=parent_dir)


def _collect_call_sites(file_chunks, file_content_by_path):
    return _shared_collect(file_chunks, file_content_by_path, profile=_TS_PROFILE)


async def _ask_lsp_for_definitions(repo_root, requests, *, timeout_seconds=60):
    """Backward-compat wrapper. TS preserves per-request language
    (TS-vs-JS) via CallSiteRequest.language, so no override needed here."""
    return await _shared_ask(
        repo_root, requests,
        profile=_TS_PROFILE,
        timeout_seconds=timeout_seconds,
    )


def _reset_parser_for_tests():
    pass


# ──────────────────────────────────────────────────────────────────────────────
# Public entry
# ──────────────────────────────────────────────────────────────────────────────

def resolve_cross_file_calls(
    files: list[dict],
    file_chunks: list[dict],
    *,
    timeout_seconds: int = 60,
    repo_root: str | None = None,
) -> TsResolutionReport:
    """Drive TS/JS LSP. Never raises."""
    import sys
    _self = sys.modules[__name__]
    return resolve_cross_file_calls_for_profile(
        files, file_chunks,
        profile=_TS_PROFILE,
        timeout_seconds=timeout_seconds,
        repo_root=repo_root,
        ask_fn=_ask_lsp_for_definitions,
        is_multilspy_available_fn=lambda: _self.is_multilspy_available(),
    )


__all__ = [
    "TsCallSiteRequest", "TsCrossFileCall", "TsResolutionReport",
    "is_multilspy_available", "attach_cross_file_calls",
    "resolve_cross_file_calls",
    "_is_ts_or_js_file", "_materialise_repo", "_find_first_occurrence",
    "_collect_call_sites", "_ask_lsp_for_definitions",
    "_reset_parser_for_tests",
]
