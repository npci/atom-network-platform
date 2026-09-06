# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

import secrets

from fastapi import APIRouter, HTTPException, status, Depends
from sqlalchemy import select, func
from app.core.deps import DbDep, AdminUser
from app.core.security import hash_password
from app.models.user import User, UserRoleAssignment
from app.schemas.user import UserCreate, UserUpdate, UserResponse, UserListResponse

router = APIRouter(prefix="/users", tags=["users"])


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, db: DbDep, _: AdminUser):
    if db.scalar(select(User).where(User.username == payload.username)):
        raise HTTPException(status_code=409, detail="Username already exists")
    if db.scalar(select(User).where(User.email == payload.email)):
        raise HTTPException(status_code=409, detail="Email already exists")

    # First assigned role is the initial active role; all are recorded as assignable.
    # auth_source='ldap' users authenticate by directory bind, so their local hash is
    # never checked — give them an unusable random one rather than a guessable stand-in.
    user = User(
        username=payload.username,
        email=payload.email,
        password_hash=hash_password(payload.password or secrets.token_urlsafe(32)),
        full_name=payload.full_name,
        role=payload.roles[0],
        role_assignments=[UserRoleAssignment(role=r) for r in payload.roles],
        auth_source=payload.auth_source,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserResponse.model_validate(user)


@router.get("", response_model=UserListResponse)
def list_users(db: DbDep, _: AdminUser, skip: int = 0, limit: int = 50):
    total = db.scalar(select(func.count()).select_from(User))
    users = db.scalars(select(User).offset(skip).limit(limit)).all()
    return UserListResponse(total=total, items=[UserResponse.model_validate(u) for u in users])


@router.get("/{user_id}", response_model=UserResponse)
def get_user(user_id: str, db: DbDep, _: AdminUser):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return UserResponse.model_validate(user)


@router.patch("/{user_id}", response_model=UserResponse)
def update_user(user_id: str, payload: UserUpdate, db: DbDep, _: AdminUser):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    data = payload.model_dump(exclude_none=True)
    new_roles = data.pop("roles", None)
    for field, value in data.items():
        setattr(user, field, value)

    if new_roles is not None:
        # Replace the assignable set; keep the active role valid.
        user.role_assignments = [UserRoleAssignment(role=r) for r in new_roles]
        if user.role not in new_roles:
            user.role = new_roles[0]

    db.commit()
    db.refresh(user)
    return UserResponse.model_validate(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def deactivate_user(user_id: str, db: DbDep, _: AdminUser):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = False
    db.commit()


@router.post("/{user_id}/mfa/reset", response_model=UserResponse)
def reset_user_mfa(user_id: str, db: DbDep, _: AdminUser):
    """Admin MFA reset — clears a user's TOTP enrolment (lost/replaced device).
    The user re-enrols on next login (forced if their role mandates MFA)."""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.mfa_enabled = False
    user.mfa_secret = None
    user.mfa_backup_codes = None
    db.commit()
    db.refresh(user)
    from app.core import auth_audit
    auth_audit.record(db, auth_audit.MFA_RESET, username=user.username, user_id=user.id)
    return user
