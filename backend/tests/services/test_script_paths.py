# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Request-supplied Phase B script paths — the allowlist-root contract.

The Build and UAT panels let an operator name WHICH script runs, so this
validation is the only thing between a request body and a subprocess. The
load-bearing cases are the escapes: `..` traversal and a symlink that points
outside the root must both reject AFTER resolution, not on string prefix.
"""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.services.script_paths import ScriptPathError, resolve_operator_script


@pytest.fixture
def root(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "nlln").mkdir()
    (scripts / "nlln" / "build.sh").write_text("#!/bin/bash\necho ok\n")
    (tmp_path / "outside.sh").write_text("#!/bin/bash\necho outside\n")
    monkeypatch.setattr(settings, "phase_b_script_root", str(scripts), raising=False)
    return scripts


def test_relative_path_resolves_inside_root(root):
    assert resolve_operator_script("nlln/build.sh") == (root / "nlln" / "build.sh").resolve()


def test_absolute_path_inside_root_is_accepted(root):
    p = str(root / "nlln" / "build.sh")
    assert resolve_operator_script(p) == (root / "nlln" / "build.sh").resolve()


def test_unset_root_disables_the_feature(monkeypatch):
    monkeypatch.setattr(settings, "phase_b_script_root", "", raising=False)
    with pytest.raises(ScriptPathError, match="not enabled"):
        resolve_operator_script("anything.sh")


def test_dotdot_traversal_is_rejected(root):
    with pytest.raises(ScriptPathError):
        resolve_operator_script("../outside.sh")


def test_absolute_path_outside_root_is_rejected(root):
    with pytest.raises(ScriptPathError):
        resolve_operator_script(str(root.parent / "outside.sh"))


def test_symlink_escaping_the_root_is_rejected(root):
    link = root / "sneaky.sh"
    link.symlink_to(root.parent / "outside.sh")
    with pytest.raises(ScriptPathError, match="escapes"):
        resolve_operator_script("sneaky.sh")


def test_symlink_staying_inside_the_root_is_fine(root):
    link = root / "alias.sh"
    link.symlink_to(root / "nlln" / "build.sh")
    assert resolve_operator_script("alias.sh") == (root / "nlln" / "build.sh").resolve()


def test_missing_file_is_rejected_without_leaking_the_root(root):
    with pytest.raises(ScriptPathError) as exc:
        resolve_operator_script("nlln/nope.sh")
    assert str(root) not in str(exc.value)


def test_directory_is_rejected(root):
    with pytest.raises(ScriptPathError):
        resolve_operator_script("nlln")


def test_non_sh_extension_is_rejected(root):
    (root / "run.py").write_text("print('x')\n")
    with pytest.raises(ScriptPathError, match=r"\.sh"):
        resolve_operator_script("run.py")


@pytest.mark.parametrize("raw", ["", "  ", "a\nb.sh", "a\x00b.sh", "x" * 501 + ".sh"])
def test_malformed_inputs_are_rejected(root, raw):
    with pytest.raises(ScriptPathError):
        resolve_operator_script(raw)
