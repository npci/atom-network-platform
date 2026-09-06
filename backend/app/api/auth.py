# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

import base64
import hashlib
import logging
import re
import secrets
import time

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
from jwt import PyJWTError
from pydantic import BaseModel
from sqlalchemy import select
from app.core import mfa, ldap_auth, auth_audit
from app.core.config import settings
from app.core.deps import DbDep, CurrentUser, AdminUser
from app.core.security import (
    ALGORITHM, verify_password, create_access_token, hash_password, decode_access_token,
)
from app.core.session_cookie import (
    COOKIE_NAME as _SESSION_COOKIE_NAME,
    clear_session_cookie, extract_token, set_session_cookie,
)
from app.models.user import User, UserRole, UserRoleAssignment
from app.schemas.auth import (
    LoginRequest, TokenResponse, LoginResult,
    MfaSetupResponse, MfaActivateRequest, MfaActivateResponse,
    MfaVerifyRequest, MfaVerifyResponse, MfaDisableRequest,
)
from app.schemas.user import UserResponse, SwitchRoleRequest

logger = logging.getLogger(__name__)


def _body_token(token: str) -> str | None:
    """The value to put in a login response's ``access_token`` field.

    Returns None by default so the raw JWT never reaches the response body:
    the session is delivered as an httpOnly cookie on the same response, and a
    body copy is exactly what let the SPA put the credential back into
    JavaScript-reachable storage. Suppressing it is what makes the cookie the
    ONLY way a browser holds the session.

    ``AUTH_RETURN_TOKEN_IN_BODY=true`` restores the old behaviour for a
    non-browser caller that cannot keep a cookie jar. It is an escape hatch,
    not a supported browser configuration.
    """
    return token if settings.auth_return_token_in_body else None


router = APIRouter(prefix="/auth", tags=["auth"])


def _mask_username(username: str) -> str:
    """Mask a username for logging — show first 2 chars + '***'."""
    if not username:
        return "***"
    if len(username) <= 4:
        return username[:1] + "***"
    return username[:2] + "***"


# ── Redis-backed JWT denylist ────────────────────────────────────────────────

_REVOKE_PREFIX = "jwt:revoked:"
_redis_client = None
_redis_checked = False


def _get_redis():
    """Lazy-init Redis client. Returns None if Redis is unavailable."""
    global _redis_client, _redis_checked
    if _redis_checked:
        return _redis_client
    try:
        import redis
        _redis_client = redis.from_url(settings.redis_url, decode_responses=True)
        _redis_client.ping()
    except Exception as e:
        logger.warning("Token revocation: Redis unavailable (%s) — denylist disabled", e)
        _redis_client = None
    _redis_checked = True
    return _redis_client


def _token_jti(token: str) -> str:
    """Extract jti from a JWT, or fall back to a SHA-256 hash of the token."""
    try:
        claims = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        jti = claims.get("jti")
        if jti:
            return jti
    except PyJWTError:
        pass
    return hashlib.sha256(token.encode()).hexdigest()


def _token_remaining_ttl(token: str) -> int:
    """Seconds until the token's exp claim; 0 if already expired or unparseable."""
    try:
        claims = jwt.decode(
            token, settings.secret_key, algorithms=[ALGORITHM],
            options={"verify_exp": False},
        )
        exp = claims.get("exp", 0)
        remaining = int(exp - time.time())
        return max(remaining, 0)
    except PyJWTError:
        return 0


def revoke_token(token: str) -> None:
    """Add a token to the Redis denylist with a TTL matching its remaining lifetime."""
    r = _get_redis()
    if r is None:
        return
    jti = _token_jti(token)
    ttl = _token_remaining_ttl(token)
    if ttl <= 0:
        return
    try:
        r.setex(f"{_REVOKE_PREFIX}{jti}", ttl, "1")
    except Exception as e:
        logger.warning("Failed to revoke token: %s", e)


def is_token_revoked(token: str) -> bool:
    """Check whether a token's jti/hash is in the Redis denylist."""
    r = _get_redis()
    if r is None:
        return False
    jti = _token_jti(token)
    try:
        return r.exists(f"{_REVOKE_PREFIX}{jti}") > 0
    except Exception:
        return False


# ── Brute-force login protection ─────────────────────────────────────────────

_LOGIN_FAIL_PREFIX = "login:fail:"


def _lockout_key(username: str) -> str:
    return f"{_LOGIN_FAIL_PREFIX}user:{username}"


def _lockout_ip_key(ip: str) -> str:
    return f"{_LOGIN_FAIL_PREFIX}ip:{ip}"


def _check_login_lockout(username: str, ip: str) -> None:
    """Raise 429 if the username or IP is locked out due to repeated failures."""
    r = _get_redis()
    if r is None:
        return
    try:
        user_fails = int(r.get(_lockout_key(username)) or 0)
        ip_fails = int(r.get(_lockout_ip_key(ip)) or 0)
    except Exception:
        return

    if user_fails >= 10 or ip_fails >= 10:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Try again in 5 minutes.",
        )
    if user_fails >= 5 or ip_fails >= 5:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Try again in 60 seconds.",
        )


def _record_login_failure(username: str, ip: str) -> None:
    """Increment failure counters for username and IP."""
    r = _get_redis()
    if r is None:
        return
    try:
        pipe = r.pipeline()
        user_key = _lockout_key(username)
        ip_key = _lockout_ip_key(ip)

        pipe.incr(user_key)
        pipe.incr(ip_key)
        results = pipe.execute()

        user_count = results[0]
        ip_count = results[1]

        # Set TTL based on failure count — 5min lockout after 10 failures,
        # 60s lockout after 5 failures.
        user_ttl = 300 if user_count >= 10 else 60
        ip_ttl = 300 if ip_count >= 10 else 60

        pipe2 = r.pipeline()
        pipe2.expire(user_key, user_ttl)
        pipe2.expire(ip_key, ip_ttl)
        pipe2.execute()
    except Exception as e:
        logger.warning("Failed to record login failure: %s", e)


def _reset_login_failures(username: str, ip: str) -> None:
    """Clear failure counters on successful login."""
    r = _get_redis()
    if r is None:
        return
    try:
        r.delete(_lockout_key(username), _lockout_ip_key(ip))
    except Exception:
        pass


# Standard password policy — 8..72 chars + ≥1 letter + ≥1 digit. Applied
# to change-password (and recommended for any future user-create flow
# that doesn't already enforce its own policy).
_HAS_LETTER = re.compile(r"[A-Za-z]")
_HAS_DIGIT  = re.compile(r"\d")

# bcrypt operates on at most 72 BYTES. The installed backend refuses a longer
# input outright (`ValueError: password cannot be longer than 72 bytes`) rather
# than truncating, so without this ceiling a long password reaches
# `hash_password` and surfaces as an unhandled 500 — a validation failure
# reported as a server fault, and an easy way for any authenticated user to
# generate error noise. Older/other bcrypt builds truncate silently instead,
# which is worse: two different long passwords sharing a 72-byte prefix would
# both verify. Rejecting at the boundary is correct under either behaviour.
#
# The limit is on BYTES, not characters: a non-ASCII password can be well under
# 72 characters and still exceed 72 bytes once UTF-8 encoded.
_MAX_PASSWORD_BYTES = 72


def _validate_new_password(pw: str) -> None:
    """Raise HTTPException(422) if `pw` doesn't satisfy the policy."""
    if len(pw) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    if len(pw.encode("utf-8")) > _MAX_PASSWORD_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"Password must be at most {_MAX_PASSWORD_BYTES} bytes "
                   "(non-ASCII characters count as more than one byte)",
        )
    if not _HAS_LETTER.search(pw):
        raise HTTPException(status_code=422, detail="Password must contain at least one letter")
    if not _HAS_DIGIT.search(pw):
        raise HTTPException(status_code=422, detail="Password must contain at least one digit")


# ── Self-hosted login CAPTCHA (InfoSec mandate) ──────────────────────────────
# GET /auth/captcha mints a single-use image challenge; the answer is held in
# Redis (short TTL) and /auth/login verifies it before touching credentials.
# Verification fails CLOSED when Redis is unavailable — disable via
# settings.captcha_enabled if that ever needs to be bypassed.

_CAPTCHA_PREFIX = "login:captcha:"
# Unambiguous alphabet — no 0/O, 1/I/L which are hard to read in a distorted image.
_CAPTCHA_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _issue_captcha() -> tuple[str, str]:
    """Mint a CAPTCHA. Returns (challenge_id, image_b64_png), or ('', '') when
    Redis is unavailable (fail-closed — no store means no verifiable challenge)."""
    r = _get_redis()
    if r is None:
        return "", ""
    challenge_id = secrets.token_urlsafe(16)
    answer = "".join(secrets.choice(_CAPTCHA_ALPHABET) for _ in range(settings.captcha_length))
    try:
        r.setex(f"{_CAPTCHA_PREFIX}{challenge_id}", settings.captcha_ttl_seconds, answer)
    except Exception as e:  # noqa: BLE001
        logger.warning("captcha store failed: %s", e)
        return "", ""
    from captcha.image import ImageCaptcha  # lazy — keeps Pillow off the import path when unused
    png = ImageCaptcha(width=200, height=70).generate(answer).getvalue()
    return challenge_id, base64.b64encode(png).decode()


def _verify_captcha(challenge_id: str | None, answer: str | None) -> bool:
    """True iff `answer` matches the stored challenge (case-insensitive). The
    challenge is single-use: the key is deleted on any attempt. Fails CLOSED when
    Redis is down or the id/answer is missing."""
    r = _get_redis()
    if r is None or not challenge_id or not answer:
        return False
    key = f"{_CAPTCHA_PREFIX}{challenge_id}"
    try:
        stored = r.get(key)
        r.delete(key)  # single-use regardless of outcome — a wrong guess can't be retried
    except Exception as e:  # noqa: BLE001
        logger.warning("captcha verify failed: %s", e)
        return False
    if not stored:
        return False
    return secrets.compare_digest(stored.strip().upper(), answer.strip().upper())


@router.get("/captcha")
def get_captcha():
    """Mint a single-use login CAPTCHA (self-hosted image challenge). Returns
    `{challenge_id, image_b64}` (PNG). 404 when disabled so the login page can
    skip it; 503 when Redis (the answer store) is unavailable."""
    if not settings.captcha_enabled:
        raise HTTPException(status_code=404, detail="CAPTCHA disabled")
    challenge_id, image_b64 = _issue_captcha()
    if not challenge_id:
        raise HTTPException(status_code=503, detail="CAPTCHA temporarily unavailable")
    return {"challenge_id": challenge_id, "image_b64": image_b64}


# ── Hybrid LDAP: JIT provisioning + group→role sync (InfoSec phase 3) ────────

def _valid_role(role: str | None) -> UserRole | None:
    try:
        return UserRole(role) if role else None
    except ValueError:
        logger.error("LDAP group→role map yielded unknown role %r", role)
        return None


def _sync_ldap_user(db, identity: "ldap_auth.LdapIdentity") -> User | None:
    """Resolve an authenticated directory identity to its application user row.

    App roles are NOT derived from directory groups. Application roles and AD groups
    are different concerns: the directory proves WHO someone is, the local row decides
    WHAT they may do, and an admin grants that in-app (Admin → Users). Two consequences
    the previous group-sync behaviour did not have:

      * A directory user with no local row is REFUSED rather than JIT-provisioned —
        there is no group to derive a starting role from, and silently inventing one
        would grant access nobody approved.
      * An existing user's role assignments are never touched by a login, so an
        admin's grant is not overwritten on their next sign-in.

    Directory-owned identity attributes (email, display name) ARE still refreshed,
    since AD remains the source of truth for those.

    Returns None when there is no local account (→ login refused).
    """
    user = db.scalar(select(User).where(User.username == identity.username))
    if user is None:
        logger.warning(
            "LDAP login denied: username=%s authenticated against the directory but has "
            "no account in this application. An admin must create it first "
            "(Admin → Users, auth_source='ldap', username matching the directory).",
            _mask_username(identity.username),
        )
        return None
    if identity.email:
        user.email = identity.email
    if identity.full_name:
        user.full_name = identity.full_name
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=LoginResult)
def login(payload: LoginRequest, request: Request, response: Response, db: DbDep):
    client_ip = request.client.host if request.client else "unknown"
    logger.info("Login attempt: username=%s ip=%s", _mask_username(payload.username), client_ip)

    # Fail CLOSED when Redis is down (loses lockout/CAPTCHA/denylist) if configured.
    if settings.login_fail_closed_without_redis and _get_redis() is None:
        logger.error("Login refused: fail-closed and Redis unavailable")
        raise HTTPException(status_code=503, detail="Login temporarily unavailable. Please try again shortly.")

    try:
        _check_login_lockout(payload.username, client_ip)
    except HTTPException:
        auth_audit.record(db, auth_audit.LOCKOUT, username=payload.username, ip=client_ip)
        raise

    # CAPTCHA gate — before credentials, so bots can't probe the password path.
    # A miss counts toward the lockout, so scripted guessing still trips it.
    if settings.captcha_enabled and not _verify_captcha(payload.captcha_id, payload.captcha_answer):
        logger.warning("Login failed: username=%s reason=captcha", _mask_username(payload.username))
        _record_login_failure(payload.username, client_ip)
        auth_audit.record(db, auth_audit.CAPTCHA_FAILED, username=payload.username, ip=client_ip)
        raise HTTPException(status_code=400, detail="CAPTCHA verification failed. Please try again.")

    # ── Credential check — hybrid local/LDAP (InfoSec phase 3) ───────────────
    # A known local user (incl. the break-glass admin) → bcrypt. An LDAP-sourced
    # user, or an unknown username when LDAP is on → directory bind + JIT sync.
    user = db.scalar(select(User).where(User.username == payload.username))
    use_ldap = settings.ldap_enabled and (user is None or user.auth_source == "ldap")
    authed: User | None = None
    if use_ldap:
        identity = ldap_auth.ldap_authenticate(payload.username, payload.password)
        if identity is not None:
            authed = _sync_ldap_user(db, identity)   # None if no local account exists
    elif user is not None and user.auth_source == "local":
        if verify_password(payload.password, user.password_hash):
            authed = user

    if authed is None:
        logger.warning("Login failed: username=%s reason=invalid_credentials", _mask_username(payload.username))
        _record_login_failure(payload.username, client_ip)
        auth_audit.record(db, auth_audit.LOGIN_FAILED, username=payload.username, ip=client_ip,
                          detail="invalid_credentials")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )
    if not authed.is_active:
        logger.warning("Login failed: username=%s reason=account_inactive", _mask_username(payload.username))
        auth_audit.record(db, auth_audit.LOGIN_FAILED, username=payload.username,
                          user_id=authed.id, ip=client_ip, detail="account_inactive")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is inactive",
        )
    user = authed

    # Password + account checks passed — clear the brute-force counters.
    _reset_login_failures(payload.username, client_ip)

    # ── MFA branch (InfoSec phase 2 · platform-wide switch, OR admin-only
    # per T7/security_architecture_skills.md §8.4) ───────────────────────────
    # When MFA is enforced — either platform-wide (`mfa_enforced`) or,
    # closing THREAT_MODEL.md T7, for ADMIN accounts specifically
    # (`admin_mfa_required`, default True) — a correct password is NOT a
    # session yet: issue a short-lived, scope-limited mfa_token and require
    # the OTP (enrolled) or first-login self-registration (not yet set up).
    _admin_mfa_required = (
        user.role == UserRole.ADMIN and getattr(settings, "admin_mfa_required", True)
    )
    if settings.mfa_enforced or _admin_mfa_required:
        if user.mfa_enabled and user.mfa_secret:
            logger.info("Login: username=%s awaiting MFA OTP", _mask_username(payload.username))
            # purpose='verify' — this token proves a correct password ONLY. It
            # must not reach /auth/mfa/setup, which would let a password alone
            # re-enrol the second factor. See mfa.mfa_pending_token().
            return LoginResult(mfa_required=True,
                               mfa_token=mfa.mfa_pending_token(user.id, purpose="verify"))
        logger.info("Login: username=%s must register MFA (enforced)", _mask_username(payload.username))
        return LoginResult(mfa_enrollment_required=True,
                           mfa_token=mfa.mfa_pending_token(user.id, purpose="enroll"))

    token = create_access_token(user.id)
    # Deliver the session as an httpOnly cookie so the SPA never has to hold it
    # in JavaScript-reachable storage. The body copy is suppressed by default
    # (see _body_token) — a token in the response body is a token a client can
    # put straight back into localStorage, which is the weakness being closed.
    set_session_cookie(response, token)
    # `_mask_username` here for the same reason as every other log line in this
    # function: the six login-path logs above it all mask, and this one did not,
    # so a successful login was the ONE case that wrote a full username to the
    # log file. `user_id` is logged alongside, so operators lose no ability to
    # identify the account — the UUID is the better join key anyway.
    logger.info("Login success: username=%s user_id=%s role=%s",
                _mask_username(payload.username), user.id, user.role.value)
    auth_audit.record(db, auth_audit.LOGIN_SUCCESS, username=user.username, user_id=user.id,
                      ip=client_ip, detail=f"role={user.role.value} auth={user.auth_source}")
    return LoginResult(access_token=_body_token(token), user=UserResponse.model_validate(user))


# ── TOTP MFA (InfoSec phase 2) ────────────────────────────────────────────────
# Enrolment (setup + activate) authenticates via EITHER a full session token
# (opt-in enrolment while logged in) OR the short-lived mfa_token from login
# (forced enrolment for a required-role user who isn't enrolled yet).

# auto_error=False: enrolment may authenticate by session COOKIE when no
# bridge-token header is supplied (opt-in enrolment while already logged in).
_enroll_bearer = HTTPBearer(auto_error=False)


def get_enrolling_user(
    request: Request,
    db: DbDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_enroll_bearer)] = None,
) -> User:
    """Authenticate MFA enrolment via EITHER the login bridge token OR a session.

    Two distinct callers, and the order below matters:

    * FORCED enrolment at login — there is no session yet, so the Login page
      sends the short-lived `mfa_token` as an explicit Bearer header. That
      header must WIN, which is why it is checked before the cookie.
    * OPT-IN enrolment while already signed in — the page sends no header, and
      the session travels as the httpOnly cookie. Before the cookie migration
      this case worked because an axios interceptor attached the session token
      to every request; with that interceptor gone, falling back to the cookie
      is what keeps it working.
    """
    token = (credentials.credentials if credentials else None) or request.cookies.get(
        _SESSION_COOKIE_NAME
    )
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user_id = decode_access_token(token)          # full session token?
    if user_id and is_token_revoked(token):
        raise HTTPException(status_code=401, detail="Token has been revoked")
    if not user_id:
        # else the login bridge token — but ONLY one minted for enrolment. A
        # 'verify' token means the user is already enrolled and merely proved a
        # password; accepting it here is what allowed the MFA bypass.
        user_id = mfa.decode_mfa_pending(token, expected_purpose="enroll")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


EnrollingUser = Annotated[User, Depends(get_enrolling_user)]


@router.post("/mfa/setup", response_model=MfaSetupResponse)
def mfa_setup(user: EnrollingUser, db: DbDep):
    """Begin TOTP enrolment: generate a secret (stored encrypted, NOT yet active)
    and return the provisioning URI + QR for the authenticator app. Call
    /auth/mfa/activate with the first code to finish."""
    secret = mfa.generate_totp_secret()
    # SCR finding #12 — mfa_secret is now EncryptedSecret (see models/user.py);
    # the column encrypts on write / decrypts on read transparently, so this
    # assigns the plaintext seed directly instead of calling
    # mfa.encrypt_secret() manually. mfa_enabled stays false until /activate.
    user.mfa_secret = secret
    db.commit()
    uri = mfa.provisioning_uri(secret, user.username)
    return MfaSetupResponse(secret=secret, provisioning_uri=uri, qr_png_b64=mfa.qr_png_b64(uri))


@router.post("/mfa/activate", response_model=MfaActivateResponse)
def mfa_activate(body: MfaActivateRequest, request: Request, user: EnrollingUser,
                 response: Response, db: DbDep):
    """Finish enrolment: verify the first TOTP code against the pending secret,
    turn MFA on, and return one-time backup codes plus a full session token."""
    if not user.mfa_secret:
        raise HTTPException(status_code=400, detail="No pending MFA setup. Call /auth/mfa/setup first.")
    client_ip = request.client.host if request.client else "unknown"
    # Same OTP brute-force throttle as /auth/mfa/verify, and for the same
    # reason: a 6-digit TOTP code is only safe against guessing if attempts
    # are rate-limited, and enrolment verifies a code exactly like login does.
    try:
        _check_login_lockout(user.username, client_ip)
    except HTTPException:
        auth_audit.record(db, auth_audit.LOCKOUT, username=user.username,
                          user_id=user.id, ip=client_ip)
        raise
    # SCR finding #12 — user.mfa_secret is already the plaintext TOTP seed
    # here; EncryptedSecret decrypts transparently on column read.
    if not mfa.verify_totp(user.mfa_secret, body.code):
        _record_login_failure(user.username, client_ip)
        raise HTTPException(status_code=400, detail="Invalid code. Check your authenticator app and try again.")
    _reset_login_failures(user.username, client_ip)
    plain_codes, hashed = mfa.generate_backup_codes()
    user.mfa_enabled = True
    user.mfa_backup_codes = hashed
    db.commit()
    logger.info("MFA activated: user_id=%s username=%s", user.id, user.username)
    auth_audit.record(db, auth_audit.MFA_ACTIVATED, username=user.username, user_id=user.id)
    # Enrolment completes a login, so this is a session-issuing point too — the
    # cookie must be set here exactly as it is on /auth/login, or a user who
    # enrols at first login ends up authenticated by body token only.
    # amr="pwd+mfa": a real TOTP code was just verified above, so this
    # session is entitled to the same claim /auth/mfa/verify issues.
    session_token = create_access_token(user.id, amr="pwd+mfa")
    set_session_cookie(response, session_token)
    return MfaActivateResponse(
        backup_codes=plain_codes,
        access_token=_body_token(session_token),
        user=UserResponse.model_validate(user),
    )


@router.post("/mfa/verify", response_model=MfaVerifyResponse)
def mfa_verify(body: MfaVerifyRequest, request: Request, response: Response, db: DbDep):
    """Second login step: exchange the mfa_token + an OTP (or backup code) for a
    full session token."""
    client_ip = request.client.host if request.client else "unknown"
    user_id = mfa.decode_mfa_pending(body.mfa_token, expected_purpose="verify")
    if not user_id:
        raise HTTPException(status_code=401, detail="MFA session expired. Please log in again.")
    user = db.get(User, user_id)
    if not user or not user.is_active or not user.mfa_enabled or not user.mfa_secret:
        raise HTTPException(status_code=401, detail="MFA not available for this account.")

    # Rate-limit OTP guesses the same way /auth/login rate-limits password
    # guesses, via the shared username+IP counters below. TOTP is a 6-digit
    # HMAC-SHA-1 code (RFC 6238's mandated default — see CBOM-TOTP-SHA1-8);
    # that space is only safe against brute force if attempts are actually
    # throttled, and this endpoint used to record failures into the counter
    # without ever checking it, so the throttle it was feeding never fired
    # here. Checked AFTER decode/user lookup so a bare invalid mfa_token
    # can't be used to probe whether a username is locked out.
    try:
        _check_login_lockout(user.username, client_ip)
    except HTTPException:
        auth_audit.record(db, auth_audit.LOCKOUT, username=user.username,
                          user_id=user.id, ip=client_ip)
        raise

    # SCR finding #12 — user.mfa_secret is already the plaintext TOTP seed
    # here; EncryptedSecret decrypts transparently on column read.
    if mfa.verify_totp(user.mfa_secret, body.code):
        pass
    else:
        ok, remaining = mfa.consume_backup_code(body.code, user.mfa_backup_codes)
        if not ok:
            _record_login_failure(user.username, client_ip)
            logger.warning("MFA verify failed: user_id=%s", user.id)
            auth_audit.record(db, auth_audit.MFA_VERIFY_FAILED, username=user.username,
                              user_id=user.id, ip=client_ip)
            raise HTTPException(status_code=400, detail="Invalid code.")
        user.mfa_backup_codes = remaining          # single-use backup code consumed
        db.commit()
        logger.info("MFA verify via backup code: user_id=%s remaining=%d", user.id, len(remaining))

    _reset_login_failures(user.username, client_ip)
    logger.info("MFA verify success: user_id=%s username=%s", user.id, user.username)
    auth_audit.record(db, auth_audit.LOGIN_SUCCESS, username=user.username, user_id=user.id,
                      ip=client_ip, detail="mfa")
    # Second login step completes the session — set the cookie here too.
    # amr="pwd+mfa": this is the ONLY branch in this file where a real OTP/
    # backup-code check has just passed, so it is the only call site
    # entitled to claim MFA was completed for this session (see
    # core.security.create_access_token's docstring / THREAT_MODEL.md T7).
    session_token = create_access_token(user.id, amr="pwd+mfa")
    set_session_cookie(response, session_token)
    return MfaVerifyResponse(access_token=_body_token(session_token),
                             user=UserResponse.model_validate(user))


@router.post("/mfa/disable")
def mfa_disable(body: MfaDisableRequest, request: Request, current_user: CurrentUser, db: DbDep):
    """Disable MFA for the authenticated user. Requires the current password (and
    a valid OTP if one is set). Blocked when the user's role mandates MFA."""
    if settings.mfa_enforced:
        raise HTTPException(status_code=403, detail="MFA is mandatory platform-wide and cannot be disabled.")
    if not verify_password(body.password, current_user.password_hash):
        raise HTTPException(status_code=401, detail="Password is incorrect")
    if current_user.mfa_enabled and current_user.mfa_secret:
        client_ip = request.client.host if request.client else "unknown"
        # Same OTP throttle as /auth/mfa/verify and /auth/mfa/activate: an
        # attacker who already has a valid SESSION (stolen cookie, XSS) but
        # not the authenticator app could otherwise guess the 6-digit code
        # here without limit to strip MFA off the account.
        try:
            _check_login_lockout(current_user.username, client_ip)
        except HTTPException:
            auth_audit.record(db, auth_audit.LOCKOUT, username=current_user.username,
                              user_id=current_user.id, ip=client_ip)
            raise
        # SCR finding #12 — current_user.mfa_secret is already the plaintext
        # TOTP seed here; EncryptedSecret decrypts transparently on read.
        if not (body.code and mfa.verify_totp(current_user.mfa_secret, body.code)):
            _record_login_failure(current_user.username, client_ip)
            raise HTTPException(status_code=400, detail="A valid authenticator code is required to disable MFA.")
        _reset_login_failures(current_user.username, client_ip)
    current_user.mfa_enabled = False
    current_user.mfa_secret = None
    current_user.mfa_backup_codes = None
    db.commit()
    logger.info("MFA disabled: user_id=%s username=%s", current_user.id, current_user.username)
    auth_audit.record(db, auth_audit.MFA_DISABLED, username=current_user.username, user_id=current_user.id)
    return {"detail": "MFA disabled"}


@router.get("/me", response_model=UserResponse)
def get_me(current_user: CurrentUser):
    return UserResponse.model_validate(current_user)


@router.post("/switch-role", response_model=UserResponse)
def switch_role(body: SwitchRoleRequest, request: Request, current_user: CurrentUser, db: DbDep):
    """Switch the caller's ACTIVE role. Only a role the user is assigned may be
    selected (no privilege escalation). Takes effect immediately — every RBAC
    guard reads the active role (User.role) fresh from the DB on each request."""
    if body.role not in current_user.roles:
        raise HTTPException(status_code=403, detail="Role not assigned to this user")
    if body.role != current_user.role:
        previous = current_user.role
        current_user.role = body.role
        db.commit()
        db.refresh(current_user)
        client_ip = request.client.host if request.client else "unknown"
        auth_audit.record(db, auth_audit.ROLE_SWITCHED, username=current_user.username,
                          user_id=current_user.id, ip=client_ip,
                          detail=f"{previous.value}->{body.role.value}")
    return UserResponse.model_validate(current_user)


@router.post("/logout")
def logout(request: Request, response: Response, db: DbDep):
    # Read the session the same way every authenticated route does, so a
    # cookie-authenticated logout actually revokes the token. Reading only the
    # Authorization header here would silently no-op for cookie sessions,
    # leaving a valid token alive until its natural 8h expiry.
    token = extract_token(request)
    user_id = None
    if token:
        user_id = decode_access_token(token)
        revoke_token(token)
    clear_session_cookie(response)
    logger.info("Logout — token revoked")
    client_ip = request.client.host if request.client else "unknown"
    auth_audit.record(db, auth_audit.LOGOUT, user_id=user_id, ip=client_ip)
    return {"message": "Logged out successfully"}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password:     str


@router.post("/change-password")
def change_password(body: ChangePasswordRequest, request: Request, response: Response,
                    current_user: CurrentUser, db: DbDep):
    """Change the authenticated user's password. The current token is
    revoked after a successful change — the client must re-authenticate."""
    if not verify_password(body.current_password, current_user.password_hash):
        logger.warning("Change-password failed: user_id=%s reason=bad_current", current_user.id)
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    _validate_new_password(body.new_password)

    if verify_password(body.new_password, current_user.password_hash):
        raise HTTPException(status_code=422, detail="New password must differ from the current one")

    current_user.password_hash = hash_password(body.new_password)
    db.commit()

    # Revoke the current token so it cannot be reused after password change.
    # Resolve it the same way the auth dependency does — a cookie-authenticated
    # session would otherwise survive its own password change.
    token = extract_token(request)
    if token:
        revoke_token(token)
    # The client must re-authenticate, so drop the cookie rather than leaving
    # the browser to send a now-revoked value on every subsequent request.
    clear_session_cookie(response)

    logger.info("Password changed: user_id=%s username=%s — token revoked", current_user.id, current_user.username)
    client_ip = request.client.host if request.client else "unknown"
    auth_audit.record(db, auth_audit.PASSWORD_CHANGED, username=current_user.username,
                      user_id=current_user.id, ip=client_ip)
    return {"detail": "Password changed"}


@router.get("/audit")
def auth_audit_log(
    _: AdminUser, db: DbDep,
    limit: int = 100, event: str | None = None, username: str | None = None,
):
    """InfoSec review — recent auth events, newest first. Admin only. Filter by
    `event` and/or `username`; `limit` capped at 500."""
    from app.models.auth_audit import AuthAudit
    q = select(AuthAudit).order_by(AuthAudit.created_at.desc())
    if event:
        q = q.where(AuthAudit.event == event)
    if username:
        q = q.where(AuthAudit.username == username)
    rows = db.scalars(q.limit(min(max(limit, 1), 500))).all()
    return [
        {
            "id": r.id, "event": r.event, "username": r.username, "user_id": r.user_id,
            "ip": r.ip, "detail": r.detail,
            "at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
