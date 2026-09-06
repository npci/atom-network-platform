# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""SecretsProvider abstraction — Phase 1 of ADR-0002 (vault migration).

Closes S4 (`ARCHITECTURE_REVIEW_ACTIONS.md` — "Move secrets from
DB-with-Fernet to a vault/control plane with central rotation and
emergency revocation, tested") to the extent Phase 1 of ADR-0002 scopes:
introduces the interface and a dual-read path WITHOUT changing today's
behavior. `DbFernetSecretsProvider` is the default and simply wraps the
existing `partner.signing_secret` / `partner.jwt_signing_secret` /
`partner.api_key` column access that already exists — this file changes
NOTHING about runtime behavior until `VaultSecretsProvider` is
implemented and `settings.secrets_provider_backend` is switched.

Note on encryption (2026-08-25 update — see ADR-0002 "Alternatives
Considered" / Phase-0 intermediate step): the org cannot depend on a
cloud-hosted vault/KMS for privacy reasons, so rather than waiting on a
self-hosted Vault deployment (ADR-0002 Phase 2+, genuinely blocked on
infrastructure), the columns `DbFernetSecretsProvider` wraps are now
encrypted at rest using the SAME Fernet mechanism already relied on for
`app_configs` secrets (`core/app_config_sync.py`'s `encrypt_secret` /
`decrypt_secret`, same `CONFIG_ENCRYPTION_KEY` KEK, same `enc:v1:`
prefix convention) — see `core/encrypted_type.py`. This closes the
"stored in plaintext" gap with no new service, no new crypto
implementation, and no call-site changes: encryption/decryption happens
transparently at the ORM column level, so `DbFernetSecretsProvider`'s
`getattr`/`setattr` below (and every other direct column access in the
codebase, e.g. `sdk_hmac_middleware.py`, `a2a_client.py`,
`partners.py`) automatically gets it for free.

Why this ships as working code now rather than staying a pure design
(see docs/adr/ADR-0002-secrets-vault-migration.md):
--------------------------------------------------------------------------
Phase 1 explicitly does not require a Vault deployment to exist yet — it
is "introduce the interface and a dual-read path," which is exactly a
code-only, config-schema-only change with a working, tested default
implementation. Deferring even this preparatory step until a Vault
cluster is provisioned means every future call site touched by Phase 1's
"one-time refactor of read sites" ships un-battle-tested on day one of
the real migration. Shipping the interface now, with `DbFernetProvider`
as the only backend, lets Phase 1's refactor happen and be verified
today, leaving ONLY the `VaultSecretsProvider` implementation itself
(which genuinely does require a provisioned Vault target) as the
Phase-2-onward work blocked on infrastructure.
"""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


class SecretNotFoundError(Exception):
    """Raised when a requested secret path/reference has no value.
    Callers should treat this identically to "partner has no secret
    configured" — the same code path startup_validation.py already
    gates on for HMAC secrets."""


@dataclass(frozen=True)
class SecretRef:
    """A reference to a secret's location, independent of backend.

    For DbFernetSecretsProvider, `path` is the DB column name and
    `owner_id` is the partner_id. For VaultSecretsProvider (future),
    `path` becomes a Vault KV path and `owner_id` remains the partner_id
    for audit correlation — the CALLER's request shape never changes
    across backends, only what happens inside `get()`."""
    owner_id: str
    kind: str  # "hmac" | "jwt" | "api_key"


class SecretsProvider(Protocol):
    """The interface every read site should call through, per ADR-0002
    Phase 1: `secrets_provider.get_partner_secret(partner.id, "hmac")`
    instead of `partner.signing_secret` (a direct column access)."""

    def get_partner_secret(self, partner_id: str, kind: str) -> str | None:
        """Returns the secret value, or None if the partner has no
        secret of this kind configured (mirrors today's "column is
        NULL" semantics exactly — NOT the same as SecretNotFoundError,
        which is reserved for a backend-level lookup failure, e.g. a
        Vault path that should exist but doesn't)."""
        ...

    def set_partner_secret(self, partner_id: str, kind: str, value: str) -> None:
        """Writes a new secret value. For DbFernetSecretsProvider this is
        the existing Fernet-encrypt-and-store path. For
        VaultSecretsProvider (future) this becomes a Vault KV write —
        the call site (rotation endpoints) does not change shape."""
        ...


class DbFernetSecretsProvider:
    """Phase 1 default backend — wraps the Postgres-stored partner secret
    columns, which are now encrypted at rest via `EncryptedSecret`
    (`core/encrypted_type.py`), reusing the SAME Fernet mechanism already
    used for `app_configs` secrets (same KEK — `CONFIG_ENCRYPTION_KEY`,
    same `enc:v1:` prefix, same legacy-plaintext passthrough). This class
    contains NO encryption logic of its own — encryption/decryption
    happens transparently at the ORM column level (`getattr`/`setattr`
    below trigger it automatically), so this remains a thin adapter: the
    refactor at call sites, if/when it happens, is mechanical (swap the
    column access for a method call); behavior/return type is unchanged.
    """

    def __init__(self, db_session_factory):
        self._db_session_factory = db_session_factory

    def get_partner_secret(self, partner_id: str, kind: str) -> str | None:
        from app.models.phase_c import PartnerAgent  # local import avoids a
        # module-level circular import between core/ and models/
        db = self._db_session_factory()
        try:
            partner = db.get(PartnerAgent, partner_id)
            if partner is None:
                return None
            column = {
                "hmac": "signing_secret",
                "jwt": "jwt_signing_secret",
                "api_key": "api_key",
            }.get(kind)
            if column is None:
                raise ValueError(f"unknown secret kind: {kind!r}")
            return getattr(partner, column, None)
        finally:
            db.close()

    def set_partner_secret(self, partner_id: str, kind: str, value: str) -> None:
        from app.models.phase_c import PartnerAgent
        db = self._db_session_factory()
        try:
            partner = db.get(PartnerAgent, partner_id)
            if partner is None:
                raise SecretNotFoundError(f"no partner {partner_id}")
            column = {
                "hmac": "signing_secret",
                "jwt": "jwt_signing_secret",
                "api_key": "api_key",
            }.get(kind)
            if column is None:
                raise ValueError(f"unknown secret kind: {kind!r}")
            setattr(partner, column, value)
            db.commit()
        finally:
            db.close()


class VaultSecretsProvider:
    """ADR-0002 Phase 2+ backend — NOT implemented in this pass.

    Left as a documented stub rather than a working implementation
    because it requires a provisioned Vault (or equivalent) target this
    session cannot stand up — see ADR-0002's own Consequences section
    ("requires provisioning and operating a Vault cluster... an
    infrastructure decision for the platform's operators"). The shape
    below is what Phase 2 fills in once that target exists; it is here
    so the interface contract (constructor signature, method shape) is
    agreed in code review NOW, before the implementation is written
    against a moving target.
    """

    def __init__(self, vault_client, mount_path: str = "secret/atom"):
        self._client = vault_client
        self._mount_path = mount_path

    def get_partner_secret(self, partner_id: str, kind: str) -> str | None:
        raise NotImplementedError(
            "VaultSecretsProvider requires a provisioned Vault target — "
            "see docs/adr/ADR-0002-secrets-vault-migration.md Phase 2. "
            "Do not enable settings.secrets_provider_backend='vault' "
            "until this method is implemented and tested against a real "
            "Vault instance in a non-production environment first."
        )

    def set_partner_secret(self, partner_id: str, kind: str, value: str) -> None:
        raise NotImplementedError(
            "See get_partner_secret's docstring — same prerequisite."
        )


def get_secrets_provider(db_session_factory) -> SecretsProvider:
    """Factory — returns the configured backend. Default is
    DbFernetSecretsProvider (today's behavior, unchanged) unless an
    operator has explicitly opted into the (currently non-functional)
    Vault backend, which fails loudly rather than silently falling back
    — per security_architecture_skills.md §4.3's "fail fast instead of
    starting insecurely" rule, a misconfigured secrets backend should
    never silently degrade to a different backend than the operator
    requested."""
    from app.core.config import settings
    backend = getattr(settings, "secrets_provider_backend", "db_fernet")
    if backend == "db_fernet":
        return DbFernetSecretsProvider(db_session_factory)
    if backend == "vault":
        raise NotImplementedError(
            "settings.secrets_provider_backend='vault' is set, but "
            "VaultSecretsProvider is not yet implemented (ADR-0002 Phase 2 "
            "prerequisite: a provisioned Vault target). Revert to "
            "'db_fernet' until Phase 2 ships, or implement Phase 2 first."
        )
    raise ValueError(f"unknown secrets_provider_backend: {backend!r}")
