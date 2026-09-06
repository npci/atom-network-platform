# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Opt-in stubs for optional third-party modules, for tests that only need
`app.*` to be importable.

Several suites import an `app` module whose transitive imports pull in packages
that are not installed in every environment (PyJWT, pgvector). They do not
exercise those packages — they just need the import to succeed. Historically
each test file inlined its own copy of the same stub block; this module is the
single home for them.

DELIBERATELY NOT A conftest.py FIXTURE. These must be installed into
`sys.modules` at import time, before the test module's own `from app...`
statements run, which a fixture cannot do. Just as importantly, a global
autouse stub would be WRONG: `tests/a2a_common/test_sdk_auth_middleware.py`
mints and verifies real tokens via `app.core.security.create_partner_token`,
so it needs the genuine PyJWT. Stubbing is therefore opt-in per file:

    from tests._optional_stubs import stub_jwt, stub_pgvector
    stub_jwt()
    stub_pgvector()

Every helper is idempotent and never displaces an already-imported real module,
so running the suite with the real dependencies installed is unaffected.
"""
from __future__ import annotations

import sys
import types


def stub_jwt(sub: str = "test", token: str = "token") -> None:
    """Install a no-op `jwt` module (PyJWT) if the real one is absent.

    Mirrors the surface `app.core.security`, `app.core.mfa` and `app.api.auth`
    touch: `encode`, `decode`, and the `PyJWTError` exception class that they
    import with `from jwt import PyJWTError`.
    """
    if "jwt" in sys.modules:
        return
    stub = types.SimpleNamespace(
        encode=lambda *_a, **_k: token,
        decode=lambda *_a, **_k: {"sub": sub},
    )
    stub.PyJWTError = Exception
    sys.modules["jwt"] = stub


def stub_pgvector() -> None:
    """Install a `pgvector.sqlalchemy` stub mapping Vector -> JSON.

    Lets SQLAlchemy models that declare pgvector columns be imported against
    SQLite, which has no vector type.
    """
    if "pgvector.sqlalchemy" in sys.modules:
        return
    import sqlalchemy as sa

    sqlalchemy_mod = types.SimpleNamespace(Vector=lambda *_a, **_k: sa.JSON())
    sys.modules["pgvector"] = types.SimpleNamespace(sqlalchemy=sqlalchemy_mod)
    sys.modules["pgvector.sqlalchemy"] = sqlalchemy_mod
