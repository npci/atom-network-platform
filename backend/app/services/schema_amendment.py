# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Schema-amendment gate (fix 2).

Phase A authors the .xsd/.xjb and a human approves them. Phase B (code) then works
against that frozen baseline and its schema writes do not land. That rule is sound —
a code run once quietly rewrote a schema it was only meant to consume — but as an
UNCONDITIONAL refusal it created a deadlock with no exit:

    Phase A wrote  <xs:enumeration value="BG"/>  for a new purpose code.
    "BG" was already taken (CommonConstant.TRANSIT_UTP_PURPOSE_CODE), so every
    transaction carrying it would be misclassified by the settlement pipeline.
    The reviewer correctly demanded BG → GP. The code agent could not make that
    edit. The reviewer re-issued the same demand every round; the agent escalated
    to a human seven times over seventeen hours and never shipped.

The finding was right and the remedy was impossible, so the loop could not converge.
This module supplies the missing third option: the code agent's schema write is
captured verbatim as a PROPOSAL, the run parks, and a human decides.

The gate deliberately shows two things the agent cannot be trusted to characterise:

* the exact before/after text, so approval replays byte-for-byte rather than
  re-asking a model to redo the edit; and
* whether the line being changed was introduced by Phase A **in this same change**
  or is pre-existing production baseline. Those are very different decisions —
  fixing your own hour-old mistake versus altering a contract other systems already
  speak — and only a diff against the pinned base SHA can tell them apart.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_CONTEXT_LINES = 6


def _base_content(run_id: str, repo_id: str, base_sha: Optional[str], path: str) -> Optional[str]:
    """File content at the run's pinned base SHA, or None when it cannot be read."""
    if not base_sha:
        return None
    try:
        from app.agents import workspace_local
        from app.agents.platform_adapter import adapter
        res = adapter.run_command(workspace_local.repo_dir(run_id, repo_id),
                                  ["git", "show", f"{base_sha}:{path}"])
        return res.stdout if res.ok else None
    except Exception as e:  # noqa: BLE001 — provenance is advisory; never break the gate
        logger.warning("schema-amendment: base read failed for %s:%s: %s", repo_id, path, e)
        return None


def _contained_target(run_id: str, repo_id: str, path: str):
    """Join `path` onto the repo dir and refuse anything that escapes it.

    The staged amendment list is LLM-authored and survives a database round-trip
    and a human approval click before it reaches a filesystem write, so the path
    must be re-validated HERE rather than trusted from the tool layer. The
    approval UI renders a file card, not a normalised path, so an operator
    approving "amend the schema" cannot see a `../` traversal in what they are
    agreeing to.

    Raises ValueError; callers report it as a failed amendment.
    """
    from app.agents import workspace_local
    root = workspace_local.repo_dir(run_id, repo_id).resolve()
    target = (root / path).resolve()
    if root != target and root not in target.parents:
        raise ValueError(f"path escapes the repository: {path!r}")
    return target


def _current_content(run_id: str, repo_id: str, path: str) -> Optional[str]:
    try:
        p = _contained_target(run_id, repo_id, path)
        return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
    except Exception as e:  # noqa: BLE001
        logger.warning("schema-amendment: current read failed for %s:%s: %s", repo_id, path, e)
        return None


def _hunk(content: str, needle: str) -> dict:
    """``{line, before}`` locating `needle` in `content` with a few lines of context."""
    if not content or not needle:
        return {"line": None, "before": ""}
    pos = content.find(needle)
    if pos < 0:
        return {"line": None, "before": ""}
    start_line = content.count("\n", 0, pos)
    lines = content.splitlines()
    lo = max(0, start_line - _CONTEXT_LINES)
    hi = min(len(lines), start_line + needle.count("\n") + 1 + _CONTEXT_LINES)
    return {"line": start_line + 1, "before": "\n".join(lines[lo:hi])}


def describe(run_id: str, workspace_run_id: str, repo_base_sha: dict,
             amendments: list[dict]) -> list[dict]:
    """Enrich each staged amendment with what a human needs in order to rule on it.

    Adds to every entry:

    * ``line`` / ``context`` — where the change lands and its surroundings.
    * ``origin`` — ``'phase_a'`` when the text being replaced does NOT exist at the
      pinned base SHA (Phase A introduced it during this very change, so amending it
      is correcting an in-flight mistake), ``'baseline'`` when it does exist (this is
      established production contract and other systems may already depend on it), or
      ``'unknown'`` when the base could not be read — never guessed.
    * ``applicable`` — whether the exact ``old_string`` is still present on disk, i.e.
      whether approval can actually replay the edit.

    Fail-open per entry: a provenance lookup that fails downgrades to ``'unknown'``
    rather than dropping the amendment, because a dropped amendment is a silently lost
    human decision.
    """
    ws = workspace_run_id or run_id
    out: list[dict] = []
    for a in amendments or []:
        item = dict(a)
        repo_id, path = item.get("repo_id") or "", item.get("path") or ""
        old = item.get("old_string") or ""
        try:
            cur = _current_content(ws, repo_id, path)
            base = _base_content(ws, repo_id, base_sha=(repo_base_sha or {}).get(repo_id), path=path)

            if item.get("kind") == "create":
                item["origin"] = "new_file"
                item["applicable"] = cur is None
                item["line"] = None
                item["context"] = (item.get("content") or "")[:2000]
            else:
                h = _hunk(cur or "", old)
                item["line"] = h["line"]
                item["context"] = h["before"]
                item["applicable"] = bool(cur and old and old in cur)
                if base is None:
                    item["origin"] = "unknown"
                elif old and old in base:
                    item["origin"] = "baseline"
                else:
                    item["origin"] = "phase_a"
        except Exception as e:  # noqa: BLE001 — a describe failure must not lose the proposal
            logger.warning("schema-amendment: describe failed for %s:%s: %s", repo_id, path, e)
            item.setdefault("origin", "unknown")
            item.setdefault("applicable", False)
        item["origin_note"] = _ORIGIN_NOTES.get(item.get("origin") or "unknown", "")
        out.append(item)
    return out


_ORIGIN_NOTES = {
    "phase_a": ("This text was added by Phase A during THIS change — it is not yet part of the "
                "production contract, so amending it corrects an in-flight mistake rather than "
                "altering an established interface."),
    "baseline": ("This text is PRE-EXISTING in the approved baseline — other systems may already "
                 "depend on it. Changing it is a wire-contract change and needs the same scrutiny "
                 "as any breaking interface change."),
    "new_file": "A schema file that does not exist yet would be created.",
    "unknown": ("The pinned base version could not be read, so the platform cannot tell whether "
                "this text is new in this change or pre-existing. Verify manually before approving."),
}


def apply(run_id: str, workspace_run_id: str, amendments: list[dict]) -> dict:
    """Apply approved amendments to the workspace verbatim.

    Returns ``{applied: [...], failed: [{...,'reason':...}]}``. An amendment whose
    ``old_string`` no longer matches is REPORTED, never force-applied: the file moved
    under the proposal, and guessing a new location is how an approval lands in the
    wrong place.
    """
    from app.agents import workspace_local
    applied, failed = [], []
    ws = workspace_run_id or run_id
    for a in amendments or []:
        repo_id, path = a.get("repo_id") or "", a.get("path") or ""
        try:
            target = _contained_target(ws, repo_id, path)
            if a.get("kind") == "create":
                if target.exists():
                    failed.append({**a, "reason": "file already exists"})
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                workspace_local.write_preserving_eol(target, (a.get("content") or "").replace("\r\n", "\n"))
                applied.append(a)
                continue

            if not target.is_file():
                failed.append({**a, "reason": "file not found"})
                continue
            content = target.read_text(encoding="utf-8", errors="replace")
            old, new = a.get("old_string") or "", a.get("new_string") or ""
            if not old or old not in content:
                failed.append({**a, "reason": "the text to replace is no longer present "
                                              "(the file changed since the proposal was staged)"})
                continue
            if content.count(old) > 1:
                failed.append({**a, "reason": f"the text to replace appears {content.count(old)}× — "
                                              "ambiguous, not applied"})
                continue
            workspace_local.write_preserving_eol(target, content.replace(old, new, 1))
            applied.append(a)
        except Exception as e:  # noqa: BLE001 — one bad amendment must not abort the rest
            logger.exception("schema-amendment apply failed for %s:%s", repo_id, path)
            failed.append({**a, "reason": f"{type(e).__name__}: {e}"})
    return {"applied": applied, "failed": failed}


def rejection_directive(amendments: list[dict], reason: str = "") -> str:
    """The binding directive handed to the code agent when a human REJECTS the amendment.

    Rejection has to be actionable, not just "no". Without a concrete instruction the
    agent re-proposes the same edit next round and the loop resumes — which is the whole
    failure this gate exists to end.
    """
    files = sorted({str(a.get("file") or a.get("path") or "?") for a in (amendments or [])})
    body = (f"The schema amendment you staged for {', '.join(files) or 'the schema'} was "
            "REVIEWED BY A HUMAN AND REJECTED. The schema stays exactly as it is.")
    if (reason or "").strip():
        body += f" Reason given: {reason.strip()[:400]}"
    body += (" Do NOT propose this schema change again — it has been decided. Implement the "
             "requirement entirely in code against the schema as it stands: scope the behaviour "
             "by message type, add a mapping or adapter layer, or constrain it at the boundary. "
             "If you believe the requirement is genuinely impossible without the schema change, "
             "say so explicitly in your summary with file:line evidence instead of re-staging "
             "the edit or silently shipping something you know is wrong.")
    return body
