# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Immutable approved ChangeManifest + human approval + push preflight (§11).

Nothing pushes before a human approves an exact ``manifest_hash``. The manifest
is frozen at the ``awaiting_human_approval`` transition; at push time a preflight
recomputes every file's hash from the workspace and re-validates each repo's base
SHA against the approved manifest — reject if ANY repo drifted (all-or-nothing).
"""
from __future__ import annotations

import hashlib
import json
import logging

logger = logging.getLogger(__name__)


def _canonical(obj) -> str:
    # sort_keys recurses (stable dict order at every depth); default=str keeps a
    # stray datetime/Decimal/dataclass in the verification/review summary from
    # crashing the freeze. Callers should still pass JSON-safe, list-order-stable
    # summaries — LIST order is preserved as-is and IS part of the hash.
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def content_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def manifest_hash(manifest: dict) -> str:
    """sha256 over the canonical JSON of everything EXCEPT the hash field itself,
    so the hash is stable and self-consistent."""
    body = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def build_manifest(*, selected_repo_ids: list[str], per_repo: list[dict],
                   change_set, verification: dict, review: dict, plan: str | dict | None = None) -> dict:
    """Freeze a ChangeManifest dict (§11). Deterministic: identical inputs →
    identical ``manifest_hash`` (operations sorted, hashes from content).

    ``plan`` (the human-ratified SPEC) is folded into the hash so a change to the APPROVED
    INTENT forces re-approval even when the resulting diff is byte-identical — closing the gap
    where a re-ratified plan could ride an already-approved hash. Pass the DURABLE ratified plan
    (resume-deterministic); a volatile in-memory plan would change the hash across resumes. Omitted
    when falsy so legacy / plan-less runs keep their existing hashes unchanged."""
    ops = []
    for op in change_set.operations:
        # Always recompute from content (don't trust a possibly-stale FileOp hash)
        # so the manifest hash and push_preflight hash come from the SAME function.
        ch = content_hash(op.content) if op.content is not None else None
        ops.append({"op": op.op, "repo_id": op.repo_id, "path": op.path, "content_hash": ch})
    ops.sort(key=lambda o: (o["repo_id"], o["path"], o["op"]))

    manifest = {
        "selected_repo_ids": sorted(selected_repo_ids),
        "per_repo": sorted(per_repo, key=lambda r: r["repo_id"]),
        "operations": ops,
        "verification": verification,
        "review": review,
    }
    if plan:
        manifest["plan"] = plan
    manifest["manifest_hash"] = manifest_hash(manifest)
    return manifest


def freeze_manifest(db, run_id: str, manifest: dict, diffs: dict | None = None):
    from app.models.agentic import ChangeManifest
    # Idempotent: run_id is unique, so a re-freeze (e.g. an XSD recovery re-run for the
    # same run) must UPDATE the existing row, not INSERT a duplicate — the duplicate
    # crashes on the unique constraint. If the frozen hash changed, invalidate any prior
    # approval so the new manifest must be re-approved (matches build_manifest's intent
    # that a re-ratified plan forces re-approval).
    existing = (db.query(ChangeManifest).filter(ChangeManifest.run_id == run_id)
                .order_by(ChangeManifest.created_at.desc()).first())
    if existing is not None:
        hash_changed = existing.manifest_hash != manifest["manifest_hash"]
        # The state transition that was invisible in prod: a re-freeze that CHANGES the
        # hash makes any prior approval AND any prior push stale. Say so explicitly.
        logger.info(
            "manifest_freeze: run=%s hash %s → %s (%s) ops=%d%s",
            run_id, existing.manifest_hash[:12], manifest["manifest_hash"][:12],
            "CHANGED" if hash_changed else "unchanged", len(manifest["operations"]),
            " — prior approval INVALIDATED, any pushed branch is now STALE"
            if hash_changed and existing.approved_at is not None else "")
        existing.manifest_hash = manifest["manifest_hash"]
        existing.selected_repo_ids = manifest["selected_repo_ids"]
        existing.per_repo = manifest["per_repo"]
        existing.operations = manifest["operations"]
        existing.verification = manifest["verification"]
        existing.review = manifest["review"]
        existing.diffs = diffs or {}
        existing.plan = manifest.get("plan")
        if hash_changed:
            existing.approved_at = None
            existing.approved_by = None
        db.flush()
        return existing
    logger.info("manifest_freeze: run=%s FIRST freeze hash=%s ops=%d repos=%d",
                run_id, manifest["manifest_hash"][:12], len(manifest["operations"]),
                len(manifest.get("per_repo") or []))
    row = ChangeManifest(
        run_id=run_id, manifest_hash=manifest["manifest_hash"],
        selected_repo_ids=manifest["selected_repo_ids"], per_repo=manifest["per_repo"],
        operations=manifest["operations"], verification=manifest["verification"],
        review=manifest["review"], diffs=diffs or {},
        # Persist the ratified plan that was folded into the hash so the stored row is
        # self-consistent (its columns reproduce its manifest_hash) and there's an audit
        # trail of exactly what plan each approval was granted against.
        plan=manifest.get("plan"),
    )
    db.add(row)
    db.flush()
    return row


def approve(db, run_id: str, approved_hash: str, approver: str | None) -> bool:
    """Approve the run's frozen manifest IFF the human approves the exact hash.
    Returns False on a mismatch (tampered/stale hash) — never auto-approves."""
    from app.models.agentic import ChangeManifest
    from app.models.base import utcnow
    row = (db.query(ChangeManifest).filter(ChangeManifest.run_id == run_id)
           .order_by(ChangeManifest.created_at.desc()).first())
    if row is None or row.manifest_hash != approved_hash:
        return False
    row.approved_at = utcnow()
    row.approved_by = approver
    db.flush()
    return True


def push_preflight(manifest: dict, *, current_base_sha: dict[str, str], read_content) -> tuple[bool, list[str]]:
    """Immediately before push (§11): recompute each op's content hash from the
    workspace and re-validate every repo's base SHA. ``read_content(repo_id, path)``
    returns the current workspace content (or None). Returns ``(ok, reasons)`` —
    ANY failure rejects the whole push (base-SHA drift → rebase_reverify upstream)."""
    reasons: list[str] = []
    for pr in manifest.get("per_repo", []):
        rid = pr["repo_id"]
        if current_base_sha.get(rid) != pr.get("base_commit_sha"):
            reasons.append(f"base SHA drifted for repo {rid}")
    for op in manifest.get("operations", []):
        content = read_content(op["repo_id"], op["path"])
        if op["op"] == "delete":
            # An approved deletion must still be absent. If the file was restored in
            # the workspace after approval, the pushed branch would silently keep it.
            if content is not None:
                reasons.append(f"approved deletion was restored: {op['repo_id']}:{op['path']}")
            continue
        if content is None:
            reasons.append(f"missing file at push: {op['repo_id']}:{op['path']}")
        elif content_hash(content) != op["content_hash"]:
            reasons.append(f"content changed since approval: {op['repo_id']}:{op['path']}")
    return (not reasons), reasons
