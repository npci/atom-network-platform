# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""TOTP MFA primitives — secret generation, provisioning, code verification,
backup codes, and the short-lived `mfa_pending` token that bridges the
two-step login (password → OTP).

InfoSec phase 2. Backup codes are stored bcrypt-hashed. Nothing here writes
to the DB — callers persist the returned values.

SCR finding #12 (Insufficiently Protected Credentials) — `User.mfa_secret`
used to be encrypted via the `encrypt_secret()` / `decrypt_secret()` pair
below (a dedicated Fernet mechanism, keyed by `secret_key` or
`mfa_encryption_key`), applied manually at every call site in
`app/api/auth.py`. It is now `EncryptedSecret` (see `core/encrypted_type.py`),
the SAME mechanism used for every other secret column in this codebase
(`PartnerAgent.api_key`, `.jwt_signing_secret`, etc. — Fernet keyed by
`CONFIG_ENCRYPTION_KEY`), applied transparently at the ORM layer instead of
by caller discipline. `app/api/auth.py` now reads/writes
`user.mfa_secret` directly as plaintext; the column encrypts/decrypts on
its own. Existing rows were migrated in place by
`alembic/versions/0130_encrypt_mfa_secret_at_rest.py`.

`encrypt_secret()` / `decrypt_secret()` below are kept (not deleted) purely
as the reference implementation that migration 0130 replicates inline (it
cannot import `app.*` — see that file's docstring) to decrypt pre-migration
rows; they are no longer called anywhere in the running application.
"""
from __future__ import annotations

import base64
import hashlib
import io
import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import bcrypt
import pyotp
import jwt
from jwt import PyJWTError

from app.core.config import settings

ALGORITHM = "HS256"
MFA_PENDING_TTL_S = 5 * 60      # window to complete the OTP step after a good password
# The two things a bridge token may authorise. They are deliberately NOT
# interchangeable — see mfa_pending_token().
_MFA_PURPOSES = frozenset({"enroll", "verify"})
_BACKUP_CODE_COUNT = 10
_TOTP_VALID_WINDOW = 1          # ±1 step (±30s) clock-drift tolerance


# ── Secret encryption at rest ────────────────────────────────────────────────

@lru_cache(maxsize=4)
def _fernet_for(dedicated_key: str, secret_key: str):
    """Build (and CACHE) the Fernet cipher for a given key pair.

    CACHED DELIBERATELY. The PBKDF2 branch below runs 600k iterations, which
    measures ~340 ms per derivation. `_fernet()` is called on every
    encrypt/decrypt, so without memoisation every MFA enrolment and every OTP
    verification paid that cost on the request path — and `decrypt_secret`'s
    legacy fallback paid it twice. That is a CPU-exhaustion lever on a
    login-adjacent endpoint, so the derivation is cached on its inputs.

    Keying the cache on the actual key material (rather than caching a
    module-level singleton) means a runtime key rotation via the admin config
    sync produces a cache MISS and re-derives, instead of silently serving a
    cipher built from the old secret.

    `maxsize=4` is enough for the realistic set (current key, plus a previous
    one still in flight during a rotation) while bounding how much key material
    stays resident.
    """
    from cryptography.fernet import Fernet
    if dedicated_key:
        return Fernet(dedicated_key.encode())
    # PBKDF2-HMAC-SHA256 with 600k iterations (OWASP 2023 recommended minimum
    # for PBKDF2-HMAC-SHA256) and a static application-specific salt. The salt
    # is fixed so the key is deterministic — no DB migration needed, and
    # existing encrypted secrets remain decryptable. The old SHA-256-derived
    # key is tried as a transparent fallback in decrypt_secret.
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


def _fernet():
    """Fernet cipher for encrypting the TOTP seed at rest (InfoSec).

    Prefers `settings.mfa_encryption_key` — a real Fernet key from
    `Fernet.generate_key()` — when an operator has set one. This is additive
    and opt-in: unset (the default), the key is derived via PBKDF2-HMAC-SHA256
    with a static salt, replacing the previous single unsalted SHA-256 which
    was not a proper KDF and tied MFA-seed encryption to the same secret that
    signs every session JWT.

    The PBKDF2 derivation is deterministic (no salt stored alongside the
    ciphertext) so existing deployments' encrypted TOTP seeds remain
    decryptable — the old SHA-256-derived key is tried as a fallback on
    decryption failure, making the migration transparent. Rotating whichever
    key is actually in use invalidates stored secrets — users re-enrol
    (acceptable, rare), same as always.

    The expensive derivation lives in the cached `_fernet_for` helper; this
    wrapper only reads the current settings so a rotated key is picked up.
    """
    return _fernet_for(
        (settings.mfa_encryption_key or "").strip(),
        settings.secret_key,
    )


def encrypt_secret(plain: str) -> str:
    """No longer used to persist `User.mfa_secret` (see module docstring —
    that column is `EncryptedSecret` now). Kept as the reference
    implementation for anything that still needs this specific Fernet
    scheme (e.g. an offline/administrative script reading pre-migration-0130
    data directly)."""
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    """Decrypt a TOTP secret, with transparent fallback to the legacy
    SHA-256-derived key for existing encrypted secrets.

    No longer used to read `User.mfa_secret` (see module docstring — that
    column is `EncryptedSecret` now, decrypted transparently by the ORM).
    Kept as the reference implementation migration 0130 replicates inline.

    The primary key uses PBKDF2 (see _fernet). If that fails — because the
    secret was encrypted before the PBKDF2 migration — the old single-unsalted-
    SHA-256 key is tried as a fallback. This makes the migration transparent:
    existing encrypted secrets remain decryptable until the user re-enrols
    (which re-encrypts with the new key).
    """
    try:
        return _fernet().decrypt(token.encode()).decode()
    except Exception:  # noqa: BLE001 — fall back to legacy key
        pass
    return _legacy_fernet(settings.secret_key).decrypt(token.encode()).decode()


@lru_cache(maxsize=2)
def _legacy_fernet(secret_key: str):
    """Cipher for the pre-PBKDF2 key (single unsalted SHA-256).

    Cached for the same reason as `_fernet_for`: this is the fallback path, so
    it runs for every not-yet-re-enrolled user and would otherwise rebuild the
    cipher on each decrypt. SHA-256 is cheap, but the allocation is pointless
    and this keeps both paths consistent.
    """
    from cryptography.fernet import Fernet
    legacy_key = base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest())
    return Fernet(legacy_key)


# ── TOTP ─────────────────────────────────────────────────────────────────────

def generate_totp_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, username: str) -> str:
    """otpauth:// URI for an authenticator app (Google/Microsoft Authenticator)."""
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=settings.mfa_issuer)


def qr_png_b64(uri: str) -> str:
    """Render the provisioning URI as a base64 PNG QR code.

    Uses `segno`, NOT `qrcode`. The swap (2026-08-28, SBOM finding 1) was
    forced by package METADATA, not by a functional problem: qrcode 8.2 ships
    contradictory licence declarations —

        License: BSD
        Classifier: License :: OSI Approved :: BSD License
        Classifier: License :: Other/Proprietary License     <- this one

    — and its actual LICENSE file is a plain 3-clause BSD. Scanners read
    classifiers, not licence files, so the stray "Other/Proprietary" matched
    the banned-licence policy and scored threat 10, the highest in the whole
    report. We were being penalised for an upstream typo.

    segno declares a single licence classifier (OSI Approved :: BSD License)
    matching the 3-clause BSD LICENSE it ships, so there is nothing for a
    classifier-reading scanner to object to. Note it is BSD-3-Clause, not MIT
    as originally assumed — verified against the 1.6.6 wheel metadata. Both
    are permissive, so the finding closes regardless. Unlike qrcode it also
    has ZERO runtime dependencies on Python >= 3.10.

    THE OUTPUT IS DELIBERATELY MATCHED TO THE OLD ONE, because this feeds
    authenticator-app enrolment and a QR that scans differently locks users
    out of setting up MFA. Measured, not assumed — both libraries were run
    side by side against a representative otpauth:// URI:

        qrcode 8.2  : error M, box_size 10, border 4
                      -> QR version 6, 41 modules -> 490x490 PNG
        segno 1.6.6 : error="m", scale=10, border=4
                      -> QR version 6, 41 modules -> 490x490 PNG

    The arguments below are what reproduce that:
      - error="m"  matches qrcode's default ERROR_CORRECT_M. segno's own
                   default differs, and a different level changes the module
                   count (L gives 37, Q gives 49), so this is not optional.
      - scale=10   matches box_size=10 (10 device pixels per module).
      - border=4   matches qrcode's border=4 (the 4-module quiet zone the
                   QR spec requires; authenticator scanners rely on it).

    See tests/core/test_mfa_qr.py, which asserts both the pixel dimensions and
    the module count, so a future segno default change cannot silently shrink
    the symbol or trade module count against scale.

    segno writes PNG itself (pure Python, no Pillow on this path), so `save`
    takes kind="png" rather than PIL's format="PNG".
    """
    import segno
    buf = io.BytesIO()
    segno.make(uri, error="m").save(buf, kind="png", scale=10, border=4)
    return base64.b64encode(buf.getvalue()).decode()


def verify_totp(secret: str, code: str) -> bool:
    if not secret or not code:
        return False
    try:
        return pyotp.TOTP(secret).verify(code.strip(), valid_window=_TOTP_VALID_WINDOW)
    except Exception:  # noqa: BLE001 — malformed code/secret → not verified
        return False


# ── Backup (recovery) codes ──────────────────────────────────────────────────

def generate_backup_codes(n: int = _BACKUP_CODE_COUNT) -> tuple[list[str], list[str]]:
    """Return (plaintext_codes, bcrypt_hashes). Plaintext is shown to the user
    ONCE at enrolment; only the hashes are persisted."""
    plain = [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(n)]  # e.g. 'a1b2-c3d4'
    hashes = [bcrypt.hashpw(c.encode(), bcrypt.gensalt()).decode() for c in plain]
    return plain, hashes


def consume_backup_code(code: str, hashes: list[str] | None) -> tuple[bool, list[str]]:
    """If `code` matches a stored hash, return (True, remaining_hashes) with the
    matched hash removed (single-use); else (False, hashes unchanged)."""
    code = (code or "").strip().lower()
    hashes = list(hashes or [])
    if not code or not hashes:
        return False, hashes
    for i, h in enumerate(hashes):
        try:
            if bcrypt.checkpw(code.encode(), h.encode()):
                return True, hashes[:i] + hashes[i + 1:]
        except Exception:  # noqa: BLE001
            continue
    return False, hashes


# ── mfa_pending bridge token (scope-limited; NOT a session) ──────────────────

def mfa_pending_token(user_id: str, purpose: str) -> str:
    """Mint the short-lived bridge token handed out after a correct password.

    `purpose` is load-bearing and must be passed explicitly. Login reaches this
    function down two branches — the user is enrolled and owes us an OTP
    (``verify``), or the user is not enrolled and must register (``enroll``) —
    and both used to mint an IDENTICAL token. That let a caller holding only the
    password carry the token from the ``verify`` branch to ``/auth/mfa/setup``,
    which overwrites the live TOTP secret, then activate a factor of their own
    choosing: a full second-factor bypass off a password alone.

    The two are now distinct credentials, and `decode_mfa_pending` refuses to
    hand a ``verify`` token to an enrolment endpoint. The crossover is closed at
    the token layer rather than by a guard each endpoint has to remember.
    """
    if purpose not in _MFA_PURPOSES:
        raise ValueError(f"unknown mfa_pending purpose {purpose!r}")
    payload = {
        "sub": str(user_id),
        "scope": "mfa",          # distinguishes it from a full session token
        "purpose": purpose,      # 'enroll' | 'verify' — see docstring
        "exp": datetime.now(timezone.utc) + timedelta(seconds=MFA_PENDING_TTL_S),
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_mfa_pending(token: str, *, expected_purpose: str) -> str | None:
    """Return the user_id iff `token` is a valid, unexpired mfa-scope token that
    was minted for `expected_purpose`.

    A token carrying no ``purpose`` claim is rejected rather than grandfathered.
    The TTL is 5 minutes, so the only tokens that can lack it are ones minted by
    a process mid-rollout; refusing them costs one re-login and keeps the check
    fail-closed.
    """
    if expected_purpose not in _MFA_PURPOSES:
        raise ValueError(f"unknown mfa_pending purpose {expected_purpose!r}")
    try:
        claims = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except PyJWTError:
        return None
    if claims.get("scope") != "mfa":
        return None
    if claims.get("purpose") != expected_purpose:
        return None
    return claims.get("sub")
