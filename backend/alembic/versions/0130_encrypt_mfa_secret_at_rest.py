# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""users.mfa_secret: widen to TEXT and re-encrypt under EncryptedSecret

SCR finding #12 (Insufficiently Protected Credentials). `users.mfa_secret`
was a plain `VARCHAR(255)` column: it WAS encrypted, but only via
application-level discipline at the call site (`app/api/auth.py` calling
`mfa.encrypt_secret()` / `mfa.decrypt_secret()` before every write/read),
using a separate Fernet mechanism (`app/core/mfa.py`, PBKDF2-derived from
`secret_key` or `mfa_encryption_key`) from every other secret in this
codebase (`PartnerAgent.api_key` / `.jwt_signing_secret` / etc., which use
`EncryptedSecret` — Fernet keyed by `CONFIG_ENCRYPTION_KEY`, `enc:v1:`
prefix, see `core/encrypted_type.py` / `core/app_config_sync.py`). Two
concrete risks that follows from that split:

  * nothing at the ORM/column level enforces encryption — a future code path
    that assigns `user.mfa_secret = raw_secret` directly (skipping
    `mfa.encrypt_secret`) would silently store plaintext, with no
    TypeDecorator to catch it;
  * two different KEKs protect "secrets at rest" in the same app, which is
    exactly the kind of drift a security review flags.

This migration closes the gap by moving `mfa_secret` onto `EncryptedSecret`
(same mechanism as every other secret column) and RE-ENCRYPTING existing
values in place: for each user with a non-null `mfa_secret`, decrypt with
the OLD mfa.py-specific Fernet key (replicated inline below — trying the
PBKDF2-derived key first, then the legacy unsalted-SHA-256 key, exactly
matching `app/core/mfa.py::decrypt_secret`'s own fallback order) and
re-encrypt with the `CONFIG_ENCRYPTION_KEY`-based `enc:v1:` scheme
(replicated inline below, matching `app/core/app_config_sync.py::encrypt_
secret`). Chosen over a forward-only column-type change + forced
re-enrollment (see remediation notes) specifically to avoid breaking MFA
for existing users — this migration is transparent to them.

Crypto logic is duplicated here rather than imported from `app.core.*`
because no migration in this repo imports application code (keeps
migrations runnable independent of app-layer refactors) — see e.g. 0129,
which applies the same "duplicate the encrypt/decrypt shape, don't import
it" convention for the same reason. `secret_key` / `mfa_encryption_key` /
`config_encryption_key` are read directly from `os.environ`, matching how
they reach the running app (both `alembic upgrade head` and `uvicorn` run
in the same container, with the same `env_file: ./backend/.env`).

Fail-open per-row: a row that can't be decrypted with EITHER legacy key
(e.g. `secret_key`/`mfa_encryption_key` genuinely lost, or a row that
was already plaintext from a pre-mfa.py-encryption era) is left
UNCHANGED rather than corrupted or dropped — it is logged so an operator
can investigate, and the user simply keeps whatever they had (worst case:
they re-enroll MFA, exactly like the "forward-only" alternative this
migration was chosen over, but only for that one row instead of everyone).

Idempotent: `enc:v1:`-prefixed values are already in the new scheme and are
skipped on a re-run.

Revision ID: 0130
Revises: 0129
Create Date: 2026-08-25
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger("alembic.runtime.migration")

revision = "0130"
down_revision = "0129"
branch_labels = None
depends_on = None

_TABLE = "users"
_COLUMN = "mfa_secret"
_ENC_PREFIX = "enc:v1:"


# ── OLD scheme (app/core/mfa.py) — read-only, used to decrypt existing rows ──

def _old_fernet_pbkdf2(secret_key: str, mfa_encryption_key: str):
    from cryptography.fernet import Fernet
    dedicated = (mfa_encryption_key or "").strip()
    if dedicated:
        return Fernet(dedicated.encode())
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"npci-mfa-totp-v2",
        iterations=600_000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(secret_key.encode()))
    return Fernet(key)


def _old_fernet_legacy_sha256(secret_key: str):
    from cryptography.fernet import Fernet
    legacy_key = base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest())
    return Fernet(legacy_key)


def _decrypt_old(token: str, secret_key: str, mfa_encryption_key: str) -> str | None:
    """Mirrors app/core/mfa.py::decrypt_secret's fallback order. Returns None
    (rather than raising) if neither key can decrypt this row."""
    try:
        return _old_fernet_pbkdf2(secret_key, mfa_encryption_key).decrypt(token.encode()).decode()
    except Exception:  # noqa: BLE001
        pass
    try:
        return _old_fernet_legacy_sha256(secret_key).decrypt(token.encode()).decode()
    except Exception:  # noqa: BLE001
        return None


# ── NEW scheme (app/core/app_config_sync.py) — used to re-encrypt ───────────

def _new_encrypt(plaintext: str, config_encryption_key: str) -> str:
    """Mirrors app/core/app_config_sync.py::encrypt_secret exactly."""
    from cryptography.fernet import Fernet
    f = Fernet(config_encryption_key.encode())
    return _ENC_PREFIX + f.encrypt(plaintext.encode()).decode()


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    existing = {c["name"]: c for c in insp.get_columns(_TABLE)}
    col = existing.get(_COLUMN)
    if col is None:
        return

    # Widen VARCHAR(255) -> TEXT first (same reasoning as 0129: an enc:v1:
    # Fernet token is wider than a base32 TOTP seed, and other enc:v1:
    # columns already use TEXT). No-op if already TEXT (idempotent / re-run
    # safe, matching 0129's own guard).
    if getattr(col["type"], "length", None) is not None:
        op.alter_column(
            _TABLE, _COLUMN,
            existing_type=sa.String(col["type"].length),
            type_=sa.Text(),
            existing_nullable=True,
        )

    config_encryption_key = (os.environ.get("CONFIG_ENCRYPTION_KEY") or "").strip()
    if not config_encryption_key:
        # Matches app_config_sync.encrypt_secret's own fail-closed rule
        # outside development: without a KEK we cannot write encrypted
        # values. Skip the data re-encryption (the column widening above
        # still applies) rather than block the whole migration — an
        # operator without CONFIG_ENCRYPTION_KEY set has bigger problems
        # than this one column (every other EncryptedSecret column, and
        # 0129's own migration, has the identical limitation).
        logger.warning(
            "0130: CONFIG_ENCRYPTION_KEY is not set — skipping mfa_secret "
            "re-encryption. Existing values are left as-is (still readable "
            "via the legacy mfa.py decrypt path until this is set and this "
            "data migration is re-run, which is idempotent)."
        )
        return

    secret_key = (os.environ.get("SECRET_KEY") or "").strip()
    mfa_encryption_key = (os.environ.get("MFA_ENCRYPTION_KEY") or "").strip()
    if not secret_key:
        logger.warning(
            "0130: SECRET_KEY is not set — cannot derive the legacy mfa.py "
            "decryption key, skipping mfa_secret re-encryption."
        )
        return

    users_table = sa.table(
        _TABLE,
        sa.column("id", sa.String),
        sa.column(_COLUMN, sa.Text),
    )
    rows = bind.execute(
        sa.select(users_table.c.id, users_table.c[_COLUMN])
        .where(users_table.c[_COLUMN].isnot(None))
        .where(users_table.c[_COLUMN] != "")
    ).fetchall()

    migrated, already_new, unreadable = 0, 0, 0
    for row in rows:
        user_id, stored = row[0], row[1]
        if stored.startswith(_ENC_PREFIX):
            already_new += 1  # idempotent re-run: already migrated
            continue
        plaintext = _decrypt_old(stored, secret_key, mfa_encryption_key)
        if plaintext is None:
            unreadable += 1
            logger.warning(
                "0130: could not decrypt mfa_secret for user_id=%s with either "
                "legacy key — leaving row unchanged. User will need to "
                "re-enroll MFA if this value is genuinely unreadable.",
                user_id,
            )
            continue
        new_value = _new_encrypt(plaintext, config_encryption_key)
        bind.execute(
            users_table.update()
            .where(users_table.c.id == user_id)
            .values({_COLUMN: new_value})
        )
        migrated += 1

    logger.info(
        "0130: mfa_secret re-encryption complete — migrated=%d already_new=%d "
        "unreadable=%d (of %d rows with a value)",
        migrated, already_new, unreadable, len(rows),
    )


def downgrade() -> None:
    # Not reversed automatically — same reasoning as 0129: rows may now hold
    # enc:v1: tokens under a different KEK than mfa.py's, so mechanically
    # "decrypt new, re-encrypt old" here would need the same crypto duplicated
    # in reverse for a rollback path this codebase has never needed. An
    # operator rolling back should treat this as "existing MFA-enrolled users
    # re-enroll," same as the forward-only alternative this migration avoided
    # for the upgrade path.
    pass
