# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""
Run once after `alembic upgrade head` to create the initial admin user.

Usage:
    python seed.py
"""
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(__file__))

from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models.user import User, UserRole
from sqlalchemy import select

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@npci.org.in")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD") or secrets.token_urlsafe(16)


def seed():
    db = SessionLocal()
    try:
        existing = db.scalar(select(User).where(User.username == ADMIN_USERNAME))
        if existing:
            print(f"Admin user '{ADMIN_USERNAME}' already exists. Skipping.")
            return

        admin = User(
            username=ADMIN_USERNAME,
            email=ADMIN_EMAIL,
            password_hash=hash_password(ADMIN_PASSWORD),
            full_name="Platform Admin",
            role=UserRole.ADMIN,
        )
        db.add(admin)
        db.commit()
        print(f"Admin user '{ADMIN_USERNAME}' created successfully.")
        print(f"  Email   : {ADMIN_EMAIL}")
        if not os.getenv("ADMIN_PASSWORD"):
            print(f"  Password: {ADMIN_PASSWORD}  ⚠️ SAVE THIS — shown once only")
        else:
            print("  Password: (set via ADMIN_PASSWORD env var)")
        print("IMPORTANT: Change the password immediately after first login.")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
