# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
import jwt
from jwt import PyJWTError
import bcrypt
from app.core.config import settings

ALGORITHM = "HS256"


def _decode(token: str, secret: str) -> dict | None:
    """Verify an HS256 JWT and return its claims, or None if any check fails.

    Single funnel for every decode in this module. `secret` is REQUIRED and
    never defaulted: the outbound path verifies with a per-partner signing
    secret, and a `secret or settings.secret_key` fallback would silently
    promote a partner with an empty secret to platform-key trust.
    """
    try:
        return jwt.decode(token, secret, algorithms=[ALGORITHM])
    except PyJWTError:
        return None


# bcrypt's maximum input length. Enforced by the callers' validation
# (`api/auth.py::_validate_new_password`, `schemas/user.py::password_policy`)
# so the user gets a 422; repeated here as a defence-in-depth assertion because
# this function is the single funnel every password passes through, and a
# future caller that forgets to validate would otherwise either crash with an
# opaque 500 or — on a bcrypt build that truncates instead of raising — silently
# accept any password sharing the first 72 bytes.
BCRYPT_MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    encoded = password.encode()
    if len(encoded) > BCRYPT_MAX_PASSWORD_BYTES:
        raise ValueError(
            f"password exceeds bcrypt's {BCRYPT_MAX_PASSWORD_BYTES}-byte limit; "
            "validate length at the API boundary before hashing"
        )
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Check a password against its bcrypt hash. Returns False — never raises —
    for any input bcrypt cannot process.

    An over-long candidate is a FAILED login, not a server error: no stored hash
    can have been produced from it (`hash_password` refuses the same length), so
    returning False is both correct and the only safe answer. Letting bcrypt's
    ValueError escape would turn an unauthenticated login attempt into a 500,
    which is a trivially reachable error-noise vector on the login route.
    """
    encoded = plain_password.encode()
    if len(encoded) > BCRYPT_MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(encoded, hashed_password.encode())
    except ValueError:
        # Malformed/corrupt stored hash — treat as a non-match rather than a
        # crash, and let the caller's audit logging record the failed attempt.
        return False


def create_access_token(subject: Any, *, amr: str = "pwd") -> str:
    """Issue a session token. `amr` (Authentication Methods Reference, the
    same claim name OIDC uses for this purpose) records HOW the session was
    established: "pwd" (password only) or "pwd+mfa" (password + a
    verified OTP/backup code). Defaults to "pwd" so every existing call
    site that does not pass `amr=` keeps issuing exactly the token shape it
    always has — this is purely additive.

    Closes THREAT_MODEL.md T7 ("MFA not confirmed mandatory specifically
    for admin-privileged sessions") — `require_admin` (core/deps.py) reads
    this claim to enforce security_architecture_skills.md §8.4's "Human
    administrative access MUST require MFA" specifically for the ADMIN
    role, independent of whether `settings.mfa_enforced` (the
    platform-wide, all-users switch) happens to be on.
    """
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_expire_minutes
    )
    payload = {
        "sub": str(subject),
        "exp": expire,
        "jti": secrets.token_hex(16),
        "amr": amr,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> str | None:
    payload = _decode(token, settings.secret_key)
    return payload.get("sub") if payload else None


def decode_access_token_amr(token: str) -> str | None:
    """Returns the token's `amr` claim ("pwd" | "pwd+mfa"), or None if the
    token predates this claim (issued before this remediation) or is
    otherwise undecodable. A None/missing amr is treated as "pwd" (the
    conservative assumption — NOT "pwd+mfa") by every caller that checks
    this, so a pre-existing session cannot be silently treated as having
    completed MFA it never actually performed."""
    payload = _decode(token, settings.secret_key)
    return payload.get("amr") if payload else None


# ── A2A Partner Authentication ────────────────────────────────────────────────

# Slice 9 of A2A security hardening: short-TTL access tokens + long-TTL
# refresh tokens. Tightening the access TTL bounds the blast radius of a
# leaked token; the refresh token preserves session ergonomics so honest
# clients don't re-handshake every 15 min.
A2A_ACCESS_TOKEN_TTL_S  = 15 * 60      # 15 minutes (was 1 hour pre-Slice 9)
A2A_REFRESH_TOKEN_TTL_S = 24 * 60 * 60 # 24 hours


def create_partner_token(partner_id: str) -> str:
    """Create a short-lived A2A access JWT (15 min, post-Slice 9).

    Used INBOUND — issued by /a2a/auth so a partner can call this
    /a2a-rpc/rpc. Signed with the platform-wide `settings.secret_key`,
    matched by `decode_partner_token`. SdkAuthMiddleware is the verifier.
    """
    expire = datetime.now(timezone.utc) + timedelta(seconds=A2A_ACCESS_TOKEN_TTL_S)
    payload = {"sub": str(partner_id), "type": "a2a", "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_partner_token(token: str) -> tuple[str | None, str | None]:
    """Decode an A2A partner JWT. Returns (partner_id, token_type) or (None, None)."""
    payload = _decode(token, settings.secret_key)
    if payload is None:
        return None, None
    return payload.get("sub"), payload.get("type")


def create_partner_refresh_token(partner_id: str) -> str:
    """Create a long-lived A2A refresh JWT (24h).

    Slice 9 of A2A security hardening. Used by /a2a/auth/refresh to
    mint a new access token without re-doing the api_key handshake.
    Distinct `type=a2a_refresh` claim so an access token can never be
    smuggled in where a refresh is expected and vice versa.
    """
    expire = datetime.now(timezone.utc) + timedelta(seconds=A2A_REFRESH_TOKEN_TTL_S)
    payload = {"sub": str(partner_id), "type": "a2a_refresh", "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_partner_refresh_token(token: str) -> str | None:
    """Verify a refresh JWT and return partner_id, or None if invalid.

    Strict: the JWT must carry `type=a2a_refresh`. Returns None on
    signature failure, expiry, or wrong type so the caller can issue
    a single 401 path regardless of which check fired.
    """
    payload = _decode(token, settings.secret_key)
    if payload is None or payload.get("type") != "a2a_refresh":
        return None
    sub = payload.get("sub")
    return str(sub) if sub else None


# ── Slice 3 — outbound JWT minted with per-partner signing secret ─────────────

def create_partner_outbound_token(partner_id: str, signing_secret: str) -> str:
    """Mint a 1-hour HS256 JWT signed with the partner's per-partner
    signing secret.

    Used OUTBOUND — attached as `Authorization: Bearer <jwt>` by
    `app.services.a2a_client.send_task_to_partner` when calling a
    partner's /a2a-rpc/rpc. The partner verifies with the same secret
    stored on its side as the authority JWT secret.

    Distinct from `create_partner_token` (inbound, platform-wide
    secret_key, partner is consumer) — this signs with a per-partner
    secret so a leak from one partner's vault doesn't let an attacker
    impersonate the authority to every other partner.

    The cert_engine partner does NOT use this path; it shares the
    platform secret via the existing `fetch_bearer_jwt` handshake.
    """
    expire = datetime.now(timezone.utc) + timedelta(hours=1)
    payload = {
        "sub":  str(partner_id),
        "type": "a2a",
        "iss":  settings.jwt_issuer,
        "exp":  expire,
    }
    return jwt.encode(payload, signing_secret, algorithm=ALGORITHM)


def decode_partner_outbound_token(token: str, signing_secret: str) -> dict | None:
    """Verify a per-partner-signed JWT. Returns the claims dict or None.

    The receiver — the partner's auth middleware, in the partner platform's
    own repository   —
    uses this same shape; this helper is shared so test code on either
    side can round-trip without duplicating the signing math.
    """
    return _decode(token, signing_secret)
