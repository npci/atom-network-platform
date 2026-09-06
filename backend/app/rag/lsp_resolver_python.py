# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Python LSP cross-file call resolver (Slice 23 — refactored facade).

Thin facade over `lsp_resolver_common`. Declares the Python language
profile + re-exports the public surface that existing tests +
`code_ingestion.py` already import.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.rag.lsp_resolver_common import (
    CallSiteRequest,
    CrossFileCall as _SharedCrossFileCall,
    LspLanguageProfile,
    ResolutionReport,
    ask_lsp_for_definitions as _shared_ask,
    attach_cross_file_calls,
    collect_call_sites as _shared_collect,
    find_first_occurrence as _find_first_occurrence,
    is_multilspy_available,
    is_supported_file as _is_supported_file,
    materialise_repo as _shared_materialise_repo,
    resolve_cross_file_calls_for_profile,
)


# Slice 23 tests construct `CrossFileCall(...)` without `language=` and
# expect default "python".
@dataclass
class CrossFileCall(_SharedCrossFileCall):
    language: str = "python"


_PYTHON_PROFILE = LspLanguageProfile(
    name="Python",
    multilspy_code_language="python",
    file_extensions=(".py",),
    chunk_languages=frozenset({"python"}),
    callsite_kinds=frozenset({"method", "function"}),
    tempdir_prefix="lsp_repo_",
    default_timeout_seconds=60,
)


# ──────────────────────────────────────────────────────────────────────────────
# Backward-compat helpers (private names tests import directly)
# ──────────────────────────────────────────────────────────────────────────────

def _is_python_file(path: str) -> bool:
    return _is_supported_file(_PYTHON_PROFILE, path)


def _materialise_repo(files, *, parent_dir=None):
    return _shared_materialise_repo(files, profile=_PYTHON_PROFILE, parent_dir=parent_dir)


def _collect_call_sites_from_within_file_graphs(file_chunks, file_content_by_path):
    return _shared_collect(file_chunks, file_content_by_path, profile=_PYTHON_PROFILE)


def _is_cross_file(definition_uri_or_path: str, caller_path: str) -> bool:
    """Compare a callee's resolved path with the caller's. Tolerates
    `file://` URIs and falls back to basename comparison for safety."""
    import os
    callee = (definition_uri_or_path or "").replace("file://", "")
    if not callee:
        return False
    return os.path.basename(callee) != os.path.basename(caller_path) \
        or callee != caller_path


async def _ask_lsp_for_definitions(repo_root, requests, *, timeout_seconds=60):
    """Backward-compat wrapper. Tests monkeypatch this name on the module;
    `resolve_cross_file_calls` injects it into the shared orchestrator
    via `ask_fn=`, so monkeypatched versions take effect."""
    return await _shared_ask(
        repo_root, requests,
        profile=_PYTHON_PROFILE,
        timeout_seconds=timeout_seconds,
        default_callee_language="python",
    )


def _reset_parser_for_tests():
    """No-op — the refactored module has no cached parser. Kept so the
    Slice 23 test that calls this still imports cleanly."""
    pass


# ──────────────────────────────────────────────────────────────────────────────
# Public entry — Slice 23 signature preserved exactly
# ──────────────────────────────────────────────────────────────────────────────

def resolve_cross_file_calls(
    files: list[dict],
    file_chunks: list[dict],
    *,
    timeout_seconds: int = 60,
    repo_root: str | None = None,
) -> ResolutionReport:
    """Drive Python LSP across `files` and attach resolved cross-file
    calls to matching chunks in-place. Never raises."""
    # `is_multilspy_available_fn` deliberately reads the module-level
    # name at call time so test monkeypatches of
    # `lsp_resolver_python.is_multilspy_available` take effect.
    import sys
    _self = sys.modules[__name__]
    return resolve_cross_file_calls_for_profile(
        files, file_chunks,
        profile=_PYTHON_PROFILE,
        timeout_seconds=timeout_seconds,
        repo_root=repo_root,
        default_callee_language="python",
        ask_fn=_ask_lsp_for_definitions,
        is_multilspy_available_fn=lambda: _self.is_multilspy_available(),
    )


__all__ = [
    "CallSiteRequest", "CrossFileCall", "ResolutionReport",
    "is_multilspy_available", "attach_cross_file_calls",
    "resolve_cross_file_calls",
    "_is_python_file", "_materialise_repo", "_find_first_occurrence",
    "_collect_call_sites_from_within_file_graphs",
    "_is_cross_file", "_ask_lsp_for_definitions",
    "_reset_parser_for_tests",
]
