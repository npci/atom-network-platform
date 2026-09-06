# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""partner_agents secret columns: widen to TEXT for Fernet-at-rest encryption

Closes ADR-0002's Phase-0 "intermediate step" (see Alternatives Considered:
"reduces blast radius of a DB compromise without requiring a new
operational product") and S4 (`ARCHITECTURE_REVIEW_ACTIONS.md`) using the
mechanism ALREADY in production for `app_configs` secrets (Fernet, KEK =
CONFIG_ENCRYPTION_KEY, `enc:v1:` prefix — see core/app_config_sync.py /
core/encrypted_type.py). No new encryption scheme, no Vault, no external
service — the model columns (`api_key`, `jwt_signing_secret`,
`signing_secret`, `previous_jwt_signing_secret`,
`previous_signing_secret`) are now wrapped in `EncryptedSecret`, which
calls the exact same encrypt_secret/decrypt_secret functions the
app_configs table already uses.

Only a column-width change: a Fernet token of a 64-char hex secret is
~190 chars, wider than the existing VARCHAR(128)/VARCHAR(200) columns,
so this widens them to TEXT (same type app_configs.value already uses
for the identical enc:v1: token shape) to avoid StringDataRightTruncation
on the first encrypted write.

Purely additive/widening — no data is altered, no backfill, no existing
value can fail to fit a wider column. Existing plaintext rows keep
working unchanged (decrypt_secret passes through anything without the
enc:v1: prefix, per its existing legacy-plaintext contract) until they
are next rotated, at which point they are written back out encrypted.

`api_key_hash` is untouched — it is looked up by equality (auth path)
and is already a one-way SHA-256 digest, the correct control for a
column that must support exact-match lookup.

Idempotent + inspector-gated (repo convention — see 0084/0106/0127).

Revision ID: 0129
Revises: 0128
Create Date: 2026-08-25
"""
from alembic import op
import sqlalchemy as sa

revision = "0129"
down_revision = "0128"
branch_labels = None
depends_on = None

_TABLE = "partner_agents"
_COLUMNS = [
    "api_key",
    "jwt_signing_secret",
    "signing_secret",
    "previous_jwt_signing_secret",
    "previous_signing_secret",
]


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    existing = {c["name"]: c for c in insp.get_columns(_TABLE)}
    for col_name in _COLUMNS:
        col = existing.get(col_name)
        if col is None:
            continue  # column doesn't exist on this deployment — nothing to widen
        # Already TEXT (no bounded length) — nothing to do. Guards re-runs
        # and deployments where this already landed.
        if getattr(col["type"], "length", None) is None:
            continue
        op.alter_column(
            _TABLE, col_name,
            existing_type=sa.String(col["type"].length),
            type_=sa.Text(),
            existing_nullable=True,
        )


def downgrade():
    # Not reversed automatically: by the time this runs, some rows may
    # hold `enc:v1:...` tokens longer than the original VARCHAR bound,
    # so narrowing would truncate/corrupt them. Rolling back requires an
    # operator to first decrypt-and-null encrypted rows.
    pass
