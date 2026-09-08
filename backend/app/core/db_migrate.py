# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Locate this checkout's alembic setup, and optionally run it in-process.

Two callers, deliberately kept together so they can never disagree about WHICH
migration scripts are authoritative:

  * `app.core.startup_validation.validate_schema_up_to_date` reads the head
    revision to compare against the database, and
  * `app.main`'s startup event applies pending revisions when
    `auto_migrate_on_startup` is on (dev hot-reload stack only — see the
    setting's comment in config.py).

`script_location = alembic` in alembic.ini is resolved by Alembic against the
process CWD, which is /app in the container, backend/ for a native run, and the
rootdir under pytest. Every path here is derived from this module's own
location instead, so all three agree.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def alembic_config():
    """Alembic `Config` for this checkout, or None if it ships without one."""
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]      # …/backend
    ini, scripts = root / "alembic.ini", root / "alembic"
    if not ini.is_file() or not scripts.is_dir():
        return None
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(scripts))
    return cfg


def script_directory():
    """Alembic `ScriptDirectory` for this checkout, or None."""
    from alembic.script import ScriptDirectory

    cfg = alembic_config()
    return None if cfg is None else ScriptDirectory.from_config(cfg)


def current_heads(engine) -> set[str]:
    """Revision(s) the database is stamped with. Empty set when it carries no
    `alembic_version` row at all — a fresh database, or one built by
    `Base.metadata.create_all` as the test suite does."""
    from alembic.runtime.migration import MigrationContext

    with engine.connect() as conn:
        return set(MigrationContext.configure(conn).get_current_heads())


def upgrade_to_head() -> list[str]:
    """Apply pending revisions; return the ones that landed (empty if none).

    Alembic's env.py takes the URL from the DATABASE_URL environment variable,
    the same source the CLI uses, so this cannot drift onto a different database
    than `docker exec … alembic upgrade head` would touch.
    """
    from alembic import command

    from app.core.database import engine

    cfg = alembic_config()
    if cfg is None:
        raise RuntimeError(
            "auto_migrate_on_startup is on but alembic.ini/alembic are not present "
            "alongside app/ — nothing to run."
        )

    before = current_heads(engine)
    if before == set(script_directory().get_heads()):
        return []

    command.upgrade(cfg, "head")
    return sorted(current_heads(engine) - before)
