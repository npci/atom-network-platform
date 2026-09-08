# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Object-level authorization for /changes/{change_id} — app/core/change_access.py.

What these protect:

  1. **The gate itself.** Creator and admin pass; another authenticated user is
     refused. Review teams get product-kit READS only, never writes, and never
     on the creator-only routers.
  2. **Coverage.** Every router serving `/changes/{change_id}` carries the
     dependency. This is the load-bearing test: the original defect was not a
     wrong check, it was five routers that never had one, and a per-route check
     is precisely what drifted. If someone adds a seventh change-scoped router
     and forgets, `test_every_change_scoped_router_is_guarded` fails.
  3. **Not vacuous.** A route with no `change_id` is untouched, so the gate
     cannot be "passing" merely because it refuses everything.
"""
import sys
import types

import pytest

pytest.importorskip("fastapi")

from fastapi import APIRouter, Depends, FastAPI          # noqa: E402
from fastapi.testclient import TestClient                # noqa: E402

from app.core import change_access                       # noqa: E402
from app.core.change_access import (                     # noqa: E402
    require_change_access, require_change_or_kit_access, require_workflow_role,
)
from app.models.user import UserRole                     # noqa: E402


class _User:
    def __init__(self, uid, role):
        self.id, self.role, self.is_active = uid, role, True


class _Change:
    def __init__(self, created_by):
        self.created_by = created_by


class _Db:
    """Returns a change owned by `owner`, or None to simulate a missing row."""

    def __init__(self, change):
        self._change = change

    def get(self, model, ident):
        return self._change

    def close(self):
        pass


def build(dep, *, caller, change, extra_route=False):
    app = FastAPI()
    router = APIRouter(dependencies=[Depends(dep)])

    @router.get("/changes/{change_id}/thing")
    def read(change_id: str):
        return {"read": change_id}

    @router.post("/changes/{change_id}/thing")
    def write(change_id: str):
        return {"wrote": change_id}

    if extra_route:
        @router.get("/unrelated")
        def unrelated():
            return {"ok": True}

    app.include_router(router)
    app.dependency_overrides[change_access._db] = lambda: _Db(change)
    return app, caller


@pytest.fixture
def patched(monkeypatch):
    """Swap session resolution for a settable caller; the JWT path is
    `get_current_user`'s job and is tested elsewhere."""
    holder = {"user": None}
    monkeypatch.setattr(change_access, "_resolve_user",
                        lambda conn, db: holder["user"])
    return holder


OWNER = _User("u-owner", UserRole.PRODUCT_MANAGER)
OTHER = _User("u-other", UserRole.PRODUCT_MANAGER)
ADMIN = _User("u-admin", UserRole.ADMIN)
REVIEWER = _User("u-sec", UserRole.INFOSEC_REVIEWER)
CHANGE = _Change(created_by="u-owner")


@pytest.mark.parametrize("user,expected", [
    (OWNER, 200),
    (ADMIN, 200),
    (OTHER, 403),
    (REVIEWER, 403),      # review teams do NOT get Phase-B/C/negotiation access
    (None, 401),
])
def test_creator_or_admin_only(patched, user, expected):
    app, _ = build(require_change_access, caller=user, change=CHANGE)
    patched["user"] = user
    r = TestClient(app).get("/changes/c1/thing")
    assert r.status_code == expected, r.text


def test_missing_change_is_404_not_403(patched):
    """A caller who may not see the change and one where it does not exist must
    not be distinguishable by anything other than the row's absence — but a
    genuine 404 still has to surface for the owner."""
    app, _ = build(require_change_access, caller=OWNER, change=None)
    patched["user"] = OWNER
    assert TestClient(app).get("/changes/nope/thing").status_code == 404


class TestProductKitRouter:
    """The kit router additionally lets review teams READ, mirroring the
    `_kit_doc and _can_read_all_changes` branch in change_requests.py."""

    def test_reviewer_may_read(self, patched):
        app, _ = build(require_change_or_kit_access, caller=REVIEWER, change=CHANGE)
        patched["user"] = REVIEWER
        assert TestClient(app).get("/changes/c1/thing").status_code == 200

    def test_reviewer_may_not_write(self, patched):
        app, _ = build(require_change_or_kit_access, caller=REVIEWER, change=CHANGE)
        patched["user"] = REVIEWER
        assert TestClient(app).post("/changes/c1/thing").status_code == 403

    def test_unrelated_user_still_refused(self, patched):
        app, _ = build(require_change_or_kit_access, caller=OTHER, change=CHANGE)
        patched["user"] = OTHER
        assert TestClient(app).get("/changes/c1/thing").status_code == 403


def test_routes_without_change_id_are_untouched(patched):
    """Guard against the matcher going vacuous — the gate must not be refusing
    everything, and must not gate routes it has no business gating."""
    app, _ = build(require_change_access, caller=None, change=CHANGE,
                   extra_route=True)
    patched["user"] = None                      # not even authenticated
    assert TestClient(app).get("/unrelated").status_code == 200


class TestWorkflowRole:
    """PM/PO/admin gate on privileged certification + negotiation decisions.

    Complements the ownership gate rather than duplicating it: ownership asks
    "is this your change?", this asks "may your role make this decision?".
    """

    @pytest.mark.parametrize("user,expected", [
        (_User("u", UserRole.PRODUCT_MANAGER), 200),
        (_User("u", UserRole.PRODUCT_OWNER), 200),
        (_User("u", UserRole.ADMIN), 200),
        (_User("u", UserRole.INFOSEC_REVIEWER), 403),
        (_User("u", UserRole.RISK_REVIEWER), 403),
        (_User("u", UserRole.TECH_LEAD), 403),
        (None, 401),
    ])
    def test_role_matrix(self, patched, user, expected):
        app, _ = build(require_workflow_role, caller=user, change=CHANGE)
        patched["user"] = user
        assert TestClient(app).post("/changes/c1/thing").status_code == expected

    def test_creator_who_is_a_reviewer_is_still_refused(self, patched):
        """The escalation the gate exists for: an infosec_reviewer who owns the
        change passes the OWNERSHIP check, and must still not sign off a
        certification waiver."""
        owning_reviewer = _User("u-owner", UserRole.INFOSEC_REVIEWER)
        patched["user"] = owning_reviewer
        app, _ = build(require_change_access, caller=owning_reviewer, change=CHANGE)
        assert TestClient(app).post("/changes/c1/thing").status_code == 200   # owns it
        app2, _ = build(require_workflow_role, caller=owning_reviewer, change=CHANGE)
        assert TestClient(app2).post("/changes/c1/thing").status_code == 403  # wrong role


GUARDS = {"require_change_access", "require_change_or_kit_access"}

# Modules whose change-scoped routes enforce access some OTHER valid way.
# Every entry is a claim that was checked by reading the module:
#   change_requests    — the reference implementation; `_is_creator_or_admin`
#                        inline on each route (this gate was extracted from it)
#   agentic, assignment_actions, clarifications, jobs
#                      — object-level checks inline
#   emergency_issues   — role-gated to PM/PO/admin
#   governance         — role-gated to AgenticUser / AdminUser
#   cert_simulator_sync, resolver, kit_publications
#                      — AdminUser only, so there is no lower-privilege caller
#                        to escalate from
#
# Adding a module here is a deliberate assertion that you read it and it is
# safe. The test exists so a NEW unguarded route cannot appear silently.
INLINE_ENFORCED = {
    "change_requests", "agentic", "assignment_actions", "clarifications",
    "jobs", "emergency_issues", "governance", "cert_simulator_sync",
    "resolver", "kit_publications",
}


def _walk(routes, prefix=""):
    """Yield (full_path, route) for every leaf route.

    Routers are NOT flattened in this FastAPI version — `include_router` leaves
    an `_IncludedRouter` wrapper whose children hang off
    `include_context.included_router`, and whose own `.path` is empty. Walking
    only `app.routes` therefore finds 9 routes instead of 360, and a coverage
    test built on it passes by discovering nothing. That is exactly how the
    first version of this test was silently vacuous, hence
    `test_route_walker_finds_the_real_routes` below.
    """
    for r in routes:
        if type(r).__name__ == "_IncludedRouter":
            ctx = r.include_context
            yield from _walk(ctx.included_router.routes, prefix + (ctx.prefix or ""))
        else:
            yield prefix + getattr(r, "path", ""), r


def _change_scoped():
    import app.main as appmod
    return [(p, r) for p, r in _walk(appmod.app.routes) if "{change_id}" in p]


def test_route_walker_finds_the_real_routes():
    """Anti-vacuity guard for the coverage test below.

    If a FastAPI upgrade changes the nesting again, `_walk` could quietly return
    almost nothing and `test_every_change_scoped_route_is_guarded` would pass
    while checking nothing at all.
    """
    routes = _change_scoped()
    assert len(routes) > 100, (
        f"only {len(routes)} change-scoped routes discovered — the walker is "
        "probably not descending into included routers any more"
    )


def test_every_change_scoped_route_is_guarded():
    """Every `/changes/{change_id}` route carries the gate, or its module is a
    justified INLINE_ENFORCED entry."""
    unguarded = []
    for path, route in _change_scoped():
        dep = getattr(route, "dependant", None)
        names = {getattr(d.call, "__name__", "") for d in dep.dependencies} if dep else set()
        if names & GUARDS:
            continue
        endpoint = getattr(route, "endpoint", None)
        module = getattr(endpoint, "__module__", "?").split(".")[-1]
        if module in INLINE_ENFORCED:
            continue
        methods = sorted(getattr(route, "methods", None) or ["WEBSOCKET"])
        unguarded.append(f"{methods} {path}  ({module})")

    assert not unguarded, (
        "change-scoped routes with neither the shared gate nor a justified "
        "INLINE_ENFORCED module:\n  " + "\n  ".join(sorted(unguarded))
    )
