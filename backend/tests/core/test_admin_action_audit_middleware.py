# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""T8 (THREAT_MODEL.md) — regression tests for the generic admin action audit.

What these tests actually protect:

  1. **Coverage beyond `/api/admin/*`.** Admin-gated routes also live under
     `/api/users`, `/api/rag`, `/api/changes`, `/api/logs`. The middleware is
     keyed on the marker `require_admin` sets, not on a URL prefix, precisely
     so those are covered. A future refactor to prefix-matching would silently
     reopen T8's coverage gap — `test_records_admin_route_outside_admin_prefix`
     fails loudly if that happens.
  2. **No noise.** Reads, non-admin requests and failed actions must not be
     recorded, or the table stops being usable as a "what changed" trail.
  3. **No double-recording.** Endpoints that write a richer semantic row must
     not also get a generic one.
  4. **Fail-open.** An audit write failure must never affect the admin action's
     own response.
"""
#
# NOTE: this module deliberately does NOT use `from __future__ import
# annotations`. That future import turns annotations into strings, and FastAPI
# resolves route-parameter types from those annotations at decoration time —
# with it enabled, `req: Request` is no longer recognised as the request-object
# injection and is treated as a query parameter instead, so `req.state` is
# never populated and every "should be recorded" assertion silently fails for a
# reason that has nothing to do with the middleware. Keep it off here.
import sys
import types

import pytest

pytest.importorskip("fastapi")


@pytest.fixture
def harness(request):
    """Builds a FastAPI app wired with the middleware, capturing audit rows.

    `app.core.database` is stubbed because this dev environment's SQLite cannot
    accept the Postgres-only pool kwargs `core/database.py` passes to
    `create_engine` — a pre-existing environment limitation unrelated to the
    middleware under test. The middleware only needs `SessionLocal()` to return
    something closable.

    NOTE on the capture mechanism: `record` is replaced by direct assignment
    with an explicit finalizer, NOT via `monkeypatch.setattr`. `monkeypatch` is
    torn down when the FIXTURE that requested it goes out of scope, which
    happens before the test body runs — so a monkeypatched `record` would be
    restored to the real (DB-writing) implementation and no rows would ever be
    captured. This bit during development; keeping the note so it is not
    "simplified" back into a monkeypatch later.
    """
    if "app.core.database" not in sys.modules:
        stub = types.ModuleType("app.core.database")

        class _FakeSession:
            def close(self):
                pass

        stub.SessionLocal = lambda: _FakeSession()
        stub.engine = None

        class _Base:
            pass

        stub.Base = _Base
        sys.modules["app.core.database"] = stub

    from fastapi import FastAPI, HTTPException, Request
    from fastapi.testclient import TestClient

    from app.core import admin_action_audit
    from app.core.admin_action_audit_middleware import (
        AdminActionAuditMiddleware,
        mark_explicitly_recorded,
    )

    rows: list[dict] = []
    _real_record = admin_action_audit.record
    admin_action_audit.record = lambda db, **kw: rows.append(kw)
    request.addfinalizer(lambda: setattr(admin_action_audit, "record", _real_record))

    app = FastAPI()
    app.add_middleware(AdminActionAuditMiddleware)

    # NOTE: the route parameters below are named `req`, NOT `request` — the
    # pytest `request` fixture is already bound in this scope, and naming a
    # route parameter `request` shadows it in a way that silently breaks the
    # finalizer registration above. Keep them named `req`.
    def _as_admin(req: Request) -> None:
        """Stands in for `core/deps.py::require_admin`, which stamps these
        exact attributes once it has authorized an admin caller."""
        req.state.admin_audit_user_id = "admin-1"
        req.state.admin_audit_username = "alice"

    @app.post("/api/admin/partners/{pid}/rotate-key")
    def rotate(pid: str, req: Request):
        _as_admin(req)
        return {"ok": True}

    @app.delete("/api/users/{uid}")           # admin route OUTSIDE /api/admin
    def delete_user(uid: str, req: Request):
        _as_admin(req)
        return {"ok": True}

    @app.get("/api/admin/partners")           # read
    def list_partners(req: Request):
        _as_admin(req)
        return []

    @app.post("/api/public/thing")            # not admin-authorized
    def public_thing():
        return {"ok": True}

    @app.post("/api/admin/explicit")          # already wrote its own row
    def explicit(req: Request):
        _as_admin(req)
        mark_explicitly_recorded(req)
        return {"ok": True}

    @app.post("/api/admin/fails")             # action rejected
    def fails(req: Request):
        _as_admin(req)
        raise HTTPException(status_code=400, detail="nope")

    client = TestClient(app, raise_server_exceptions=False)
    return client, rows


class TestWhatGetsRecorded:
    def test_records_mutating_admin_action(self, harness):
        client, rows = harness
        client.post("/api/admin/partners/3f9a1c2b4d5e6f7a8b9c0d1e/rotate-key")
        assert len(rows) == 1
        row = rows[0]
        assert row["source"] == "middleware"
        assert row["http_method"] == "POST"
        assert row["path"] == "/api/admin/partners/3f9a1c2b4d5e6f7a8b9c0d1e/rotate-key"
        assert row["status_code"] == 200
        assert row["user_id"] == "admin-1"
        assert row["username"] == "alice"

    def test_records_admin_route_outside_admin_prefix(self, harness):
        """The whole reason this middleware keys on the admin dependency rather
        than a path prefix. If this ever fails, T8's coverage gap is back."""
        client, rows = harness
        client.delete("/api/users/42")
        assert len(rows) == 1
        assert rows[0]["path"] == "/api/users/42"

    def test_no_field_diff_on_generic_rows(self, harness):
        """The middleware cannot know what changed, so it must not invent a
        diff — `source` is what tells a reviewer this row is HTTP-level only."""
        client, rows = harness
        client.delete("/api/users/42")
        assert rows[0]["before"] is None
        assert rows[0]["after"] is None


class TestWhatIsSkipped:
    def test_reads_are_not_recorded(self, harness):
        client, rows = harness
        client.get("/api/admin/partners")
        assert rows == []

    def test_non_admin_requests_are_not_recorded(self, harness):
        client, rows = harness
        client.post("/api/public/thing")
        assert rows == []

    def test_explicitly_recorded_actions_are_not_duplicated(self, harness):
        client, rows = harness
        client.post("/api/admin/explicit")
        assert rows == []

    def test_failed_actions_are_not_recorded(self, harness):
        """A 4xx means the action did not take effect; recording it would mix
        'what changed' with 'what was attempted'."""
        client, rows = harness
        client.post("/api/admin/fails")
        assert rows == []


class TestFailOpen:
    def test_audit_failure_does_not_affect_the_response(self, harness):
        """An audit subsystem that can break the admin API is worse than a
        missing audit row — the write happens on the response path, after the
        action has already been committed."""
        client, _rows = harness
        from app.core import admin_action_audit

        def _explode(db, **kw):
            raise RuntimeError("simulated audit DB outage")

        # Same direct-assignment reasoning as the fixture (see its docstring):
        # restored in `finally` so the failure injection cannot leak into
        # another test.
        previous = admin_action_audit.record
        admin_action_audit.record = _explode
        try:
            resp = client.delete("/api/users/42")
        finally:
            admin_action_audit.record = previous
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


class TestActionNaming:
    def test_same_action_aggregates_across_resource_ids(self):
        """`action` must be queryable ("how many rotate-key calls this month"),
        so it must not embed the resource id."""
        from app.core.admin_action_audit_middleware import _action_name_for

        a = _action_name_for("POST", "/api/admin/partners/3f9a1c2b4d5e6f7a8b9c0d1e/rotate-key")
        b = _action_name_for("POST", "/api/admin/partners/999888777666555444333222/rotate-key")
        assert a == b == "admin.post.partners.rotate-key"

    def test_action_name_fits_the_column(self):
        from app.core.admin_action_audit_middleware import _action_name_for

        assert len(_action_name_for("POST", "/api/admin/" + "x1" * 200)) <= 80

    def test_resource_id_extracted_from_path(self):
        from app.core.admin_action_audit_middleware import _resource_id_from_path

        assert _resource_id_from_path(
            "/api/admin/partners/3f9a1c2b4d5e6f7a8b9c0d1e/rotate-key"
        ) == "3f9a1c2b4d5e6f7a8b9c0d1e"
        assert _resource_id_from_path("/api/users/42") == "42"
        assert _resource_id_from_path("/api/admin/config") is None
