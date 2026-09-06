# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Sync layer between the ``app_configs`` DB table and the runtime ``settings``.

Operator-tunable configuration (LLM provider/models, integration endpoints) and
secrets (API keys, tokens, passwords) are owned by the ``app_configs`` table and
edited through Admin -> Configuration. This module is the single place that:

  * encrypts / decrypts secret values at rest (Fernet), and
  * applies a stored DB value onto the in-memory ``settings`` singleton with the
    correct pydantic field type.

Both the startup loader (``app.main``) and the write path (``app.api.app_config``)
go through here so encryption and type-coercion never drift between them.

**Secret encryption.** Values for ``is_secret`` keys are stored as
``enc:v1:<fernet-token>`` in ``app_configs.value``. The key-encryption key (KEK)
comes from the ``CONFIG_ENCRYPTION_KEY`` env var and is the *only* secret that
must stay in ``.env`` -- you cannot bootstrap DB-stored secrets without it. The
``enc:v1:`` tag lets reads distinguish encrypted values from legacy plaintext
rows written before encryption existed (those decrypt to themselves), and leaves
room for a future ``v2`` key rotation.

**Type coercion.** The DB stores everything as text; a naive
``settings.__dict__[key] = row.value`` leaves int fields (``smtp_port``,
``gemini_thinking_budget``) holding strings. We coerce through the field's
declared pydantic type before assigning.
"""
from __future__ import annotations

import logging

from pydantic import TypeAdapter

from app.core.config import Settings, settings

logger = logging.getLogger(__name__)

_ENC_PREFIX = "enc:v1:"


# ── Encryption ──────────────────────────────────────────────────────────────

_fernet = None
_fernet_loaded = False


def _fernet_or_none():
    """Load (once) the Fernet cipher from CONFIG_ENCRYPTION_KEY, or None if unset/invalid."""
    global _fernet, _fernet_loaded
    if _fernet_loaded:
        return _fernet
    _fernet_loaded = True
    key = (settings.config_encryption_key or "").strip()
    if not key:
        _fernet = None
        return None
    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(key.encode())
    except Exception as e:  # malformed key
        logger.error("Invalid CONFIG_ENCRYPTION_KEY (%s) — secret encryption disabled", e)
        _fernet = None
    return _fernet


def encryption_available() -> bool:
    return _fernet_or_none() is not None


def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith(_ENC_PREFIX)


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret for storage, returning ``enc:v1:<token>``.

    Empty means "unset" and is stored as-is. Fails closed outside development:
    without a KEK we refuse to persist a plaintext secret. In development we warn
    and store plaintext so a fresh clone runs without operator-supplied keys.
    """
    if not plaintext:
        return ""
    if is_encrypted(plaintext):
        return plaintext  # already encrypted — don't double-wrap
    f = _fernet_or_none()
    if f is None:
        if (settings.app_env or "").lower() == "development":
            logger.warning(
                "CONFIG_ENCRYPTION_KEY not set — storing secret as PLAINTEXT (development only)"
            )
            return plaintext
        raise RuntimeError(
            "CONFIG_ENCRYPTION_KEY is required to store secrets when app_env != development"
        )
    return _ENC_PREFIX + f.encrypt(plaintext.encode()).decode()


def decrypt_secret(stored: str) -> str:
    """Decrypt a stored secret. Legacy plaintext (no ``enc:v1:`` tag) passes through."""
    if not stored:
        return ""
    if not is_encrypted(stored):
        return stored  # legacy plaintext row
    f = _fernet_or_none()
    if f is None:
        logger.error("Encrypted secret present but CONFIG_ENCRYPTION_KEY is not set")
        return ""
    from cryptography.fernet import InvalidToken
    try:
        return f.decrypt(stored[len(_ENC_PREFIX):].encode()).decode()
    except InvalidToken:
        logger.error("Failed to decrypt a secret — wrong CONFIG_ENCRYPTION_KEY?")
        return ""


# ── Type coercion + live application ────────────────────────────────────────

_adapters: dict[str, TypeAdapter] = {}


def coerce_setting(key: str, value):
    """Coerce a text value to the declared pydantic type of ``Settings.<key>``.

    Non-string fields (int/bool/...) would otherwise stay strings when applied
    from the DB. Empty values and unknown keys pass through unchanged.

    SCR #2 (Filtering Sensitive Logs): `value` here may be a DECRYPTED SECRET —
    `apply_setting_override` calls `decrypt_secret()` and hands the plaintext
    straight to `set_live_setting` -> here. The failure branch must therefore
    never render it. Two things would have leaked it: the `%r` of `value`, and
    pydantic's own message, which embeds the rejected input as
    `input_value='...'`.

    This is currently unreachable rather than exploitable — all 8 `is_secret`
    keys in `CONFIG_SCHEMA` are declared `str`, and `TypeAdapter(str)` does not
    reject a `str`. That is a property of today's schema, not of this function,
    and the first secret key declared `int`/`AnyUrl`/`SecretStr` would turn it
    into a live credential leak in the application log. Log the field name and
    the failing TYPE only; neither depends on the value.
    """
    if value == "" or value is None or key not in Settings.model_fields:
        return value
    adapter = _adapters.get(key)
    if adapter is None:
        adapter = _adapters[key] = TypeAdapter(Settings.model_fields[key].annotation)
    try:
        return adapter.validate_python(value)
    except Exception as e:
        logger.warning(
            "Could not coerce config %s to %s (%s) — keeping the raw value",
            key, Settings.model_fields[key].annotation, type(e).__name__,
        )
        return value


def set_live_setting(key: str, plaintext) -> None:
    """Apply a plaintext value onto the in-memory settings singleton, coerced to type.

    Empty/None is treated as "leave the current value" so clearing a DB row falls
    back to the .env default rather than blanking an int field.
    """
    if not hasattr(settings, key) or plaintext == "" or plaintext is None:
        return
    settings.__dict__[key] = coerce_setting(key, plaintext)


def apply_setting_override(key: str, stored_value: str, is_secret: bool):
    """Decrypt (if secret) a stored DB value and apply it onto ``settings``.

    Used by the startup loader. Returns the applied plaintext (or "" if empty).
    """
    plain = decrypt_secret(stored_value) if is_secret else stored_value
    set_live_setting(key, plain)
    return plain


def load_db_overrides() -> list[str]:
    """Apply DB-owned (admin-schema) config from ``app_configs`` onto ``settings``.

    Shared by the FastAPI startup hook AND the Celery worker-init signal so both
    processes see the same operator config — the codegen pipeline runs in the
    worker, which would otherwise see only .env. Only allowlisted schema keys are
    applied (infra / feature flags are never DB-overridable); secrets are
    decrypted; values are coerced to their field type. Fail-open. Returns the
    list of keys actually applied.
    """
    applied: list[str] = []
    try:
        from app.core.database import SessionLocal
        from app.models.app_config import AppConfig
        from app.api.app_config import CONFIG_SCHEMA  # lazy: avoids core<-api import cycle

        allowed = {c["key"]: c["is_secret"] for c in CONFIG_SCHEMA}
        db = SessionLocal()
        try:
            for row in db.query(AppConfig).all():
                if not row.value or row.key not in allowed:
                    continue
                try:
                    apply_setting_override(row.key, row.value, allowed[row.key])
                    applied.append(row.key)
                except Exception:
                    logger.exception("Failed to apply DB config override: %s", row.key)
        finally:
            db.close()
    except Exception as e:
        logger.warning("Could not load DB config overrides: %s", e)
    return applied
