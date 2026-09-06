# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""SQLAlchemy column type that transparently encrypts a value at rest,
reusing the EXACT Fernet mechanism already used for the ``app_configs``
table (see ``core/app_config_sync.py``) — same KEK (``CONFIG_ENCRYPTION_KEY``),
same ``enc:v1:`` prefix convention, same legacy-plaintext passthrough on
decrypt, same development-mode plaintext fallback when no key is set.

This is deliberately NOT a new encryption scheme. ``encrypt_secret`` /
``decrypt_secret`` are imported directly from ``app_config_sync`` so there
remains exactly one implementation of "how a secret is encrypted in this
codebase" — this module only adds the SQLAlchemy plumbing needed to apply
that existing implementation at the ORM column level instead of manually
at each read/write call site.

Why a TypeDecorator instead of routing everything through
``core/secrets_provider.py``: several call sites read
``partner.signing_secret`` / ``.jwt_signing_secret`` / ``.api_key`` as a
direct column access (``sdk_hmac_middleware.py``, ``a2a_client.py``,
``partners.py``, ``startup_validation.py``, ``celery_tasks.py``, ...).
Rewriting every one of those to go through a provider object would be a
much larger, riskier refactor than closing the actual gap, which is
simply "these columns are stored in plaintext." A TypeDecorator makes
encryption transparent at the point values enter/leave the database —
every existing column access, in every file, is encrypted at rest with
NO call-site changes and no behavioural change to any function's return
type (it is still a ``str | None``, exactly as before).

Backward compatibility (see ``app_config_sync.is_encrypted`` /
``decrypt_secret``):
  * Existing rows written before this change hold plain values with no
    ``enc:v1:`` prefix. ``decrypt_secret`` recognises this and returns
    them unchanged — no migration/backfill of existing data is required.
  * The next time such a row is written (e.g. a rotate-*-secret call),
    it is encrypted going forward.
  * In development with no ``CONFIG_ENCRYPTION_KEY`` set, values are
    stored/read as plaintext, identical to today's behaviour — nothing
    changes for local dev or the test suite's default configuration.
  * Outside development, writing a secret without ``CONFIG_ENCRYPTION_KEY``
    set raises (fail-closed) — this is the SAME rule already enforced for
    ``app_configs`` secrets, not a new constraint introduced by this file.
"""
from __future__ import annotations

from sqlalchemy.types import Text, TypeDecorator


class EncryptedSecret(TypeDecorator):
    """Transparently Fernet-encrypts/decrypts a column's value.

    Stored representation is ``enc:v1:<fernet-token>`` for values written
    after this was wired in, or legacy plaintext for values written
    before. ``impl = Text`` — the underlying column is widened to TEXT by
    migration so an encrypted token (larger than the original plaintext)
    always fits, matching how ``app_configs.value`` (which stores the
    same kind of ``enc:v1:`` token) is already declared as TEXT.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):  # Python -> DB
        if value is None or value == "":
            return value
        from app.core.app_config_sync import encrypt_secret
        return encrypt_secret(value)

    def process_result_value(self, value, dialect):  # DB -> Python
        if value is None or value == "":
            return value
        from app.core.app_config_sync import decrypt_secret
        return decrypt_secret(value)
