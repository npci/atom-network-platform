# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Composing the Phase B build+deploy invocation.

A live run failed with a build log containing exactly one line —
``bash: : No such file or directory`` — because the Build panel's script field
was empty AND ``PHASE_B_BUILD_SCRIPT`` was unset, so the runner composed
``bash '' master master``. The message points at a missing script rather than
at the real cause (no script was ever selected), which is the expensive kind of
error to debug. These pin the fallback chain and the refusal.
"""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.services.build_runner import _build_command


@pytest.fixture(autouse=True)
def _no_configured_default(monkeypatch):
    """Isolate from whatever the host's .env sets."""
    monkeypatch.setattr(settings, "phase_b_build_script", "", raising=False)


def test_request_supplied_script_wins(monkeypatch):
    monkeypatch.setattr(settings, "phase_b_build_script", "/opt/default.sh", raising=False)
    cmd = _build_command("master", "release/2.0", "/srv/scripts/nlln/build_and_deploy.sh")
    assert cmd == "bash /srv/scripts/nlln/build_and_deploy.sh master release/2.0"


def test_falls_back_to_the_configured_default(monkeypatch):
    monkeypatch.setattr(settings, "phase_b_build_script", "/opt/default.sh", raising=False)
    assert _build_command("master", "master", None) == "bash /opt/default.sh master master"


def test_no_script_anywhere_refuses_instead_of_running_bash_on_nothing():
    """The regression: `bash ''` must never be composed."""
    with pytest.raises(ValueError) as ei:
        _build_command("master", "master", None)
    msg = str(ei.value)
    assert "PHASE_B_BUILD_SCRIPT" in msg, "the message must name the setting to fix"
    assert "PHASE_B_SCRIPT_ROOT" in msg, "…and the other way to supply a script"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_whitespace_only_script_is_also_refused(blank, monkeypatch):
    monkeypatch.setattr(settings, "phase_b_build_script", blank, raising=False)
    with pytest.raises(ValueError):
        _build_command("master", "master", blank)


def test_branch_names_are_quoted_as_single_tokens():
    """Branch names come from the UI — they must not be able to add shell words."""
    cmd = _build_command("master; rm -rf /", "a b", "/srv/s.sh")
    assert "rm -rf" in cmd                       # present, but inert…
    assert cmd.count("bash") == 1
    assert cmd.startswith("bash /srv/s.sh '")    # …because it is one quoted token
