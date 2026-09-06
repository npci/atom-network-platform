# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""DB-backed `TaskStore` factory using the SDK's `DatabaseTaskStore`.

The SDK ships a complete SQLAlchemy-based `TaskStore`
(`a2a.server.tasks.database_task_store.DatabaseTaskStore`). We don't wrap
or reinvent it — we just provide a thin factory that:

  1. Translates the host's existing sync `DATABASE_URL` to the equivalent
     async-driver URL (asyncpg for Postgres, aiosqlite for SQLite).
  2. Lazy-creates a process-global `AsyncEngine` so every executor in the
     same process shares a single connection pool.
  3. Returns a `DatabaseTaskStore` configured to persist Tasks in a
     dedicated `a2a_tasks` table (default name `tasks` collides with too
     many things to be safe).

Required extras:
    backend (Postgres host):     `asyncpg>=0.29`     in requirements.txt
    partner platform (SQLite):   `aiosqlite>=0.19`   in its own repository

The first call to `get_task_store(database_url)` builds the engine. The
SDK's store does `CREATE TABLE IF NOT EXISTS` on first save/get when
`create_table=True`, so no alembic migration is required for this slice
— add one once the SDK schema stabilises if you want strict ownership.

Slice 2 ships only the factory + URL translation. Tests for the actual
Task lifecycle persistence land in Slice 3 once an Executor exists to
drive the store.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from a2a.server.tasks.database_task_store import DatabaseTaskStore


# Process-global engine — built lazily on first call. Each backend has
# exactly one DB; one engine per process keeps the pool budget small.
_engine: Optional[AsyncEngine] = None


def _to_async_url(database_url: str) -> str:
    """Convert a sync `DATABASE_URL` to its async-driver equivalent.

    The host's existing engines use psycopg2 (Postgres) or the default
    SQLite driver. The SDK requires async — we translate without
    duplicating connection-string config.

    Translations:
        postgresql://...          → postgresql+asyncpg://...
        postgresql+psycopg2://... → postgresql+asyncpg://...
        sqlite:///...             → sqlite+aiosqlite:///...
        anything already async    → unchanged

    Pass-through for unknown schemes so a teammate using e.g. MariaDB
    can install `asyncmy` and prefix the URL themselves without hitting
    a hardcoded driver assertion here.
    """
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url
    if database_url.startswith("postgresql+psycopg2://"):
        return database_url.replace(
            "postgresql+psycopg2://", "postgresql+asyncpg://", 1
        )
    if database_url.startswith("postgresql://"):
        return database_url.replace(
            "postgresql://", "postgresql+asyncpg://", 1
        )
    if database_url.startswith("sqlite+aiosqlite://"):
        return database_url
    if database_url.startswith("sqlite://"):
        return database_url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return database_url


def _get_async_engine(database_url: str) -> AsyncEngine:
    """Lazy-create the process-global async engine for the TaskStore.

    Pool sized small (5 + 2 overflow) — TaskStore writes are short
    transactions and we don't want to drain the host's main pool
    budget. `pool_pre_ping=True` survives DB restarts without forcing
    operators to bounce the backend.
    """
    global _engine  # noqa: PLW0603
    if _engine is None:
        _engine = create_async_engine(
            _to_async_url(database_url),
            pool_size=5,
            max_overflow=2,
            pool_pre_ping=True,
        )
    return _engine


def get_task_store(
    database_url: str,
    *,
    table_name: str = "a2a_tasks",
    create_table: bool = True,
) -> DatabaseTaskStore:
    """Build a `DatabaseTaskStore` for this backend.

    Args:
        database_url: The host's sync `DATABASE_URL`. Converted to the
                      async-driver equivalent internally; pass the same
                      string `app.core.database.engine` was built from.
        table_name:   Table the SDK persists Tasks in. Default
                      `a2a_tasks` to avoid colliding with the SDK's
                      generic `tasks` default — many apps already have
                      a `tasks` table.
        create_table: If True, the SDK runs `CREATE TABLE IF NOT EXISTS`
                      lazily on first save/get. Flip to False once an
                      alembic migration owns the schema.

    Returns:
        A `DatabaseTaskStore` ready to pass into
        `build_a2a_components(..., task_store=store)`. The SDK calls
        `initialize()` lazily, so explicit `await store.initialize()`
        is optional.
    """
    return DatabaseTaskStore(
        engine=_get_async_engine(database_url),
        create_table=create_table,
        table_name=table_name,
    )


async def reset_engine_for_tests() -> None:
    """Dispose the cached engine so a subsequent `get_task_store` call
    rebuilds it. Used by integration tests that swap DBs between cases.
    Production code should never need this.
    """
    global _engine  # noqa: PLW0603
    if _engine is not None:
        await _engine.dispose()
        _engine = None


__all__ = [
    "get_task_store",
    "reset_engine_for_tests",
]
