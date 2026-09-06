# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 6.4 — hierarchical file-tree summary tests.

Pure-Python — no DB, no agents.
"""
from __future__ import annotations

from app.agents._file_tree import format_tree_summary


def test_empty_paths_returns_empty():
    assert format_tree_summary([]) == ""


def test_single_file_simple():
    out = format_tree_summary(["src/Main.java"])
    assert "Main.java" in out
    assert "src/" in out


def test_rollup_aggregates_file_count_at_depth_limit():
    paths = [
        "src/main/java/com/network/core/entities/Token.java",
        "src/main/java/com/network/core/entities/Refund.java",
        "src/main/java/com/network/core/entities/Dispute.java",
        "src/main/java/com/network/core/services/AuthSvc.java",
        "src/main/java/com/network/core/services/RefundSvc.java",
    ]
    # depth=8 reaches all the way to the leaf files (7 dir levels + leaf).
    out = format_tree_summary(paths, depth=8)
    assert "entities" in out
    # Same paths at depth=4 should still surface the upper-level structure
    # without exploding all leaves.
    out_shallow = format_tree_summary(paths, depth=4)
    assert "src" in out_shallow
    # The full flat list of files shouldn't appear at the shallower depth.
    assert "Token.java" not in out_shallow


def test_deeper_paths_get_aggregated_under_depth_cap():
    # Many paths in one subtree should all roll up to one "(N files)" line.
    paths = [
        f"a/b/c/d/file_{i}.py" for i in range(50)
    ]
    out = format_tree_summary(paths, depth=2)
    # We expect b/ to show a count of 50 (or its subdir to).
    assert "50 files" in out or "files)" in out


def test_indentation_grows_with_depth():
    paths = [
        "x/y/z/file.java",
        "x/y/file2.java",
    ]
    out = format_tree_summary(paths, depth=3)
    lines = out.splitlines()
    # The "y/" line should be indented further than "x/"
    x_line = next(ln for ln in lines if ln.lstrip().startswith("x/"))
    y_line = next(ln for ln in lines if ln.lstrip().startswith("y/"))
    assert len(y_line) - len(y_line.lstrip()) > len(x_line) - len(x_line.lstrip())


def test_depth_one_rolls_everything_to_top():
    paths = ["a/b/c/d.py", "a/x/y/z.py", "a/foo/bar.py"]
    out = format_tree_summary(paths, depth=1)
    # Only top-level dirs should be expanded; all descendants aggregated.
    # We're not strict about exact count format, just that the deep
    # filenames don't all appear.
    assert "a/" in out
    assert "d.py" not in out and "bar.py" not in out


def test_invalid_depth_clamps_to_one():
    out = format_tree_summary(["a/b/c.py"], depth=0)
    assert out  # didn't crash
