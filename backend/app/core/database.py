# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from app.core.config import settings


# A13 (architecture review High #6, "Database Connection Pool Lacks Recycling
# and Timeout") — pool_size/max_overflow are externalised (were hardcoded
# literals, which the review flags as an anti-pattern), and
# pool_recycle/pool_timeout are added:
#   - pool_pre_ping alone catches a dead connection but pays a round-trip
#     PER CHECKOUT to do it; pool_recycle proactively retires a connection
#     before the DB server's own idle/wait_timeout can silently drop it,
#     which is what a long-lived Celery worker actually hits.
#   - pool_timeout bounds how long a checkout waits for a free connection
#     under saturation instead of blocking indefinitely (P10 —
#     "resource access has no timeout or limit" is a flagged anti-pattern).
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_recycle=settings.db_pool_recycle_s,
    pool_timeout=settings.db_pool_timeout_s,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass
