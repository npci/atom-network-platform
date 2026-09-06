# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Workspace-clone discovery for the API Registry ingest/harvest sources.

Discovery must resolve from ``settings.agentic_workspace_root`` — the same setting
the codegen used to create the clones, so it works identically for docker and
host deployments (the hardcoded ``/app/workspace`` broke the latter) — prefer the
production-baseline repos' clones (branch match first), and scan whole clones:
every module's ``src/main/java``, production sources only.
"""
from __future__ import annotations

from pathlib import Path

from app.core.config import settings
from app.services.api_registry_code_harvest import (
    clone_branch, discover_default_java_dirs, scan_java_dir,
)
from app.services.api_registry_ingest import discover_default_xsd_dirs

ANNOTATED = "@Size(max=35)\nprivate String {name};\n"


def _mk_clone(root: Path, run: str, repo: str, branch="main", modules=(("mod", 1),)) -> Path:
    clone = root / run / repo
    (clone / ".git").mkdir(parents=True)
    (clone / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
    for name, n_java in modules:
        src = clone / name / "src" / "main" / "java"
        src.mkdir(parents=True)
        for i in range(n_java):
            (src / f"F{i}.java").write_text(
                "public class F%d {\n%s}\n" % (i, ANNOTATED.format(name=f"{name}Field{i}")))
    return clone


def _ws(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    return tmp_path


# ── clone_branch ──────────────────────────────────────────────────────────────

def test_clone_branch_reads_head(tmp_path):
    clone = _mk_clone(tmp_path, "r1", "repoA", branch="release/5.0")
    assert clone_branch(clone) == "release/5.0"


def test_clone_branch_detached_or_missing_is_none(tmp_path):
    clone = _mk_clone(tmp_path, "r1", "repoA")
    (clone / ".git" / "HEAD").write_text("0123456789abcdef0123456789abcdef01234567\n")
    assert clone_branch(clone) is None
    assert clone_branch(tmp_path / "nope") is None


# ── java-dir discovery ────────────────────────────────────────────────────────

def test_discovery_rooted_at_workspace_setting(tmp_path, monkeypatch):
    ws = _ws(monkeypatch, tmp_path)
    clone = _mk_clone(ws, "run1", "repoA")
    assert discover_default_java_dirs() == [clone]


def test_baseline_clone_beats_richer_other_repo(tmp_path, monkeypatch):
    ws = _ws(monkeypatch, tmp_path)
    small = _mk_clone(ws, "run1", "repoA", modules=(("m", 1),))
    rich = _mk_clone(ws, "run1", "repoB", modules=(("m", 5),))
    assert discover_default_java_dirs([{"repo_id": "repoA", "branch": "main"}]) == [small]
    # no baselines → richest clone overall
    assert discover_default_java_dirs() == [rich]


def test_one_clone_per_baseline_repo(tmp_path, monkeypatch):
    ws = _ws(monkeypatch, tmp_path)
    core = _mk_clone(ws, "run1", "core-id")
    app = _mk_clone(ws, "run2", "app-id")
    picks = discover_default_java_dirs([
        {"repo_id": "core-id", "branch": "main"},
        {"repo_id": "app-id", "branch": "main"},
    ])
    assert picks == [core, app]


def test_branch_match_beats_richness_within_repo(tmp_path, monkeypatch):
    ws = _ws(monkeypatch, tmp_path)
    on_branch = _mk_clone(ws, "run1", "repoA", branch="main", modules=(("m", 1),))
    _mk_clone(ws, "run2", "repoA", branch="agent/wip", modules=(("m", 9),))
    assert discover_default_java_dirs([{"repo_id": "repoA", "branch": "main"}]) == [on_branch]


def test_baseline_without_clone_falls_back_to_richest(tmp_path, monkeypatch):
    ws = _ws(monkeypatch, tmp_path)
    clone = _mk_clone(ws, "run1", "repoA")
    assert discover_default_java_dirs([{"repo_id": "never-cloned", "branch": "main"}]) == [clone]


def test_branch_match_wins_even_with_zero_richness(tmp_path, monkeypatch):
    # An EMPTY production-branch clone must still beat a source-bearing WIP
    # clone of the same repo — never substitute WIP content as the baseline.
    ws = _ws(monkeypatch, tmp_path)
    prod = ws / "prod-run" / "repoA"
    (prod / ".git").mkdir(parents=True)
    (prod / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    _mk_clone(ws, "wip-run", "repoA", branch="agent/wip", modules=(("m", 3),))
    assert discover_default_java_dirs([{"repo_id": "repoA", "branch": "main"}]) == [prod]


def test_no_wip_xsd_substitution_for_production_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "knowledge_base_dir", str(tmp_path / "no-kb"))
    ws = _ws(monkeypatch, tmp_path / "ws")
    ws.mkdir()
    prod = ws / "prod-run" / "repoA"
    (prod / ".git").mkdir(parents=True)
    (prod / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    wip = _mk_clone(ws, "wip-run", "repoA", branch="agent/wip")
    res = wip / "m" / "src" / "main" / "resources"
    res.mkdir(parents=True)
    (res / "ReqWip.xsd").write_text("<xs/>")
    # Production clone has no XSDs → no dirs, NOT the WIP clone's schemas.
    assert discover_default_xsd_dirs([{"repo_id": "repoA", "branch": "main"}]) == []


def test_cache_dirs_and_missing_root_skipped(tmp_path, monkeypatch):
    ws = _ws(monkeypatch, tmp_path)
    _mk_clone(ws, "_reconcile_cache", "repoA")
    assert discover_default_java_dirs() == []
    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path / "absent"))
    assert discover_default_java_dirs() == []


# ── scan_java_dir on a clone root ─────────────────────────────────────────────

def test_scan_covers_all_modules_main_sources_only(tmp_path):
    clone = _mk_clone(tmp_path, "run1", "repoA", modules=(("alpha", 1), ("beta", 1)))
    test_src = clone / "alpha" / "src" / "test" / "java"
    test_src.mkdir(parents=True)
    (test_src / "T.java").write_text(
        "public class T {\n@Size(max=1)\nprivate String testOnly;\n}\n")
    fields = {h["field"] for h in scan_java_dir(clone)}
    assert fields == {"alphaField0", "betaField0"}  # both modules, no test sources


def test_scan_direct_source_tree_still_works(tmp_path):
    clone = _mk_clone(tmp_path, "run1", "repoA", modules=(("m", 1),))
    hits = scan_java_dir(clone / "m" / "src" / "main" / "java")
    assert [h["field"] for h in hits] == ["mField0"]


def test_scan_cap_shared_across_modules(tmp_path, monkeypatch):
    # The global file cap is spread round-robin, so a lexically early module
    # bigger than the whole cap can't starve later modules out of the scan.
    import app.services.api_registry_code_harvest as harvest
    monkeypatch.setattr(harvest, "_MAX_FILES", 4)
    clone = _mk_clone(tmp_path, "run1", "repoA", modules=(("alpha", 6),))
    omega = clone / "omega" / "src" / "main" / "java"
    omega.mkdir(parents=True)
    (omega / "Evidence.java").write_text(
        "class E {\n@Size(max=35)\nprivate String omegaEvidence;\n}\n")
    fields = {h["field"] for h in scan_java_dir(clone)}
    assert "omegaEvidence" in fields


# ── xsd-dir discovery ─────────────────────────────────────────────────────────

def test_xsd_kb_priority(tmp_path, monkeypatch):
    kb = tmp_path / "kb"
    (kb / "existing_xsds").mkdir(parents=True)
    (kb / "existing_xsds" / "ReqTransfer.xsd").write_text("<xs/>")
    monkeypatch.setattr(settings, "knowledge_base_dir", str(kb))
    assert discover_default_xsd_dirs() == [kb / "existing_xsds"]


def test_xsd_from_baseline_clone_when_no_kb(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "knowledge_base_dir", str(tmp_path / "no-kb"))
    ws = _ws(monkeypatch, tmp_path / "ws")
    ws.mkdir()
    clone = _mk_clone(ws, "run1", "core-id")
    res = clone / "network-domain-xsd" / "src" / "main" / "resources"
    res.mkdir(parents=True)
    (res / "ReqTransfer.xsd").write_text("<xs/>")
    assert discover_default_xsd_dirs([{"repo_id": "core-id", "branch": "main"}]) == [res]
