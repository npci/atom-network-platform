# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

import os
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context

# Load app models so Alembic can autogenerate migrations
from app.core.database import Base
import app.models  # noqa: F401 — registers all models with Base

config = context.config

# Override sqlalchemy.url from environment variable if present.
# Supports DATABASE_URL (full connection string) or individual
# PGUSER / POSTGRES_USER, PGPASSWORD / POSTGRES_PASSWORD, PGHOST,
# PGPORT, PGDATABASE vars — matching the docker-compose convention.
database_url = os.environ.get("DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)
else:
    pg_user   = os.environ.get("PGUSER") or os.environ.get("POSTGRES_USER") or "atom_user"
    pg_pass   = os.environ.get("PGPASSWORD") or os.environ.get("POSTGRES_PASSWORD") or ""
    pg_host   = os.environ.get("PGHOST") or "localhost"
    pg_port   = os.environ.get("PGPORT") or "5432"
    pg_db     = os.environ.get("PGDATABASE") or os.environ.get("POSTGRES_DB") or "atom_cm_db"
    if pg_pass:
        constructed = f"postgresql://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_db}"
    else:
        constructed = f"postgresql://{pg_user}@{pg_host}:{pg_port}/{pg_db}"
    config.set_main_option("sqlalchemy.url", constructed)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
