# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Server-enforced repository selection, scoping, and index provenance (§5).

Every agentic run is confined to the **user-selected, indexed** repos. This
module is the single source of those rules so the S13 start endpoint, the S4
clone step, the S6 tools, and the S8 context assembler all enforce them
identically:

* :func:`validate_selection` — the hard gate: only known **and indexed** repos
  pass; anything else is rejected (never silently coerced). (rules 1, 3)
* :func:`chunk_scope_filter` / :func:`assert_repo_selected` — repo-scope every
  index query and every generated/edited file to the selected set. (rules 2, 4)
* :func:`indexed_sha` / :func:`is_stale` / :func:`build_stale_map` — index
  provenance + stale detection, so index-derived results can be downgraded to
  *advisory* when the clone has drifted from what was indexed. (rules 5, 6)

**Provenance reconciliation (see S1):** the plan proposed a new
``code_repos.indexed_commit_sha`` column, but ``code_repo_state.last_ingested_sha``
already records exactly that and is written at ingest
(``code_ingestion._persist_repo_state_sha``). We reuse it rather than add a
duplicate column. Per-chunk commit provenance is intentionally not added —
repo-level SHA is sufficient for the §16 stale-detection acceptance.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.domain.contract import repo_roles_of
from app.core.domain.registry import get_active_pack
from app.models.code_repo import CodeRepo
from app.models.code_repo_state import CodeRepoState


class RepoSelectionError(ValueError):
    """A repo selection violated a hard scoping rule (§5).

    The S13 API layer maps this to HTTP 400 — selections are rejected, never
    silently coerced (e.g. the legacy "assign to the primary repo" fallback is
    gone).
    """


# ── Selection gate (rules 1, 3) ──────────────────────────────────────────────────

def validate_selection(db: Session, repo_ids: list[str]) -> list[CodeRepo]:
    """Resolve selected repo ids to indexed ``CodeRepo`` rows, or reject.

    Rejects an empty selection, an unknown id, or a repo that has not been
    indexed (no ``last_indexed_at`` or zero chunks). De-dups while preserving
    the caller's order (which seeds the clone/verify order until the build
    graph in §20 refines it). Returns the resolved rows.
    """
    if not repo_ids:
        raise RepoSelectionError("No repositories selected.")

    resolved: list[CodeRepo] = []
    seen: set[str] = set()
    for rid in repo_ids:
        if rid in seen:
            continue
        seen.add(rid)
        repo = db.get(CodeRepo, rid)
        if repo is None:
            raise RepoSelectionError(f"Unknown repo_id: {rid}")
        if repo.last_indexed_at is None or (repo.chunks_count or 0) <= 0:
            raise RepoSelectionError(f"Repo not indexed: {rid} ({repo.label})")
        resolved.append(repo)

    roles = repo_roles_of(get_active_pack())
    if not roles:
        return resolved

    declared = {role.key: role for role in roles}
    expected = ", ".join(declared)
    selected_labels = ", ".join(repo.label for repo in resolved)

    for repo in resolved:
        if repo.role is None or repo.role not in declared:
            raise RepoSelectionError(
                f"Repo {repo.label} has undeclared role {repo.role!r}; "
                f"expected role keys: {expected}."
            )

    for role in roles:
        matching = [repo for repo in resolved if repo.role == role.key]
        if role.required and not matching:
            raise RepoSelectionError(
                f"Selected repos ({selected_labels}) are missing required role "
                f"{role.key!r}; expected role keys: {expected}."
            )
        if not role.multiple and len(matching) > 1:
            offending = ", ".join(repo.label for repo in matching)
            raise RepoSelectionError(
                f"Repos {offending} all have role {role.key!r}, which does not allow "
                f"multiple selections; expected role keys: {expected}."
            )
    return resolved


# ── Per-query / per-file scoping (rules 2, 4) ─────────────────────────────────────

def chunk_scope_filter(selected_repo_ids: list[str]):
    """SQLAlchemy filter restricting ``DocumentChunk`` rows to selected repos.

    Matches the existing repo_id idiom (``metadata_['repo_id'].as_string()``)
    used across ingestion/retrieval. Add it to a query:
    ``q.filter(chunk_scope_filter(ids))``.
    """
    from app.models.document_chunk import DocumentChunk  # lazy: pulls pgvector
    return DocumentChunk.metadata_["repo_id"].as_string().in_(list(selected_repo_ids))


def is_repo_selected(repo_id: str | None, selected_repo_ids: list[str]) -> bool:
    return repo_id is not None and repo_id in set(selected_repo_ids)


def assert_repo_selected(repo_id: str | None, selected_repo_ids: list[str]) -> None:
    """Reject a file/operation whose resolved repo is outside the selection.

    The S6 edit/write path and S9 code-change call this after mapping a clone
    path back to its ``repo_id`` (rule 4) — no silent reassignment to a
    "primary" repo.
    """
    if not is_repo_selected(repo_id, selected_repo_ids):
        raise RepoSelectionError(
            f"File maps to repo {repo_id!r}, which is not in the selected set "
            f"{sorted(set(selected_repo_ids))}."
        )


# ── Index provenance + stale detection (rules 5, 6) ───────────────────────────────

def indexed_sha(db: Session, repo_id: str) -> str | None:
    """The commit SHA the repo was last indexed at, or None if never recorded."""
    row = db.get(CodeRepoState, repo_id)
    return row.last_ingested_sha if row else None


def is_stale(db: Session, repo_id: str, base_commit_sha: str | None) -> bool:
    """True when the index may not reflect the cloned working tree (§5).

    Stale (→ index results advisory) when provenance is unknown OR the indexed
    SHA differs from the clone's base SHA. When the base SHA is unknown (no
    clone yet) we cannot assert drift, so we report fresh (False).
    """
    if base_commit_sha is None:
        return False
    recorded = indexed_sha(db, repo_id)
    return recorded is None or recorded != base_commit_sha


def build_stale_map(db: Session, repo_base_sha: dict[str, str]) -> dict[str, bool]:
    """``{repo_id: is_stale}`` for a run's cloned repos — feeds ``ContextPack.stale_index``."""
    return {rid: is_stale(db, rid, sha) for rid, sha in repo_base_sha.items()}
