# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""workspace_local pure parts: clone-URL build + GC collectability (§6).
Real clone + GC-with-DB are covered by the S4 integration smoke."""
from datetime import timedelta

from app.agents.workspace_local import build_clone_url, _is_collectable
from app.models.agentic import AgenticRun, AgenticStatus
from app.models.base import utcnow


def test_build_clone_url_injects_token_and_rewrites_localhost():
    url = build_clone_url("http://localhost:8929", "root/network-core", "TOK")
    assert url == "http://oauth2:TOK@host.docker.internal:8929/root/network-core.git"


def test_build_clone_url_without_token():
    url = build_clone_url("https://gitlab.example.com", "g/r", "")
    assert url == "https://gitlab.example.com/g/r.git"


def _run(status, *, age_hours, lease_owner=None, lease_exp=None):
    r = AgenticRun(change_request_id="c", phase="x", status=status.value)
    r.updated_at = utcnow() - timedelta(hours=age_hours)
    r.lease_owner = lease_owner
    r.lease_expires_at = lease_exp
    return r


def test_orphan_dir_with_no_run_is_collectable():
    assert _is_collectable(None, utcnow(), 24) is True


def test_active_run_is_never_collected():
    assert _is_collectable(_run(AgenticStatus.ACTIVE, age_hours=999), utcnow(), 24) is False


def test_terminal_but_recent_is_kept():
    assert _is_collectable(_run(AgenticStatus.COMPLETED, age_hours=1), utcnow(), 24) is False


def test_terminal_old_and_lease_free_is_collected():
    assert _is_collectable(_run(AgenticStatus.FAILED, age_hours=48), utcnow(), 24) is True


def test_terminal_old_but_still_leased_is_kept():
    now = utcnow()
    held = _run(AgenticStatus.COMPLETED, age_hours=48,
                lease_owner="w1", lease_exp=now + timedelta(minutes=5))
    assert _is_collectable(held, now, 24) is False
