# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Slice 2 unit tests — `SdkAuthMiddleware` rejection paths.

Pure middleware tests against a stub Starlette app. No real SDK mount,
no real JSON-RPC handler. Confirms each error code (`missing_bearer_token`,
`invalid_token`, `session_unknown`, `session_revoked`, `partner_inactive`)
is returned with the matching 401.

Skipped cleanly when `a2a-sdk` isn't installed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest


pytest.importorskip("a2a")
pytest.importorskip("sqlalchemy")


@pytest.fixture
def db_session():
    """In-memory SQLite session with the phase_c models registered."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.core.database import Base
    import app.models.phase_c  # noqa: F401
    import app.models.change_request  # noqa: F401
    import app.models.user  # noqa: F401

    # StaticPool: a bare :memory: engine hands every connection its OWN empty
    # database, so the middleware's separately-opened session would see no
    # tables. One shared connection (+ cross-thread, for TestClient) fixes it.
    from sqlalchemy.pool import StaticPool
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        yield session, Session
    finally:
        session.close()
        engine.dispose()


def _build_test_client(db_session_fixture):
    """Build a Starlette TestClient with SdkAuthMiddleware wrapping a
    stub /rpc handler that just echoes 200."""
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from app.a2a_common.sdk_auth_middleware import SdkAuthMiddleware

    _, Session = db_session_fixture

    # Patch SessionLocal at its source module: the middleware imports it
    # lazily inside the request handler (`from app.core.database import
    # SessionLocal`, §3.3 lazy-import convention), so there is no
    # module-level attribute on sdk_auth_middleware to patch — the per-call
    # import reads the patched source attribute instead.
    patcher = patch("app.core.database.SessionLocal", Session)
    patcher.start()

    async def _rpc(request):
        # If we got here, auth passed. Confirm contextvar populated.
        from app.a2a_common.sdk_auth_middleware import AUTH_CONTEXT
        ctx = AUTH_CONTEXT.get()
        return JSONResponse({"ok": True, "partner_id": ctx.partner.id if ctx else None})

    app = Starlette(
        routes=[Route("/rpc", _rpc, methods=["POST"])],
        middleware=[Middleware(SdkAuthMiddleware)],
    )
    client = TestClient(app)
    return client, patcher


def _make_partner_and_session(db, *, jwt_hash, expires_in=3600, revoked=False, status=None):
    from app.models.phase_c import A2ASession, PartnerAgent, PartnerStatus

    p = PartnerAgent(
        id="p-1",
        name="Test Partner",
        partner_type=["bank"],
        api_key="k",
        api_key_hash="h",
        status=status or PartnerStatus.ACTIVE,
    )
    db.add(p)
    s = A2ASession(
        id="s-1",
        partner_id="p-1",
        jwt_token_hash=jwt_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        created_at=datetime.now(timezone.utc),
        revoked_at=datetime.now(timezone.utc) if revoked else None,
    )
    db.add(s)
    db.commit()
    return p, s


def test_missing_authorization_returns_401(db_session):
    client, patcher = _build_test_client(db_session)
    try:
        r = client.post("/rpc", json={})
        assert r.status_code == 401
        assert r.json()["error"] == "missing_bearer_token"
    finally:
        patcher.stop()


def test_malformed_token_returns_401(db_session):
    client, patcher = _build_test_client(db_session)
    try:
        r = client.post("/rpc", headers={"Authorization": "Bearer not-a-real-jwt"}, json={})
        assert r.status_code == 401
        assert r.json()["error"] == "invalid_token"
    finally:
        patcher.stop()


def test_session_unknown_returns_401(db_session):
    """JWT decodes fine but no A2ASession row exists for its hash."""
    from app.core.security import create_partner_token

    db, _ = db_session
    # Don't insert an A2ASession row; just create a partner.
    from app.models.phase_c import PartnerAgent, PartnerStatus
    db.add(PartnerAgent(
        id="p-1", name="X", partner_type=["bank"],
        api_key="k", api_key_hash="h", status=PartnerStatus.ACTIVE,
    ))
    db.commit()

    token = create_partner_token("p-1")
    client, patcher = _build_test_client(db_session)
    try:
        r = client.post("/rpc", headers={"Authorization": f"Bearer {token}"}, json={})
        assert r.status_code == 401
        assert r.json()["error"] == "session_unknown"
    finally:
        patcher.stop()


def test_revoked_session_returns_401(db_session):
    import hashlib
    from app.core.security import create_partner_token

    db, _ = db_session
    token = create_partner_token("p-1")
    jwt_hash = hashlib.sha256(token.encode()).hexdigest()
    _make_partner_and_session(db, jwt_hash=jwt_hash, revoked=True)

    client, patcher = _build_test_client(db_session)
    try:
        r = client.post("/rpc", headers={"Authorization": f"Bearer {token}"}, json={})
        assert r.status_code == 401
        assert r.json()["error"] == "session_revoked"
    finally:
        patcher.stop()


def test_inactive_partner_returns_401(db_session):
    import hashlib
    from app.core.security import create_partner_token
    from app.models.phase_c import PartnerStatus

    db, _ = db_session
    token = create_partner_token("p-1")
    jwt_hash = hashlib.sha256(token.encode()).hexdigest()
    _make_partner_and_session(db, jwt_hash=jwt_hash, status=PartnerStatus.SUSPENDED)

    client, patcher = _build_test_client(db_session)
    try:
        r = client.post("/rpc", headers={"Authorization": f"Bearer {token}"}, json={})
        assert r.status_code == 401
        assert r.json()["error"] == "partner_inactive"
    finally:
        patcher.stop()


def test_valid_token_passes_to_inner_app(db_session):
    """Happy path: middleware sets the contextvar; inner handler sees
    `AUTH_CONTEXT.get().partner.id == 'p-1'`."""
    import hashlib
    from app.core.security import create_partner_token

    db, _ = db_session
    token = create_partner_token("p-1")
    jwt_hash = hashlib.sha256(token.encode()).hexdigest()
    _make_partner_and_session(db, jwt_hash=jwt_hash)

    client, patcher = _build_test_client(db_session)
    try:
        r = client.post("/rpc", headers={"Authorization": f"Bearer {token}"}, json={})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["partner_id"] == "p-1"
    finally:
        patcher.stop()


def test_well_known_path_skips_auth(db_session):
    """`/.well-known/agent-card.json` must not require Bearer — clients
    discover the card before they have a token."""
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from app.a2a_common.sdk_auth_middleware import SdkAuthMiddleware

    async def _card(request):
        return JSONResponse({"name": "test"})

    app = Starlette(
        routes=[Route("/.well-known/agent-card.json", _card, methods=["GET"])],
        middleware=[Middleware(SdkAuthMiddleware)],
    )
    client = TestClient(app)
    r = client.get("/.well-known/agent-card.json")
    assert r.status_code == 200
    assert r.json()["name"] == "test"
