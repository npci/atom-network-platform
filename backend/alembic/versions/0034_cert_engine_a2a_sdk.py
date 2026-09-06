# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Backfill cert_engine partner rows to `protocol_version='a2a_sdk'`.

Slice 7 of the unified A2A SDK refactor.

Cert-agent has never exposed a hand-rolled `POST /a2a/tasks/send`
endpoint — it only mounts the SDK's JSON-RPC at `/a2a-rpc/rpc` (Slice 7
moved this from `/a2a/rpc`). So a cert_engine partner row sitting on
the column default `protocol_version='legacy'` would 404 on every
outbound call from the platform.

This migration finds every PartnerAgent whose `partner_type` JSON
contains `"cert_engine"` and flips its `protocol_version` to `'a2a_sdk'`.
Idempotent: rows already on `'a2a_sdk'` are unaffected; non-cert_engine
rows are unaffected.

Revision ID: 0034
Revises: 0033
Create Date: 2026-05-07
"""
from alembic import op


revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The `partner_type` column is JSON-typed (a list like ["bank"] or
    # ["bank","psp"] or ["cert_engine"]). PostgreSQL's `?` operator
    # tests JSONB membership; we cast first because the column was
    # declared `JSON` not `JSONB` in alembic 0008. The cast is cheap
    # and tolerant of either underlying type.
    op.execute(
        """
        UPDATE partner_agents
           SET protocol_version = 'a2a_sdk'
         WHERE protocol_version <> 'a2a_sdk'
           AND (partner_type::jsonb) ? 'cert_engine'
        """
    )


def downgrade() -> None:
    # Revert is best-effort — we can't tell which cert_engine rows had
    # legacy explicitly set vs picked up the column default. Flipping
    # them all back to 'legacy' breaks every cert_engine call (404 on
    # a non-existent endpoint), so we leave them on 'a2a_sdk' on
    # downgrade. Operators wanting a hard revert should set the column
    # back manually.
    pass
