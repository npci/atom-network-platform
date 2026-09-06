# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Create a single user from CLI arguments.

Idempotent: re-running with the same email is a no-op (prints "exists").
Use --update to update name/role/password on an existing user instead.

Usage:
    cd /opt/npci-platform/app/backend
    source venv/bin/activate
    python scripts/create_user.py \\
        --email   alice@npci.org.in \\
        --name    "Alice Patel" \\
        --role    product_owner \\
        --password 'StartHere123!'

    # Update an existing user (force-reset password / change role / rename):
    python scripts/create_user.py --email alice@npci.org.in --password 'NewPass!' --update

    # Generate a random password (printed to stdout once, never again):
    python scripts/create_user.py --email bob@npci.org.in --name Bob --role tech_lead

Roles (case-insensitive): product_owner | product_manager | tech_lead |
                          infosec_reviewer | risk_reviewer | admin
"""
from __future__ import annotations

import argparse
import secrets
import string
import sys
from pathlib import Path

# Make sure the backend/ folder is on sys.path when run from anywhere
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models.base import generate_uuid
from app.models.user import User, UserRole


def _coerce_role(raw: str) -> UserRole:
    """Accept names case-insensitively; raise with a helpful list on bad input."""
    if not raw:
        raise ValueError("role is required")
    norm = raw.strip().lower()
    for r in UserRole:
        if r.value.lower() == norm or r.name.lower() == norm:
            return r
    valid = ", ".join(r.value for r in UserRole)
    raise ValueError(f"unknown role {raw!r}; valid options: {valid}")


def _generate_password(length: int = 16) -> str:
    """16-char random password mixing letters, digits, and a punctuation char.

    Avoids ambiguous chars (O/0, l/1) so anyone reading it off a terminal
    isn't going to mistype it.
    """
    alphabet = (
        "ABCDEFGHJKLMNPQRSTUVWXYZ"
        "abcdefghjkmnpqrstuvwxyz"
        "23456789"
        "!@#$%^&*-_+="
    )
    return "".join(secrets.choice(alphabet) for _ in range(length))


def main() -> int:
    ap = argparse.ArgumentParser(description="Create or update a single user.")
    ap.add_argument("--email",    required=True, help="Login email")
    ap.add_argument("--name",     required=False, default=None, help="Display name (defaults to email's local-part if omitted)")
    ap.add_argument("--role",     required=False, default="product_owner",
                    help="User role (default: product_owner)")
    ap.add_argument("--password", required=False, default=None,
                    help="Password (auto-generated if omitted)")
    ap.add_argument("--inactive", action="store_true",
                    help="Create the user but leave is_active=False")
    ap.add_argument("--update",   action="store_true",
                    help="If the email already exists, UPDATE in place (otherwise no-op)")
    ns = ap.parse_args()

    try:
        role = _coerce_role(ns.role)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    password = ns.password or _generate_password()
    name = ns.name or ns.email.split("@", 1)[0].replace(".", " ").title()

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == ns.email).one_or_none()

        if existing and not ns.update:
            print(f"SKIP {ns.email} — already exists "
                  f"(role={existing.role.value if hasattr(existing.role, 'value') else existing.role}). "
                  f"Pass --update to modify.")
            return 0

        if existing:
            existing.name = name
            existing.role = role
            existing.is_active = not ns.inactive
            if ns.password:
                existing.password_hash = hash_password(password)
            db.commit()
            print(f"UPDATED {ns.email}  role={role.value}  active={not ns.inactive}")
            if ns.password:
                print(f"  Password set to the value you supplied.")
            return 0

        user = User(
            id=generate_uuid(),
            email=ns.email,
            name=name,
            password_hash=hash_password(password),
            role=role,
            is_active=not ns.inactive,
        )
        db.add(user)
        db.commit()

        print(f"CREATED {ns.email}  role={role.value}  active={not ns.inactive}")
        if not ns.password:
            print(f"  Generated password: {password}")
            print(f"  ⚠  This is shown ONCE. Save it now or run with --update --password to reset.")
        return 0
    except Exception as e:
        db.rollback()
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
