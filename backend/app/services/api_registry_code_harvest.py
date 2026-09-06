# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tier-1 code-constraint harvest for the API Registry (deterministic, no LLM).

Scans Java sources in a repo clone for DECLARATIVE validation constraints —
Bean Validation annotations (@Size/@Pattern/@NotNull/@NotBlank/@Digits) on
field declarations — and attaches them as evidence to matching ``ApiField``
rows (``constraint_sources["code"]``), each entry carrying file, line and the
verbatim snippet so it stays re-verifiable after the code moves.

This tier NEVER changes a row's canonical constraint cells: code evidence is
recorded alongside the XSD facts, and a conflict flag is set when the two
disagree (e.g. code @Size(max=35) vs XSD maxLength 255) so the UI/reviewer
can resolve it. Imperative validators (cross-field, conditional) are tier 2 —
agent-extracted with human review — and out of scope here.
"""
from __future__ import annotations

import logging
import re
from itertools import islice, zip_longest
from pathlib import Path

logger = logging.getLogger(__name__)

_ANNOT_RE = re.compile(
    r"@(Size|Pattern|NotNull|NotBlank|NotEmpty|Digits)\s*(\(([^)]*)\))?", re.MULTILINE)
_FIELD_DECL_RE = re.compile(
    r"(?:private|protected|public)\s+[\w.<>\[\]]+\s+(\w+)\s*[;=]")
_PARAM_RE = re.compile(r"(\w+)\s*=\s*(\"(?:[^\"\\]|\\.)*\"|\d+)")

_MAX_FILES = 4000


def _parse_params(raw: str | None) -> dict:
    if not raw:
        return {}
    out = {}
    for k, v in _PARAM_RE.findall(raw):
        out[k] = v.strip('"') if v.startswith('"') else int(v)
    return out


def scan_java_dir(java_dir: Path) -> list[dict]:
    """Return [{field, annotation, params, file, line, snippet}] for annotated fields.

    Given a repo/clone root with Maven-style modules, scans production sources only
    (every ``**/src/main/java`` tree beneath it); a dir with no such trees — e.g. an
    explicit ``java_dir`` pointing at a single source tree — is scanned as-is.
    """
    root = Path(java_dir)
    main_trees = sorted(root.rglob("src/main/java"))
    if main_trees:
        # Interleave modules round-robin under the global cap — a lexically early
        # huge module must not starve later modules out of the scan entirely.
        per_tree = [sorted(t.rglob("*.java")) for t in main_trees]
        interleaved = (f for rnd in zip_longest(*per_tree) for f in rnd if f is not None)
        files = list(islice(interleaved, _MAX_FILES))
        total = sum(len(t) for t in per_tree)
        if total > _MAX_FILES:
            logger.info("scan_java_dir: %d java files under %s capped to %d "
                        "(round-robin per module)", total, root, _MAX_FILES)
    else:
        files = list(root.rglob("*.java"))[:_MAX_FILES]
    found: list[dict] = []
    for path in files:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        pending: list[tuple[str, dict, int]] = []  # annotations awaiting their field decl
        for i, line in enumerate(lines):
            for m in _ANNOT_RE.finditer(line):
                pending.append((m.group(1), _parse_params(m.group(3)), i + 1))
            fm = _FIELD_DECL_RE.search(line)
            if fm and pending:
                snippet_start = min(ln for _, _, ln in pending) - 1
                snippet = "\n".join(lines[snippet_start:i + 1])[:500]
                for annot, params, ln in pending:
                    found.append({
                        "field": fm.group(1), "annotation": annot, "params": params,
                        "file": str(path), "line": ln, "snippet": snippet,
                    })
                pending = []
            elif line.strip() and not line.strip().startswith(("@", "//", "*", "/*")):
                pending = []
    return found


def clone_branch(clone: Path) -> str | None:
    """Current branch of a git clone dir, read from .git/HEAD (no subprocess)."""
    try:
        head = (Path(clone) / ".git" / "HEAD").read_text(errors="replace").strip()
    except OSError:
        return None
    return head.removeprefix("ref: refs/heads/") if head.startswith("ref:") else None


def select_source_clones(baselines: list[dict] | None, richness) -> list[Path]:
    """Workspace clone roots to read registry inputs from.

    Clones live at ``<agentic_workspace_root>/<run_id>/<repo_id>`` (the
    ``workspace_local`` layout — the same setting the codegen used to put them
    there, so this resolves identically in docker and host deployments).

    ``baselines`` is ``[{"repo_id": ..., "branch": ...}]`` from the rows marked
    ``is_registry_baseline``. For each baseline pick its best clone — prefer
    clones still on the baseline branch, then the richest by ``richness(clone)``,
    then the newest. With no baselines (or none of them cloned yet) fall back to
    the single richest clone overall, so the buttons keep working before a
    production branch is selected.
    """
    from app.core.config import settings

    root = Path(settings.agentic_workspace_root)
    if not root.is_dir():
        return []
    clones: list[tuple[str, Path, int]] = []  # (repo_id, clone, richness)
    for clone in sorted(root.glob("*/*")):
        # Run-level "_" dirs (_reconcile_cache) are caches, not run clones.
        if clone.parent.name.startswith("_") or not clone.is_dir():
            continue
        clones.append((clone.name, clone, richness(clone)))

    picks: list[Path] = []
    for b in baselines or []:
        # Zero-richness clones stay rankable: a clone on the production branch
        # must win even when it holds no relevant files — falling through to a
        # source-bearing WIP clone would present non-production content as the
        # baseline.
        matching = [(clone_branch(c) == b.get("branch"), r, c.stat().st_mtime, c)
                    for repo_id, c, r in clones if repo_id == b.get("repo_id")]
        if matching:
            matching.sort(reverse=True)
            picks.append(matching[0][3])
    if picks:
        return picks
    rich_clones = [t for t in clones if t[2] > 0]
    if rich_clones:
        best = max(rich_clones, key=lambda t: (t[2], t[1].stat().st_mtime))
        return [best[1]]
    return []


def discover_default_java_dirs(baselines: list[dict] | None = None) -> list[Path]:
    """Repo-clone roots to harvest — the best clone of every production-baseline
    repo (see ``select_source_clones``). ``scan_java_dir`` covers every module's
    ``src/main/java`` beneath each root."""
    def java_count(clone: Path) -> int:
        return sum(1 for d in clone.rglob("src/main/java") for _ in d.rglob("*.java"))
    return select_source_clones(baselines, java_count)


def _xsd_max_len(f) -> int | None:
    xsd = (f.constraint_sources or {}).get("xsd") or {}
    for key in ("length", "max_length"):
        v = xsd.get(key)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
    return None


def harvest_into_registry(db, java_dirs: Path | list[Path]) -> dict:
    """Attach annotation evidence to fields whose xml_tag matches the Java field name."""
    from app.models.api_registry import ApiField

    dirs = [Path(java_dirs)] if isinstance(java_dirs, (str, Path)) else [Path(d) for d in java_dirs]
    hits = [h for d in dirs for h in scan_java_dir(d)]
    counts = {"annotations_found": len(hits), "fields_updated": 0, "conflicts": 0}
    if not hits:
        return counts

    by_field: dict[str, list[dict]] = {}
    for h in hits:
        by_field.setdefault(h["field"].lower(), []).append(h)

    rows = db.query(ApiField).filter(ApiField.status == "active").all()
    for row in rows:
        matches = by_field.get(row.xml_tag.lower())
        if not matches:
            continue
        evidence = []
        conflict = False
        for h in matches[:5]:
            entry = {
                "annotation": h["annotation"], "params": h["params"],
                "file": h["file"], "line": h["line"], "snippet": h["snippet"],
            }
            code_max = h["params"].get("max")
            xsd_max = _xsd_max_len(row)
            if isinstance(code_max, int) and xsd_max is not None and code_max != xsd_max:
                entry["conflict_with_xsd"] = f"code max={code_max} vs xsd max={xsd_max}"
                conflict = True
            evidence.append(entry)
        cs = dict(row.constraint_sources or {})
        cs["code"] = {"evidence": evidence, "java_dirs": [str(d) for d in dirs],
                      "match": "field-name (tier-1 heuristic)"}
        row.constraint_sources = cs
        counts["fields_updated"] += 1
        if conflict:
            counts["conflicts"] += 1
    db.commit()
    return counts
