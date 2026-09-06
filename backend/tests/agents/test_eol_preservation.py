# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Line-ending preservation on agent writes — a 1-line edit must stay a 1-line diff.

Regression guard for the prod symptom on a ~25K-line CRLF (Windows-authored) Java
file: the edit tool read with universal newlines (CRLF→LF in memory) and wrote the
LF text back, so ONE surgical edit converted every line of the file — git showed
−24,789/+24,890 and the agent's actual change was undiscoverable. Writes now
re-expand to the file's own dominant EOL (edit_file, create_file, the XSD restore,
and the self-correction restore all route through write_preserving_eol).
"""
import subprocess
from pathlib import Path
from types import SimpleNamespace

from app.agents import agentic_orchestrator as O
from app.agents import agentic_tools as T
from app.agents import workspace_local as W


# ── Pure helpers ───────────────────────────────────────────────────────────────

def test_detect_crlf():
    assert W.detect_crlf(b"a\r\nb\r\n") is True
    assert W.detect_crlf(b"a\nb\n") is False
    assert W.detect_crlf(b"") is False
    assert W.detect_crlf(b"a\r\nb\r\nc\n") is True      # CRLF-majority
    assert W.detect_crlf(b"a\r\nb\nc\nd\n") is False    # LF-majority: one stray CRLF


def test_write_preserving_eol_matrix(tmp_path):
    crlf = tmp_path / "crlf.java"
    crlf.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    W.write_preserving_eol(crlf, "one\nTWO\nthree\n")
    assert crlf.read_bytes() == b"one\r\nTWO\r\nthree\r\n"

    lf = tmp_path / "lf.java"
    lf.write_bytes(b"one\ntwo\n")
    W.write_preserving_eol(lf, "one\nTWO\n")
    assert lf.read_bytes() == b"one\nTWO\n"

    new = tmp_path / "new.java"                          # nothing on disk → LF
    W.write_preserving_eol(new, "a\nb\n")
    assert new.read_bytes() == b"a\nb\n"

    mixed_in = tmp_path / "norm.java"                    # CRLF in the CONTENT is
    mixed_in.write_bytes(b"x\r\n")                       # normalized first — never \r\r\n
    W.write_preserving_eol(mixed_in, "x\r\nY\n")
    assert mixed_in.read_bytes() == b"x\r\nY\r\n"


# ── End-to-end: the 25K-line CRLF file through the REAL edit tool ─────────────

def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True, check=False)


def test_one_line_edit_on_25k_line_crlf_file_is_a_one_line_diff(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "workspace_root", lambda: tmp_path)
    rd = tmp_path / "ws1" / "repoA"
    rd.mkdir(parents=True)
    _git(rd, "init", "-q")
    _git(rd, "config", "user.email", "t@t"); _git(rd, "config", "user.name", "t")
    n = 25_000
    body = "".join(f"    private int field{i:07d} = {i};\r\n" for i in range(n))
    (rd / "Giant.java").write_bytes(f"class Giant {{\r\n{body}}}\r\n".encode())
    _git(rd, "add", "-A"); _git(rd, "commit", "-q", "-m", "base")

    ctx = T.RunContext(run_id="ws1", selected_repo_ids=["repoA"])
    ctx.read_files.add(("repoA", "Giant.java"))
    out = T.edit_file(ctx, "repoA", "Giant.java",
                      "    private int field0012345 = 12345;",
                      "    private long field0012345 = 12345L;   // agent change")
    assert out.startswith("edited")

    raw = (rd / "Giant.java").read_bytes()
    assert W.detect_crlf(raw) and b"\r\r" not in raw     # still CRLF, no doubling
    add_, del_, _p = _git(rd, "diff", "--numstat").stdout.split()
    assert (add_, del_) == ("1", "1")                     # ← the whole point

    # and the panel artifact agrees: one modified file, +1/−1, full preview
    art = O._capture_diffs(None, SimpleNamespace(id="ws1", workspace_run_id=None,
                                                 selected_repo_ids=["repoA"]))
    f = art["repoA"]["files"][0]
    assert (f["path"], f["add"], f["del"], f["truncated"]) == ("Giant.java", 1, 1, False)


def test_stale_edit_guard_survives_eol_preservation(tmp_path, monkeypatch):
    # read_file → edit_file → edit_file again must not false-positive the stale guard:
    # hashes are computed on LF-normalized text on every side of the write.
    monkeypatch.setattr(W, "workspace_root", lambda: tmp_path)
    rd = tmp_path / "ws1" / "repoA"
    rd.mkdir(parents=True)
    _git(rd, "init", "-q")
    (rd / "A.java").write_bytes(b"class A {\r\n    int x = 1;\r\n}\r\n")

    ctx = T.RunContext(run_id="ws1", selected_repo_ids=["repoA"])
    T.read_file(ctx, "repoA", "A.java")                   # records the seen-hash
    T.edit_file(ctx, "repoA", "A.java", "int x = 1;", "int x = 2;")
    out = T.edit_file(ctx, "repoA", "A.java", "int x = 2;", "int x = 3;")
    assert out.startswith("edited")
    assert (rd / "A.java").read_bytes() == b"class A {\r\n    int x = 3;\r\n}\r\n"


def test_create_file_normalizes_model_crlf(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "workspace_root", lambda: tmp_path)
    rd = tmp_path / "ws1" / "repoA"; rd.mkdir(parents=True)
    ctx = T.RunContext(run_id="ws1", selected_repo_ids=["repoA"])
    T.create_file(ctx, "repoA", "New.java", "class N {\r\n}\r\n")   # model emitted CRLF
    assert (rd / "New.java").read_bytes() == b"class N {\n}\n"      # stored LF, hash-consistent


def test_materialize_restore_keeps_target_eol(tmp_path, monkeypatch):
    # Phase-A XSD restore over a re-cloned base: the base file is CRLF, the stored
    # handoff content is LF-normalized — the restore must not flip the file's EOL.
    monkeypatch.setattr(W, "workspace_root", lambda: tmp_path)
    rd = tmp_path / "ws1" / "repoA"; rd.mkdir(parents=True)
    (rd / "network.xsd").write_bytes(b"<a>\r\n</a>\r\n")
    n = W.materialize_files("ws1", "repoA", [{"repo_id": "repoA", "path": "network.xsd",
                                              "content": "<a>\n  <new/>\n</a>\n"}])
    assert n == 1
    assert (rd / "network.xsd").read_bytes() == b"<a>\r\n  <new/>\r\n</a>\r\n"
