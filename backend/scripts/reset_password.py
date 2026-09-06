# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Reset a single user's password.

Usage:
    cd /opt/npci-platform/app/backend
    source venv/bin/activate

    # Set explicit password
    python scripts/reset_password.py alice@npci.org.in --password 'NewPass123!'

    # Generate random password
    python scripts/reset_password.py alice@npci.org.in
    # → prints generated password ONCE, save it now.

    # Force-activate a user that was deactivated
    python scripts/reset_password.py alice@npci.org.in --activate
"""
from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models.user import User


def _generate_password(length: int = 16) -> str:
    alphabet = (
        "ABCDEFGHJKLMNPQRSTUVWXYZ"
        "abcdefghjkmnpqrstuvwxyz"
        "23456789"
        "!@#$%^&*-_+="
    )
    return "".join(secrets.choice(alphabet) for _ in range(length))


def main() -> int:
    ap = argparse.ArgumentParser(description="Reset a user's password.")
    ap.add_argument("email", help="User's login email")
    ap.add_argument("--password", default=None,
                    help="New password (auto-generated if omitted)")
    ap.add_argument("--activate", action="store_true",
                    help="Also set is_active=True (useful for unlocking dormant accounts)")
    ns = ap.parse_args()

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == ns.email).one_or_none()
        if user is None:
            print(f"ERROR: no user with email {ns.email!r}", file=sys.stderr)
            return 1

        new_pw = ns.password or _generate_password()
        user.password_hash = hash_password(new_pw)
        if ns.activate:
            user.is_active = True
        db.commit()

        print(f"OK: reset password for {ns.email}  (active={user.is_active})")
        if not ns.password:
            print(f"  Generated password: {new_pw}")
            print(f"  ⚠  Shown once. Save it now.")
        return 0
    except Exception as e:
        db.rollback()
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
