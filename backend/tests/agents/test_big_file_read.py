# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""read_file on a huge file returns a navigable skeleton, not the body (§8 context safety)."""
import subprocess

import pytest

from app.core.config import settings
from app.agents import agentic_tools as T

RID = "repo-1"
RUN = "run-1"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    rd = tmp_path / RUN / RID
    (rd / "src").mkdir(parents=True)
    # realistic multi-line methods (one-liners get noise-filtered as trivial)
    big = "package p;\nclass Big {\n" + "\n".join(
        f"  public int method_{i}(int alpha, String beta) {{\n"
        f"    int gamma = alpha + {i};\n"
        f"    for (int k = 0; k < gamma; k++) gamma += k;\n"
        f"    return gamma;\n"
        f"  }}"
        for i in range(2500)) + "\n}\n"
    (rd / "src" / "Big.java").write_text(big)
    subprocess.run(["git", "init", "-q"], cwd=rd, check=True)
    return rd, big


def _ctx():
    return T.RunContext(run_id=RUN, selected_repo_ids=[RID])


def test_huge_file_returns_outline_not_body(ws):
    rd, big = ws
    assert len(big) > 200 * 1024                         # genuinely over the read cap
    ctx = _ctx()
    out = T.read_file(ctx, RID, "src/Big.java")
    assert len(out) < len(big) // 3                      # not the full file
    assert "is large" in out                             # the big-file banner fired
    # a skeleton-only read does NOT count as a full read — the agent must read a real
    # range before it can edit (read-before-edit still holds)
    assert (RID, "src/Big.java") not in ctx.read_files


def test_range_read_of_huge_file_works_and_counts(ws):
    ctx = _ctx()
    ranged = T.read_file(ctx, RID, "src/Big.java", start_line=1, end_line=3)
    assert "class Big" in ranged
    assert (RID, "src/Big.java") in ctx.read_files


def test_skeleton_lists_symbols_with_line_ranges(ws):
    from app.rag import code_chunker_langs as C
    if "java" not in C.supported_languages():
        pytest.skip("tree-sitter java not available in this environment")
    out = T.read_file(_ctx(), RID, "src/Big.java")
    assert "STRUCTURE" in out and "method_" in out       # symbols listed
    assert "L1" in out or "L2" in out or "L3" in out      # with line ranges


def test_is_context_overflow_detection():
    from app.agents.agentic_runtime import _is_context_overflow
    assert _is_context_overflow(Exception("prompt is too long: 1200000 tokens > 1000000 maximum"))
    assert _is_context_overflow(RuntimeError("context window exceeded"))
    assert not _is_context_overflow(Exception("rate limit exceeded (429)"))
    assert not _is_context_overflow(Exception("invalid x-api-key"))
