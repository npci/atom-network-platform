# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""export_zip — the pre-push developer download of the run's working trees.
Pure filesystem: fake workspace under tmp_path, no git / DB."""
import zipfile

import pytest

from app.agents.workspace_local import WorkspaceError, export_zip
from app.core.config import settings


def _fake_repo(root, files):
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    return tmp_path


def test_zips_all_repos_under_named_folders(ws):
    _fake_repo(ws / "run1" / "rid-a", {"pom.xml": "<project/>", "src/A.java": "class A {}"})
    _fake_repo(ws / "run1" / "rid-b", {"pom.xml": "<project/>"})
    out = export_zip("run1", {"rid-a": "network", "rid-b": "network-2.0"})
    try:
        names = set(zipfile.ZipFile(out).namelist())
    finally:
        out.unlink()
    assert names == {"network/pom.xml", "network/src/A.java", "network-2.0/pom.xml"}


def test_excludes_git_lease_and_build_output(ws):
    _fake_repo(ws / "r" / "rid", {
        "src/A.java": "x",
        ".git/config": "url = http://oauth2:SECRET@gitlab/network.git",
        ".git/FETCH_HEAD": "http://oauth2:SECRET@gitlab/network.git",
        ".lease": "r",
        "target/A.class": "bin",
        "mod/target/B.class": "bin",
        "node_modules/x/y.js": "js",
    })
    out = export_zip("r", {"rid": "network"})
    try:
        zf = zipfile.ZipFile(out)
        names = set(zf.namelist())
        blob = b"".join(zf.read(n) for n in names)
    finally:
        out.unlink()
    assert names == {"network/src/A.java"}
    assert b"SECRET" not in blob


def test_extra_files_written_at_archive_root(ws):
    _fake_repo(ws / "r" / "rid", {"pom.xml": "<project/>"})
    out = export_zip("r", {"rid": "network"}, extra_files={"network.changes.diff": "diff --git a/x b/x"})
    try:
        zf = zipfile.ZipFile(out)
        assert zf.read("network.changes.diff") == b"diff --git a/x b/x"
    finally:
        out.unlink()


def test_missing_workspace_raises(ws):
    with pytest.raises(WorkspaceError):
        export_zip("gone", {"rid": "network"})


def test_missing_repo_dir_is_skipped_not_fatal(ws):
    _fake_repo(ws / "r" / "rid-a", {"pom.xml": "<project/>"})
    out = export_zip("r", {"rid-a": "network", "rid-gone": "network-2.0"})
    try:
        assert set(zipfile.ZipFile(out).namelist()) == {"network/pom.xml"}
    finally:
        out.unlink()
