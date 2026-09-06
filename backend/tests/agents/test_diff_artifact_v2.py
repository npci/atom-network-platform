# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The v2 structured diff artifact — exact stats, bounded previews, complete file list.

Regression guard for the prod defect chain where the UI derived the changed-file list
and ± counts from a size-capped text blob: the cap silently dropped files from the
rendered list and produced cut-off counts (the observed fake "−6793"), while the push
carried the full manifest. The v2 artifact computes op/±counts from the FULL diff
BEFORE any storage bounding — bounding only ever shortens the stored ``patch`` preview.

These tests run against REAL temp git repos (same git the workspace uses), plus
monkeypatch-style endpoint shaping tests for /diff, mirroring test_approve_xsd_gate.
"""
import subprocess
from pathlib import Path
from types import SimpleNamespace

from app.agents import agentic_orchestrator as O
from app.agents import workspace_local as W
from app.api import agentic as A


# ── Real-git fixtures ──────────────────────────────────────────────────────────

def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def _mk_repo(tmp_path: Path, ws="ws1", rid="repoA") -> Path:
    rd = tmp_path / ws / rid
    rd.mkdir(parents=True)
    _git(rd, "init", "-q")
    _git(rd, "config", "user.email", "t@t"); _git(rd, "config", "user.name", "t")
    return rd


def _commit_all(rd, msg="base"):
    _git(rd, "add", "-A")
    _git(rd, "commit", "-q", "-m", msg)
    return _git(rd, "rev-parse", "HEAD").stdout.strip()


def _run_stub(ws="ws1", rids=("repoA",)):
    return SimpleNamespace(id=ws, kind="full", workspace_run_id=None,
                           selected_repo_ids=list(rids))


def _use_tmp_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(W, "workspace_root", lambda: tmp_path)


# ── _capture_diffs: shape, ops, counts ─────────────────────────────────────────

def test_v2_shape_ops_and_exact_counts(tmp_path, monkeypatch):
    _use_tmp_workspace(monkeypatch, tmp_path)
    rd = _mk_repo(tmp_path)
    (rd / "mod.txt").write_text("a\nb\nc\n")
    (rd / "gone.txt").write_text("x\ny\n")
    _commit_all(rd)
    (rd / "mod.txt").write_text("a\nB2\nc\nd\n")     # 2 added, 1 removed
    (rd / "gone.txt").unlink()                        # deleted (2 removed)
    (rd / "new.txt").write_text("n1\nn2\n")           # untracked new (2 added)

    out = O._capture_diffs(None, _run_stub())
    assert out["repoA"]["v"] == 2
    by = {f["path"]: f for f in out["repoA"]["files"]}
    assert set(by) == {"mod.txt", "gone.txt", "new.txt"}
    assert by["mod.txt"]["op"] == "modify" and by["mod.txt"]["add"] == 2 and by["mod.txt"]["del"] == 1
    assert by["gone.txt"]["op"] == "delete" and by["gone.txt"]["del"] == 2
    assert by["new.txt"]["op"] == "add" and by["new.txt"]["add"] == 2
    assert all(not f["truncated"] for f in by.values())
    assert "+n1" in by["new.txt"]["patch"] and "new file mode" in by["new.txt"]["patch"]


def test_capture_leaves_index_untouched(tmp_path, monkeypatch):
    # The intent-to-add (`git add -N`) used to surface untracked files must be undone:
    # nothing staged afterwards, and the new file still reported as untracked.
    _use_tmp_workspace(monkeypatch, tmp_path)
    rd = _mk_repo(tmp_path)
    (rd / "a.txt").write_text("a\n")
    _commit_all(rd)
    (rd / "new.txt").write_text("n\n")
    O._capture_diffs(None, _run_stub())
    assert _git(rd, "diff", "--cached", "--quiet").returncode == 0
    assert "new.txt" in _git(rd, "ls-files", "--others", "--exclude-standard").stdout


def test_25k_line_file_full_rewrite_stored_in_full(tmp_path, monkeypatch):
    # THE fleet's real worst case: the largest source file is ~25K lines. A FULL-file
    # rewrite (25K removed + 25K added ≈ 3MB of diff) must fit inside the per-file cap
    # UNTRUNCATED — real counts AND the complete patch preview.
    _use_tmp_workspace(monkeypatch, tmp_path)
    rd = _mk_repo(tmp_path)
    n = 25_000
    (rd / "Giant.java").write_text("".join(f"    private int oldField{i:07d} = {i};\n" for i in range(n)))
    _commit_all(rd)
    (rd / "Giant.java").write_text("".join(f"    private long newField{i:07d} = {i}L;\n" for i in range(n)))

    f = {x["path"]: x for x in O._capture_diffs(None, _run_stub())["repoA"]["files"]}["Giant.java"]
    assert f["add"] == n and f["del"] == n and f["op"] == "modify"
    assert f["truncated"] is False
    assert f"+    private long newField{n - 1:07d}" in f["patch"]   # last line present → complete
    assert f"-    private int oldField{n - 1:07d}" in f["patch"]


def test_per_file_truncation_keeps_exact_counts(tmp_path, monkeypatch):
    # Beyond the fleet's largest file (a >4M single-file diff): preview must be bounded,
    # the count must be the REAL one.
    _use_tmp_workspace(monkeypatch, tmp_path)
    rd = _mk_repo(tmp_path)
    (rd / "keep.txt").write_text("k\n")
    _commit_all(rd)
    n = 250_000
    (rd / "big.txt").write_text("".join(f"line-{i:012d}\n" for i in range(n)))

    f = {x["path"]: x for x in O._capture_diffs(None, _run_stub())["repoA"]["files"]}["big.txt"]
    assert f["add"] == n and f["truncated"] is True
    assert len(f["patch"]) <= 4_000_000 + 200
    assert "patch preview truncated" in f["patch"]


def test_total_budget_bounds_previews_never_facts(tmp_path, monkeypatch):
    # 6 giant files × 4M capped previews > 20M total: some previews get omitted, but
    # every file keeps its row and its exact count — nothing silently disappears.
    _use_tmp_workspace(monkeypatch, tmp_path)
    rd = _mk_repo(tmp_path)
    (rd / "seed.txt").write_text("s\n")
    _commit_all(rd)
    n = 240_000
    for i in range(6):
        (rd / f"f{i}.txt").write_text("".join(f"row-{i}-{j:010d}\n" for j in range(n)))

    files = O._capture_diffs(None, _run_stub())["repoA"]["files"]
    assert len(files) == 6
    assert all(f["add"] == n for f in files)                      # exact, every file
    omitted = [f for f in files if "patch preview omitted" in f["patch"]]
    assert omitted and all(f["truncated"] for f in omitted)
    assert sum(len(f["patch"]) for f in files) <= 20_000_000 + 4_000_000 + 1_000
    # the rendered text (what /diff serves) still lists every file
    text = "".join(f["patch"] for f in files)
    import re
    assert len(re.findall(r"(?m)^diff --git ", text)) == 6


def test_build_output_and_lease_excluded(tmp_path, monkeypatch):
    _use_tmp_workspace(monkeypatch, tmp_path)
    rd = _mk_repo(tmp_path)
    (rd / "src.txt").write_text("s\n")
    _commit_all(rd)
    (rd / "src.txt").write_text("s2\n")
    (rd / "target").mkdir(); (rd / "target" / "junk.class").write_text("bin")
    (rd / ".lease").write_text("uuid")

    files = O._capture_diffs(None, _run_stub())["repoA"]["files"]
    assert [f["path"] for f in files] == ["src.txt"]


def test_empty_changeset_omits_repo(tmp_path, monkeypatch):
    _use_tmp_workspace(monkeypatch, tmp_path)
    rd = _mk_repo(tmp_path)
    (rd / "a.txt").write_text("a\n")
    _commit_all(rd)
    assert O._capture_diffs(None, _run_stub()) == {}


# ── _diff_text: both artifact shapes ──────────────────────────────────────────

def test_diff_text_accepts_both_shapes():
    assert A._diff_text("legacy blob") == "legacy blob"
    assert A._diff_text({"v": 2, "files": [{"patch": "p1"}, {"patch": None}, {}]}) == "p1"
    assert A._diff_text(None) == ""
    assert A._diff_text({}) == ""


# ── /diff endpoint shaping (monkeypatch style, no DB) ─────────────────────────

class _Q:
    def __init__(self, man): self.man = man
    def query(self, model):
        return SimpleNamespace(filter=lambda *a: SimpleNamespace(
            order_by=lambda *a2: SimpleNamespace(first=lambda: self.man)))


def _diff_call(monkeypatch, man, run):
    monkeypatch.setattr(A, "_run_or_404", lambda db, rid: run)
    monkeypatch.setattr(A, "_authz_read", lambda run, user: None)
    # run.id as run_id so the live-workspace fallback resolves the same tmp clone dir
    return A.get_diff(run.id, _Q(man), SimpleNamespace(id="u1"))


def test_diff_endpoint_serves_v2_text_plus_exact_stats(monkeypatch):
    v2 = {"v": 2, "files": [
        {"path": "a.java", "op": "modify", "add": 5, "del": 2, "patch": "diff --git a/a.java b/a.java\n+x\n", "truncated": False},
        {"path": "b.java", "op": "add", "add": 6793, "del": 0, "patch": "diff --git a/b.java b/b.java\n… (patch preview omitted — the branch holds the full file)\n", "truncated": True},
    ]}
    man = SimpleNamespace(diffs={"repoA": v2})
    out = _diff_call(monkeypatch, man, _run_stub(rids=("repoA",)))
    assert "diff --git a/a.java" in out["diffs"]["repoA"] and "diff --git a/b.java" in out["diffs"]["repoA"]
    assert out["stats"]["repoA"]["a.java"] == {"op": "modify", "add": 5, "del": 2, "truncated": False}
    assert out["stats"]["repoA"]["b.java"]["add"] == 6793 and out["stats"]["repoA"]["b.java"]["truncated"] is True


def test_diff_endpoint_legacy_blob_passthrough_no_stats(monkeypatch):
    man = SimpleNamespace(diffs={"repoA": "diff --git a/x b/x\n+1\n"})
    out = _diff_call(monkeypatch, man, _run_stub(rids=("repoA",)))
    assert out["diffs"]["repoA"] == "diff --git a/x b/x\n+1\n"
    assert out["stats"] == {}


def test_diff_endpoint_frozen_repo_absent_means_untouched(monkeypatch):
    man = SimpleNamespace(diffs={"repoA": "diff --git a/x b/x\n+1\n"})
    out = _diff_call(monkeypatch, man, _run_stub(rids=("repoA", "repoB")))
    assert out["diffs"]["repoB"] == "(no changes in this phase)"


def test_diff_endpoint_gcd_workspace_message(monkeypatch, tmp_path):
    monkeypatch.setattr(W, "workspace_root", lambda: tmp_path)   # empty → no .git anywhere
    out = _diff_call(monkeypatch, None, _run_stub(rids=("repoA",)))
    assert out["diffs"]["repoA"] == "(workspace cleaned up — no stored diff)"
    assert out["stats"] == {}


def test_diff_endpoint_live_prefreeze_excludes_lease(monkeypatch, tmp_path):
    monkeypatch.setattr(W, "workspace_root", lambda: tmp_path)
    rd = _mk_repo(tmp_path)
    (rd / "a.txt").write_text("a\n")
    _commit_all(rd)
    (rd / "a.txt").write_text("a2\n")
    (rd / ".lease").write_text("uuid")
    out = _diff_call(monkeypatch, None, _run_stub(rids=("repoA",)))
    assert "+a2" in out["diffs"]["repoA"] and ".lease" not in out["diffs"]["repoA"]


# ── _walkthrough_diff: scoped to the change-set, robust to the push commit ─────

def _wt_call(monkeypatch, man, run):
    monkeypatch.setattr(A, "_run_or_404", lambda db, rid: run)   # not used, but harmless
    return A._walkthrough_diff(_Q(man), run)


def test_walkthrough_scoped_to_manifest_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "workspace_root", lambda: tmp_path)
    rd = _mk_repo(tmp_path)
    (rd / "fileA.java").write_text("a\n")
    base = _commit_all(rd)
    (rd / "fileA.java").write_text("a\nchanged\n")
    (rd / "junk.txt").write_text("noise\n")                       # NOT in the manifest
    (rd / "target").mkdir(); (rd / "target" / "x.class").write_text("bin")
    (rd / ".lease").write_text("uuid")
    man = SimpleNamespace(
        per_repo=[{"repo_id": "repoA", "base_commit_sha": base}],
        operations=[{"repo_id": "repoA", "path": "fileA.java", "op": "modify"}])

    d = _wt_call(monkeypatch, man, _run_stub())
    assert "+changed" in d
    assert "junk.txt" not in d and "target/" not in d and ".lease" not in d


def test_walkthrough_survives_the_push_commit(tmp_path, monkeypatch):
    # After push, HEAD contains the change — the walkthrough must diff vs the recorded
    # base, or it sees an empty change (the prod "only target/ + .lease" walkthrough).
    monkeypatch.setattr(W, "workspace_root", lambda: tmp_path)
    rd = _mk_repo(tmp_path)
    (rd / "fileA.java").write_text("a\n")
    base = _commit_all(rd)
    (rd / "fileA.java").write_text("a\nchanged\n")
    _commit_all(rd, "agentic: pushed")                            # simulate the push commit
    man = SimpleNamespace(
        per_repo=[{"repo_id": "repoA", "base_commit_sha": base}],
        operations=[{"repo_id": "repoA", "path": "fileA.java", "op": "modify"}])

    d = _wt_call(monkeypatch, man, _run_stub())
    assert "+changed" in d


def test_walkthrough_no_manifest_falls_back_to_changed_files(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "workspace_root", lambda: tmp_path)
    rd = _mk_repo(tmp_path)
    (rd / "fileA.java").write_text("a\n")
    _commit_all(rd)
    (rd / "fileA.java").write_text("a2\n")
    (rd / "target").mkdir(); (rd / "target" / "x.class").write_text("bin")
    (rd / ".lease").write_text("uuid")

    d = _wt_call(monkeypatch, None, _run_stub())
    assert "+a2" in d and "target/" not in d and ".lease" not in d
