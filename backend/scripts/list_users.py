# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Print all users — pure read-only audit listing.

Usage:
    cd /opt/npci-platform/app/backend
    source venv/bin/activate
    python scripts/list_users.py

    # Filter by role
    python scripts/list_users.py --role admin

    # Show only inactive users
    python scripts/list_users.py --inactive
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.core.database import SessionLocal
from app.models.user import User, UserRole


def main() -> int:
    ap = argparse.ArgumentParser(description="List users.")
    ap.add_argument("--role",     default=None,
                    help="Filter by role (e.g. admin, product_owner, tech_lead)")
    ap.add_argument("--inactive", action="store_true",
                    help="Show only inactive users (default: show all)")
    ns = ap.parse_args()

    db = SessionLocal()
    try:
        q = db.query(User)
        if ns.role:
            try:
                role = next(r for r in UserRole if r.value.lower() == ns.role.lower())
            except StopIteration:
                print(f"ERROR: unknown role {ns.role!r}", file=sys.stderr)
                return 2
            q = q.filter(User.role == role)
        if ns.inactive:
            q = q.filter(User.is_active.is_(False))
        users = q.order_by(User.role, User.email).all()

        if not users:
            print("(no users match)")
            return 0

        print(f"{'EMAIL':<40} {'NAME':<24} {'ROLE':<20} {'ACTIVE':<6}")
        print("-" * 96)
        for u in users:
            role_str = u.role.value if hasattr(u.role, "value") else str(u.role)
            print(f"{u.email:<40} {(u.name or '-'):<24} {role_str:<20} "
                  f"{'yes' if u.is_active else 'NO':<6}")
        print("-" * 96)
        print(f"  total: {len(users)}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
