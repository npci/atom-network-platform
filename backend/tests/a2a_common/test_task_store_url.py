# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Unit tests for the sync→async DATABASE_URL translation.

Pure-function tests — no DB engine or SDK involvement. Confirms that
`get_task_store(database_url)` in production lands on the right async
driver regardless of whether ops happen to write the URL with or
without an explicit driver suffix.

Real Task lifecycle round-trip tests land in Slice 3 once an Executor
exists to drive the store.
"""
from __future__ import annotations

import pytest


pytest.importorskip("a2a")
pytest.importorskip("sqlalchemy")

from app.a2a_common.task_store_db import _to_async_url


def test_postgresql_bare_scheme():
    assert _to_async_url("postgresql://u:p@h:5432/d") == \
        "postgresql+asyncpg://u:p@h:5432/d"


def test_postgresql_with_psycopg2_explicit():
    assert _to_async_url("postgresql+psycopg2://u:p@h:5432/d") == \
        "postgresql+asyncpg://u:p@h:5432/d"


def test_postgresql_already_asyncpg_passes_through():
    asyncpg_url = "postgresql+asyncpg://u:p@h:5432/d"
    assert _to_async_url(asyncpg_url) == asyncpg_url


def test_sqlite_bare_scheme():
    assert _to_async_url("sqlite:///./local.db") == \
        "sqlite+aiosqlite:///./local.db"


def test_sqlite_already_aiosqlite_passes_through():
    aiosqlite_url = "sqlite+aiosqlite:///./local.db"
    assert _to_async_url(aiosqlite_url) == aiosqlite_url


def test_unknown_scheme_passes_through():
    """Pass-through for unknown schemes — a teammate using e.g. MariaDB
    can install asyncmy and prefix the URL themselves without hitting a
    hardcoded driver assertion in our factory."""
    weird = "mysql+aiomysql://u:p@h:3306/d"
    assert _to_async_url(weird) == weird


def test_query_string_preserved():
    """SSL params, schema search paths, etc. must survive the rewrite."""
    pg_with_ssl = "postgresql://u:p@h:5432/d?sslmode=require"
    assert _to_async_url(pg_with_ssl) == \
        "postgresql+asyncpg://u:p@h:5432/d?sslmode=require"
