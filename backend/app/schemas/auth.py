# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

from pydantic import BaseModel
from app.schemas.user import UserResponse


class LoginRequest(BaseModel):
    username: str
    password: str
    # Self-hosted CAPTCHA (from GET /auth/captcha). Optional on the wire so the
    # request still validates when CAPTCHA is disabled; /auth/login enforces
    # presence + correctness when settings.captcha_enabled is true.
    captcha_id: str | None = None
    captcha_answer: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class LoginResult(BaseModel):
    """Login outcome: either a full session, or an MFA challenge to complete.

    - non-MFA user → `user` populated; the session itself is delivered as an
      httpOnly cookie. `access_token` is None unless
      AUTH_RETURN_TOKEN_IN_BODY is set (non-browser callers only) — clients
      should treat a populated `user` as the "logged in" signal.
    - MFA-enrolled user → mfa_required=True + mfa_token (submit OTP to /auth/mfa/verify)
    - role mandates MFA but user not enrolled → mfa_enrollment_required=True + mfa_token
      (enrol via /auth/mfa/setup + /auth/mfa/activate using the mfa_token)
    """
    mfa_required: bool = False
    mfa_enrollment_required: bool = False
    mfa_token: str | None = None
    access_token: str | None = None
    token_type: str = "bearer"
    user: UserResponse | None = None


class MfaSetupResponse(BaseModel):
    secret: str
    provisioning_uri: str
    qr_png_b64: str


class MfaActivateRequest(BaseModel):
    code: str


class MfaActivateResponse(BaseModel):
    backup_codes: list[str]         # shown to the user ONCE
    # Session is delivered as an httpOnly cookie; None unless
    # AUTH_RETURN_TOKEN_IN_BODY is set for a non-browser caller.
    access_token: str | None = None
    token_type: str = "bearer"
    user: UserResponse


class MfaVerifyRequest(BaseModel):
    mfa_token: str
    code: str                       # 6-digit TOTP or a backup code


class MfaVerifyResponse(BaseModel):
    # Session is delivered as an httpOnly cookie; None unless
    # AUTH_RETURN_TOKEN_IN_BODY is set for a non-browser caller.
    access_token: str | None = None
    token_type: str = "bearer"
    user: UserResponse


class MfaDisableRequest(BaseModel):
    password: str
    code: str | None = None
