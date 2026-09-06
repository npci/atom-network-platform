# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

import enum
from sqlalchemy import String, Boolean, Enum, JSON, ForeignKey
from sqlalchemy.orm import mapped_column, Mapped, relationship
from app.core.database import Base
from app.core.encrypted_type import EncryptedSecret
from app.models.base import TimestampMixin, generate_uuid


class UserRole(str, enum.Enum):
    PRODUCT_OWNER = "product_owner"
    PRODUCT_MANAGER = "product_manager"
    TECH_LEAD = "tech_lead"
    INFOSEC_REVIEWER = "infosec_reviewer"
    RISK_REVIEWER = "risk_reviewer"
    ADMIN = "admin"


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Auth backend for this user: 'local' (bcrypt password_hash) or 'ldap' (bind
    # against the directory). Break-glass admins stay 'local'. (InfoSec phase 3)
    auth_source: Mapped[str] = mapped_column(String(16), default="local", nullable=False)

    # ── TOTP MFA (InfoSec phase 2) ──────────────────────────────────────────
    # mfa_secret — SCR finding #12 (Insufficiently Protected Credentials).
    # Used to be a plain String column encrypted only via call-site discipline
    # in app/core/mfa.py (a separate Fernet mechanism/KEK from every other
    # secret in this codebase). Now EncryptedSecret — the SAME mechanism as
    # PartnerAgent.api_key/.jwt_signing_secret/etc. (Fernet keyed by
    # CONFIG_ENCRYPTION_KEY, enc:v1: prefix; see core/encrypted_type.py) —
    # so encryption is enforced at the column level rather than depending on
    # every call site remembering to encrypt/decrypt manually. Existing rows
    # were re-encrypted in place by alembic/versions/0130_encrypt_mfa_secret_
    # at_rest.py; callers now read/write the plaintext TOTP seed directly
    # (mfa.encrypt_secret()/decrypt_secret() are no longer used for storage —
    # see app/api/auth.py).
    # mfa_backup_codes is a JSON list of bcrypt-hashed single-use recovery codes.
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mfa_secret: Mapped[str | None] = mapped_column(EncryptedSecret, nullable=True)
    mfa_backup_codes: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Relationships
    change_requests: Mapped[list["ChangeRequest"]] = relationship(
        "ChangeRequest", back_populates="creator", foreign_keys="ChangeRequest.created_by"
    )
    notifications: Mapped[list["Notification"]] = relationship(
        "Notification", back_populates="user"
    )
    approvals: Mapped[list["Approval"]] = relationship(
        "Approval", back_populates="approver"
    )

    # ── Multi-role (role switching) ─────────────────────────────────────────
    # `role` above is the user's ACTIVE role (what every RBAC guard reads). The
    # rows below are the roles the user MAY switch to. A switch validates the
    # target is in this set, then updates `role`. See POST /auth/switch-role.
    role_assignments: Mapped[list["UserRoleAssignment"]] = relationship(
        "UserRoleAssignment", back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def roles(self) -> list["UserRole"]:
        """Assigned roles the user may switch to (always includes the active role)."""
        s = {ra.role for ra in self.role_assignments}
        s.add(self.role)
        return sorted(s, key=lambda r: r.value)

    @property
    def active_role(self) -> "UserRole":
        return self.role


class UserRoleAssignment(Base):
    """A role a user is assigned (and may switch to). The user's *active* role is
    `User.role`; this join table is the set of roles they're allowed to act as."""
    __tablename__ = "user_roles"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, values_callable=lambda x: [e.value for e in x]), primary_key=True
    )

    user: Mapped["User"] = relationship("User", back_populates="role_assignments")
