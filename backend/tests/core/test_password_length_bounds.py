# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""bcrypt's 72-byte ceiling must be enforced at the API boundary.

Found during the adversarial re-review of the Checkmarx SCR triage. The password
policy enforced a MINIMUM of 8 characters and no maximum, but bcrypt hashes at
most 72 bytes. The installed backend raises rather than truncating::

    ValueError: password cannot be longer than 72 bytes, truncate manually ...

so `POST /auth/change-password` with a long password produced an unhandled
exception — a 500 for what is really a validation failure, reachable by any
authenticated user.

The other bcrypt behaviour is worse and these tests pin against it too: builds
that TRUNCATE silently would let two different passwords sharing a 72-byte
prefix both verify. Rejecting at the boundary is correct under either.
"""
import pytest

from app.core.security import (
    BCRYPT_MAX_PASSWORD_BYTES,
    hash_password,
    verify_password,
)


# ── the boundary itself ──────────────────────────────────────────────────────

def test_password_at_the_limit_still_hashes():
    """72 bytes is allowed — the ceiling must not be off by one."""
    pw = "a1" + "x" * (BCRYPT_MAX_PASSWORD_BYTES - 2)
    assert len(pw.encode()) == BCRYPT_MAX_PASSWORD_BYTES
    assert verify_password(pw, hash_password(pw))


def test_over_long_password_is_refused_by_the_hasher():
    """Defence in depth: the funnel every password passes through refuses,
    rather than crashing opaquely or silently truncating."""
    pw = "a1" + "x" * BCRYPT_MAX_PASSWORD_BYTES
    with pytest.raises(ValueError, match="72-byte limit"):
        hash_password(pw)


def test_verify_returns_false_for_over_long_candidate():
    """An over-long candidate is a failed login, never a 500.

    No stored hash can have been produced from it (hash_password refuses the
    same length), so False is both the safe and the correct answer.
    """
    stored = hash_password("correct-horse1")
    assert verify_password("x" * 500 + "1", stored) is False


def test_verify_returns_false_for_corrupt_stored_hash():
    """A malformed hash column must not turn a login attempt into a crash."""
    assert verify_password("anything1", "not-a-bcrypt-hash") is False


def test_truncation_collision_is_impossible_through_the_public_api():
    """The property the ceiling exists to protect.

    Two passwords sharing a 72-byte prefix must never both verify. With the
    bound in place neither can even be hashed, so the collision is unreachable.
    """
    base = "a1" + "x" * (BCRYPT_MAX_PASSWORD_BYTES - 2)
    with pytest.raises(ValueError):
        hash_password(base + "SUFFIX_ONE")
    stored = hash_password(base)
    assert verify_password(base + "SUFFIX_TWO", stored) is False


# ── the policy validators that keep it a 422 rather than a 500 ───────────────

def _import_or_skip(name: str):
    """Import an app module, skipping if its optional runtime deps are absent.

    `pytest.importorskip` is not enough here. It skips on ModuleNotFoundError
    for the requested module, but these modules pull in a DB driver and
    `pydantic[email]`, and pydantic *re-raises* the missing email-validator as
    a plain ImportError naming a different package. That propagates and is
    reported as a policy failure when it is really an environment gap.

    Worse, which dependency is missing depends on test ORDER: run alone, the
    import dies early on psycopg2; run after a test that has already loaded the
    ORM, it gets further and dies on email-validator instead. Catching
    ImportError as a family is the only stable answer.

    The hasher-level tests above pin the invariant in every environment, and CI
    installs both dependencies, so the policy layers are still covered there.
    """
    try:
        return __import__(name, fromlist=["__name__"])
    except ImportError as exc:
        pytest.skip(f"{name} needs a dependency that is not installed here: {exc}")


def _auth_module():
    return _import_or_skip("app.api.auth")


def test_change_password_policy_rejects_over_long():
    auth = _auth_module()
    from fastapi import HTTPException

    limit = auth._MAX_PASSWORD_BYTES
    auth._validate_new_password("a1" + "x" * (limit - 2))  # at the limit: fine

    with pytest.raises(HTTPException) as ei:
        auth._validate_new_password("a1" + "x" * limit)
    assert ei.value.status_code == 422
    assert "bytes" in str(ei.value.detail)


def test_policy_counts_bytes_not_characters():
    """A non-ASCII password can be well under 72 CHARACTERS and still exceed 72
    BYTES once UTF-8 encoded — the check must use the encoded length."""
    auth = _auth_module()
    from fastapi import HTTPException

    pw = "é" * 40 + "a1"          # 42 characters, 82 bytes
    assert len(pw) < 72 and len(pw.encode("utf-8")) > 72
    with pytest.raises(HTTPException) as ei:
        auth._validate_new_password(pw)
    assert ei.value.status_code == 422


def test_user_create_schema_rejects_over_long_password():
    """The admin user-create path calls hash_password directly, so the ceiling
    has to live in the schema too — not only in the change-password route."""
    from pydantic import ValidationError

    schemas = _import_or_skip("app.schemas.user")
    UserRole = _import_or_skip("app.models.user").UserRole
    UserCreate = schemas.UserCreate

    ok = UserCreate(username="u", email="u@example.com",
                    password="a1" + "x" * 70, roles=[UserRole.ADMIN])
    assert ok.password

    with pytest.raises(ValidationError, match="bytes"):
        UserCreate(username="u", email="u@example.com",
                   password="a1" + "x" * 71, roles=[UserRole.ADMIN])
