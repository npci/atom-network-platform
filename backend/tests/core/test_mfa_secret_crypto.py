# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Insufficiently protected credentials (CWE-522).

Pins the MFA secret crypto in `app/core/mfa.py`: which salt a fresh secret is
written under, which superseded salts must still open an existing row, and the
two paths where getting either wrong presents as "invalid MFA code" forever
rather than as an error anyone can see.

HISTORY. This file used to also test the inline encrypt/decrypt helpers inside
`alembic/versions/0130_encrypt_mfa_secret_at_rest.py`, loading that migration
by file path to check its hand-copied crypto had not drifted from `mfa.py`.
The 135 historical revisions were collapsed into a single `0001` baseline and
that migration no longer exists, so those tests went with it — there is no
longer a second copy of the logic that can drift. What remains here tests the
real `app/core/mfa.py` functions, which are still very much live: rows written
before 0130 ever ran are still out there, and `_LEGACY_KDF_SALTS` is still the
only thing that can open them.
"""
from __future__ import annotations

import pytest

cryptography = pytest.importorskip("cryptography")


def test_the_superseded_salt_still_decrypts():
    """A secret written under the superseded salt must still open.

    This assertion has flipped twice, so the reasoning belongs here rather than
    in a commit message. The salt was briefly dropped on the argument that its
    population is narrow — not "everyone with MFA" (the ORM reads
    `User.mfa_secret` through CONFIG_ENCRYPTION_KEY and never touches these
    salts) but only a row still holding a raw Fernet token from a 0130 upgrade
    run with that key unset.

    That argument is correct about the population and wrong about the
    consequence. `_as_totp_seed` exists precisely to repair those rows, and every
    one of them predates 0130 — so the superseded salt is the ONLY salt that can
    open them. Dropping it made the repair dead code for exactly the set it was
    written for, and the failure surfaces as "invalid MFA code" forever, not as
    an error anyone can see.

    A KDF salt is not a secret and not an identifier. It is a VALUE that keys
    rows already written, which this refactor's own rule pins rather than
    renames — the same reason table names and enum values were left alone.
    """
    import base64 as _b64

    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    from app.core import mfa
    from app.core.config import settings

    assert b"npci-mfa-totp-v2" in mfa._LEGACY_KDF_SALTS, (
        "the superseded salt was dropped again — every pre-0130 raw-Fernet row "
        "becomes permanently unreadable, presenting as a wrong OTP")

    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=b"npci-mfa-totp-v2", iterations=600_000)
    old_key = _b64.urlsafe_b64encode(kdf.derive(settings.secret_key.encode()))
    token = Fernet(old_key).encrypt(b"OLDSALTSEED123").decode()

    assert mfa.decrypt_secret(token) == "OLDSALTSEED123"
    # ...and `_as_totp_seed`, the caller that actually repairs such a row.
    assert mfa._as_totp_seed(token) == "OLDSALTSEED123"

    # DECRYPT-ONLY: the current salt is still what a fresh secret is written
    # under. `test_a_freshly_encrypted_secret_uses_the_current_salt` pins that.
    assert mfa.decrypt_secret(mfa.encrypt_secret("CURRENTSEED")) == "CURRENTSEED"


def test_a_freshly_encrypted_secret_uses_the_current_salt():
    """encrypt_secret must write under `_KDF_SALT`, never a legacy one —
    otherwise the fallback list would grow forever and never drain."""
    import base64 as _b64

    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    from app.core import mfa
    from app.core.config import settings

    token = mfa.encrypt_secret("FRESHSEED456")
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=mfa._KDF_SALT, iterations=600_000)
    current_key = _b64.urlsafe_b64encode(kdf.derive(settings.secret_key.encode()))
    assert Fernet(current_key).decrypt(token.encode()).decode() == "FRESHSEED456"


# ── Reviewer findings #2 and #3 — the two silent-lockout paths ───────────────

def test_verify_totp_accepts_a_row_migration_0130_left_encrypted(monkeypatch):
    """Finding #2: upgrading with CONFIG_ENCRYPTION_KEY unset leaves the
    PRE-0130 Fernet ciphertext in the column, and the ORM read path returns
    unprefixed values unchanged — so pyotp would be handed a ciphertext and
    every code would fail for every enrolled user, silently.
    """
    import pyotp

    from app.core import mfa

    seed = pyotp.random_base32()
    stored = mfa.encrypt_secret(seed)          # what the column actually holds
    assert stored.startswith("gAAAAA"), "precondition: this is a Fernet token"

    code = pyotp.TOTP(seed).now()
    assert mfa.verify_totp(stored, code) is True, \
        "an un-migrated ciphertext must still authenticate"
    # and a plaintext seed keeps working unchanged
    assert mfa.verify_totp(seed, pyotp.TOTP(seed).now()) is True


def test_derived_key_row_still_readable_after_a_dedicated_key_is_configured(monkeypatch):
    """Finding #3: enrol under the DERIVED key, then configure
    MFA_ENCRYPTION_KEY. The dedicated cipher cannot open that row; the fallback
    must still try the derived one, or the secret is permanently unreadable.

    Exercised with the CURRENT salt now that the superseded one is gone. The
    finding was never about which salt — it is about the derived-key walk
    running at all once a dedicated key is configured.
    """
    import base64 as _b64

    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    from app.core import mfa
    from app.core.config import settings

    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=mfa._KDF_SALT, iterations=600_000)
    derived = _b64.urlsafe_b64encode(kdf.derive(settings.secret_key.encode()))
    token = Fernet(derived).encrypt(b"ENROLLEDBEFORE").decode()

    monkeypatch.setattr(settings, "mfa_encryption_key", Fernet.generate_key().decode())
    assert mfa.decrypt_secret(token) == "ENROLLEDBEFORE"
