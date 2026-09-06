# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Stale-edit guard (hashline-lite): edit_file refuses when the on-disk content no longer
matches what the model last read/wrote — a re-read refreshes the hash and unblocks."""
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
    (rd / "src" / "A.java").write_text("class A {\n    int x = 1;\n}\n")
    subprocess.run(["git", "init", "-q"], cwd=rd, check=True)
    subprocess.run(["git", "add", "-A"], cwd=rd, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
                   cwd=rd, check=True)
    return rd


def _ctx():
    return T.RunContext(run_id=RUN, selected_repo_ids=[RID])


def test_external_change_after_read_refuses_edit(ws):
    ctx = _ctx()
    T.read_file(ctx, RID, "src/A.java")
    # something else rewrites the file behind the model's back
    (ws / "src" / "A.java").write_text("class A {\n    int x = 1;\n    int y = 2;\n}\n")
    with pytest.raises(T.ToolError, match="CHANGED on disk"):
        T.edit_file(ctx, RID, "src/A.java", "int x = 1;", "int x = 2;")
    # file untouched by the refused edit
    assert "int x = 1;" in (ws / "src" / "A.java").read_text()


def test_reread_refreshes_hash_and_unblocks(ws):
    ctx = _ctx()
    T.read_file(ctx, RID, "src/A.java")
    (ws / "src" / "A.java").write_text("class A {\n    int x = 1;\n    int y = 2;\n}\n")
    T.read_file(ctx, RID, "src/A.java")            # the re-read the error demands
    msg = T.edit_file(ctx, RID, "src/A.java", "int x = 1;", "int x = 2;")
    assert "edited" in msg
    assert "int x = 2;" in (ws / "src" / "A.java").read_text()


def test_own_edits_never_trip_the_guard(ws):
    ctx = _ctx()
    T.read_file(ctx, RID, "src/A.java")
    T.edit_file(ctx, RID, "src/A.java", "int x = 1;", "int x = 2;")
    # a second edit without a fresh read is fine — the write updated the hash
    msg = T.edit_file(ctx, RID, "src/A.java", "int x = 2;", "int x = 3;")
    assert "edited" in msg


def test_created_file_tracks_hash(ws):
    ctx = _ctx()
    T.create_file(ctx, RID, "src/B.java", "class B {\n  int q = 1;\n}\n")
    (ws / "src" / "B.java").write_text("class B {\n  int q = 1;\n  int r = 2;\n}\n")
    with pytest.raises(T.ToolError, match="CHANGED on disk"):
        T.edit_file(ctx, RID, "src/B.java", "int q = 1;", "int q = 9;")


def test_large_file_skeleton_reread_unwedges_the_guard(ws, monkeypatch):
    # A >_READ_MAX file's un-ranged re-read returns the skeleton/head view — it must
    # still refresh the hash, or the demanded re-read leaves the edit wedged forever.
    ctx = _ctx()
    T.read_file(ctx, RID, "src/A.java", start_line=1, end_line=3)   # ranged read registers the hash
    monkeypatch.setattr(T, "_READ_MAX", 10)                          # now the file counts as "large"
    (ws / "src" / "A.java").write_text("class A {\n    int x = 1;\n    int y = 2;\n}\n")
    with pytest.raises(T.ToolError, match="CHANGED on disk"):
        T.edit_file(ctx, RID, "src/A.java", "int x = 1;", "int x = 2;")
    T.read_file(ctx, RID, "src/A.java")            # the demanded re-read → skeleton/head view
    msg = T.edit_file(ctx, RID, "src/A.java", "int x = 1;", "int x = 2;")
    assert "edited" in msg
    assert "int x = 2;" in (ws / "src" / "A.java").read_text()


def test_delete_clears_hash_state(ws):
    ctx = _ctx()
    T.read_file(ctx, RID, "src/A.java")
    T.delete_file(ctx, RID, "src/A.java")
    assert (RID, "src/A.java") not in ctx.read_hashes
