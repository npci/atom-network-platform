# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

import re
from typing import Literal

from pydantic import BaseModel, EmailStr, field_validator, model_validator
from app.models.user import UserRole

_HAS_LETTER = re.compile(r"[A-Za-z]")
_HAS_DIGIT  = re.compile(r"\d")

# bcrypt hashes at most 72 BYTES and the installed backend raises rather than
# truncating, so an over-long password would reach `hash_password` and surface
# as an unhandled 500 instead of a validation error. Kept in step with
# `api/auth.py::_MAX_PASSWORD_BYTES`. Measured in bytes because a non-ASCII
# password can be under 72 characters yet over 72 bytes once UTF-8 encoded.
_MAX_PASSWORD_BYTES = 72


class UserCreate(BaseModel):
    username: str
    email: EmailStr
    # Omitted for auth_source='ldap' — the directory holds the credential and the
    # local hash is never checked, so the API generates an unusable random one.
    password: str | None = None
    full_name: str | None = None
    # Roles the user is assigned (may switch between). The first is the initial
    # active role. Must be non-empty.
    roles: list[UserRole]
    # 'local' → bcrypt against password_hash. 'ldap' → the login routes to the
    # directory (see api/auth.py::login) and these roles are authoritative: an
    # LDAP sign-in never overwrites them.
    auth_source: Literal["local", "ldap"] = "local"

    @field_validator("roles")
    @classmethod
    def at_least_one_role(cls, v: list[UserRole]) -> list[UserRole]:
        if not v:
            raise ValueError("At least one role is required")
        return list(dict.fromkeys(v))  # dedupe, preserve order

    @model_validator(mode="after")
    def password_required_for_local(self) -> "UserCreate":
        if self.auth_source == "local" and not self.password:
            raise ValueError("password is required when auth_source is 'local'")
        return self

    @field_validator("password")
    @classmethod
    def password_policy(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if len(v.encode("utf-8")) > _MAX_PASSWORD_BYTES:
            raise ValueError(
                f"Password must be at most {_MAX_PASSWORD_BYTES} bytes "
                "(non-ASCII characters count as more than one byte)"
            )
        if not _HAS_LETTER.search(v):
            raise ValueError("Password must contain at least one letter")
        if not _HAS_DIGIT.search(v):
            raise ValueError("Password must contain at least one digit")
        return v


class UserUpdate(BaseModel):
    full_name: str | None = None
    email: EmailStr | None = None
    # Replace the assigned-role set (non-empty). If the current active role is
    # dropped, it resets to the first of the new set.
    roles: list[UserRole] | None = None
    is_active: bool | None = None

    @field_validator("roles")
    @classmethod
    def roles_non_empty(cls, v: list[UserRole] | None) -> list[UserRole] | None:
        if v is not None:
            if not v:
                raise ValueError("At least one role is required")
            return list(dict.fromkeys(v))
        return v


class SwitchRoleRequest(BaseModel):
    role: UserRole


class UserResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    username: str
    email: str
    full_name: str | None
    role: UserRole                 # the ACTIVE role (what RBAC uses)
    roles: list[UserRole] = []     # all assigned roles the user may switch to
    active_role: UserRole | None = None
    is_active: bool
    mfa_enabled: bool = False
    auth_source: str = "local"


class UserListResponse(BaseModel):
    total: int
    items: list[UserResponse]
