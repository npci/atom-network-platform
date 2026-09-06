# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""SCR finding #12 (Insufficiently Protected Credentials).

Round-trip test for the encrypt/decrypt helper functions inside
`alembic/versions/0130_encrypt_mfa_secret_at_rest.py`. That migration
duplicates the crypto logic from `app/core/mfa.py` (old scheme, read-only)
and `app/core/app_config_sync.py` (new scheme, write) inline rather than
importing `app.*` — matching this repo's migration convention (no migration
file imports application code; see 0129's docstring for the same rule) — so
this test loads the migration module directly via its file path and exercises
those helpers against the real `cryptography` library, independent of
`alembic`/`app` being importable.

This is the test that would have caught it if the manual re-implementation in
the migration had drifted from the real `mfa.py` / `app_config_sync.py`
functions it mirrors (wrong salt, wrong iteration count, wrong prefix, wrong
fallback order).
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

cryptography = pytest.importorskip("cryptography")

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic" / "versions" / "0130_encrypt_mfa_secret_at_rest.py"
)


def _load_migration_module():
    """Alembic version files are numeric-prefixed and not a normal importable
    package, so load by file path. Stub out `alembic` (this migration only
    uses `op`/`sa` at upgrade()/downgrade() call time, not at module import
    time — the helper functions under test here don't touch either)."""
    import types
    if "alembic" not in sys.modules:
        fake_alembic = types.ModuleType("alembic")
        fake_alembic.op = types.SimpleNamespace()
        sys.modules["alembic"] = fake_alembic
    spec = importlib.util.spec_from_file_location("migration_0130", _MIGRATION_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def migration():
    return _load_migration_module()


# ── Old scheme (mirrors app/core/mfa.py) ─────────────────────────────────────

def test_old_pbkdf2_scheme_round_trips(migration):
    secret_key = "a" * 32
    plaintext = "JBSWY3DPEHPK3PXP"  # a realistic base32 TOTP seed
    token = migration._old_fernet_pbkdf2(secret_key, "").encrypt(plaintext.encode()).decode()
    assert migration._decrypt_old(token, secret_key, "") == plaintext


def test_old_pbkdf2_scheme_prefers_dedicated_mfa_encryption_key(migration):
    from cryptography.fernet import Fernet
    dedicated_key = Fernet.generate_key().decode()
    plaintext = "SOMEOTPSEED1234"
    token = migration._old_fernet_pbkdf2("irrelevant-secret-key", dedicated_key).encrypt(
        plaintext.encode()).decode()
    # Decrypting with a DIFFERENT secret_key but the SAME dedicated key must
    # still work — mirrors mfa.py's "prefers mfa_encryption_key" rule.
    assert migration._decrypt_old(token, "different-secret-key", dedicated_key) == plaintext


def test_old_legacy_sha256_fallback_is_tried_second(migration):
    # A token encrypted with the OLD OLD scheme (pre-PBKDF2, plain SHA-256)
    # must still decrypt via the fallback path, exactly like mfa.py's own
    # decrypt_secret() does for pre-migration users.
    secret_key = "b" * 32
    plaintext = "LEGACYSEED7890"
    token = migration._old_fernet_legacy_sha256(secret_key).encrypt(plaintext.encode()).decode()
    assert migration._decrypt_old(token, secret_key, "") == plaintext


def test_old_scheme_matches_mfa_py_salt_and_iterations_exactly(migration):
    # Pin the exact KDF parameters mfa.py uses (salt=b"npci-mfa-totp-v2",
    # 600_000 iterations, SHA256, 32-byte key) by deriving independently here
    # and comparing ciphertext-compatibility (a token from one derivation must
    # decrypt under the other — i.e. they produce the SAME Fernet key).
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    from cryptography.fernet import Fernet

    secret_key = "c" * 40
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=b"npci-mfa-totp-v2", iterations=600_000)
    independently_derived_key = base64.urlsafe_b64encode(kdf.derive(secret_key.encode()))
    plaintext = "CROSSCHECK999"
    token = Fernet(independently_derived_key).encrypt(plaintext.encode()).decode()
    assert migration._decrypt_old(token, secret_key, "") == plaintext


def test_old_scheme_returns_none_for_undecryptable_token(migration):
    # Fail-open contract: a token neither key can open returns None rather
    # than raising, so the migration's per-row loop can skip it and log a
    # warning instead of crashing the whole migration.
    assert migration._decrypt_old("not-a-valid-fernet-token", "x" * 32, "") is None


# ── New scheme (mirrors app/core/app_config_sync.py) ─────────────────────────

def test_new_scheme_produces_enc_v1_prefix(migration):
    from cryptography.fernet import Fernet
    kek = Fernet.generate_key().decode()
    out = migration._new_encrypt("plaintext-seed", kek)
    assert out.startswith("enc:v1:")


def test_new_scheme_is_decryptable_by_the_real_app_config_sync_scheme(migration):
    """The whole point of the migration: a value it writes must be readable by
    the SAME decrypt_secret() that EncryptedSecret.process_result_value calls
    on every ORM read of User.mfa_secret going forward."""
    from cryptography.fernet import Fernet

    kek = Fernet.generate_key().decode()
    plaintext = "REALTOTPSEED555"
    stored = migration._new_encrypt(plaintext, kek)

    # Re-implement app_config_sync.decrypt_secret's core logic (can't import
    # app.* here without a live DB-backed Settings singleton) to confirm the
    # token this migration writes is exactly what that function expects.
    assert stored.startswith(migration._ENC_PREFIX)
    token = stored[len(migration._ENC_PREFIX):]
    decrypted = Fernet(kek.encode()).decrypt(token.encode()).decode()
    assert decrypted == plaintext


# ── End-to-end: old scheme in, new scheme out (the migration's actual job) ──

def test_full_migration_path_old_ciphertext_to_new_ciphertext(migration):
    secret_key = "d" * 32
    mfa_encryption_key = ""
    config_encryption_key = "e" * 32  # any 32-byte string; only used as Fernet.generate_key()-shaped below
    from cryptography.fernet import Fernet
    config_encryption_key = Fernet.generate_key().decode()

    plaintext = "END2ENDSEED42"
    old_token = migration._old_fernet_pbkdf2(secret_key, mfa_encryption_key).encrypt(
        plaintext.encode()).decode()

    # Step 1 (what upgrade() does per row): decrypt with the old scheme.
    recovered_plaintext = migration._decrypt_old(old_token, secret_key, mfa_encryption_key)
    assert recovered_plaintext == plaintext

    # Step 2: re-encrypt with the new scheme.
    new_token = migration._new_encrypt(recovered_plaintext, config_encryption_key)
    assert new_token.startswith("enc:v1:")

    # Step 3: confirm the new token decrypts back to the same plaintext under
    # the new KEK (what EncryptedSecret will do on every subsequent read).
    raw = new_token[len("enc:v1:"):]
    assert Fernet(config_encryption_key.encode()).decrypt(raw.encode()).decode() == plaintext
