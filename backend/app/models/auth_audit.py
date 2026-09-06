# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Authentication audit trail (InfoSec phase 4).

An append-only record of security-relevant auth events — logins (success/fail),
CAPTCHA/lockout, MFA enrol/verify/disable/reset, and LDAP provisioning — so
InfoSec can review who authenticated, from where, and how it went. Written
best-effort by `app.core.auth_audit.record`; never blocks the auth flow.
"""
from sqlalchemy import String, Text
from sqlalchemy.orm import mapped_column, Mapped

from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid


class AuthAudit(Base, TimestampMixin):
    __tablename__ = "auth_audit"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    event: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(150), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
