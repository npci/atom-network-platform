# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Bulk-create users from a CSV file. Idempotent.

CSV schema (header row required):
    email,name,role,password
    alice@npci.org.in,Alice Patel,product_owner,StartHere123!
    bob@npci.org.in,Bob,tech_lead,
    charlie@npci.org.in,Charlie,admin,SuperSecret!

Rules per row:
    - email: required, unique. Lowercased before insert.
    - name:  optional. Defaults to email's local-part Title-Cased.
    - role:  required. See `app.models.user.UserRole` for valid values.
    - password: optional. If empty, a random 16-char password is generated
                and printed in the run summary (printed once, never again).

Usage:
    cd /opt/npci-platform/app/backend
    source venv/bin/activate
    python scripts/bulk_create_users.py /path/to/users.csv

    # Update mode — overwrites name/role/password for already-existing emails:
    python scripts/bulk_create_users.py --update /path/to/users.csv

    # Dry-run — validate the CSV without writing to DB:
    python scripts/bulk_create_users.py --dry-run /path/to/users.csv
"""
from __future__ import annotations

import argparse
import csv
import secrets
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models.base import generate_uuid
from app.models.user import User, UserRole


REQUIRED_COLS = {"email", "role"}


def _coerce_role(raw: str) -> UserRole:
    norm = (raw or "").strip().lower()
    for r in UserRole:
        if r.value.lower() == norm or r.name.lower() == norm:
            return r
    valid = ", ".join(r.value for r in UserRole)
    raise ValueError(f"unknown role {raw!r}; valid: {valid}")


def _generate_password(length: int = 16) -> str:
    alphabet = (
        "ABCDEFGHJKLMNPQRSTUVWXYZ"
        "abcdefghjkmnpqrstuvwxyz"
        "23456789"
        "!@#$%^&*-_+="
    )
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"{path} is empty or malformed")
        missing = REQUIRED_COLS - {c.strip().lower() for c in reader.fieldnames}
        if missing:
            raise ValueError(f"CSV missing required columns: {sorted(missing)}")
        rows: list[dict] = []
        for ln, raw in enumerate(reader, start=2):    # start=2 since header is line 1
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            row["_line"] = ln
            if not row.get("email"):
                continue   # skip blank lines
            rows.append(row)
        return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Bulk-create users from CSV.")
    ap.add_argument("csv_path",  type=Path, help="Path to users CSV")
    ap.add_argument("--update",  action="store_true",
                    help="If an email already exists, UPDATE name/role/password instead of skipping")
    ap.add_argument("--dry-run", action="store_true",
                    help="Validate the CSV without touching the DB")
    ns = ap.parse_args()

    try:
        rows = _read_csv(ns.csv_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    # Validate all rows BEFORE writing anything — fail-fast on bad input.
    parsed: list[dict] = []
    seen_emails: set[str] = set()
    errors: list[str] = []
    for row in rows:
        email = row["email"].lower()
        if email in seen_emails:
            errors.append(f"line {row['_line']}: duplicate email {email!r}")
            continue
        seen_emails.add(email)
        try:
            role = _coerce_role(row.get("role", ""))
        except ValueError as e:
            errors.append(f"line {row['_line']}: {e}")
            continue
        password = row.get("password") or _generate_password()
        name = row.get("name") or email.split("@", 1)[0].replace(".", " ").title()
        parsed.append({
            "line": row["_line"],
            "email": email,
            "name": name,
            "role": role,
            "password": password,
            "password_was_generated": not row.get("password"),
        })

    if errors:
        print("Validation errors:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 2

    print(f"Parsed {len(parsed)} valid rows from {ns.csv_path}.")
    if ns.dry_run:
        print("DRY-RUN — no DB changes.")
        for p in parsed:
            print(f"  WOULD INSERT/UPDATE  {p['email']:<40}  role={p['role'].value}")
        return 0

    db = SessionLocal()
    created = 0
    updated = 0
    skipped = 0
    generated_passwords: list[tuple[str, str]] = []

    try:
        for p in parsed:
            existing = db.query(User).filter(User.email == p["email"]).one_or_none()
            if existing and not ns.update:
                print(f"  SKIP    {p['email']:<40}  (exists; pass --update to modify)")
                skipped += 1
                continue
            if existing:
                existing.name = p["name"]
                existing.role = p["role"]
                if p.get("password") and p.get("password_was_generated") is False:
                    existing.password_hash = hash_password(p["password"])
                # When CSV had blank password we leave the existing hash alone
                # (avoid surprise password changes during a bulk re-run).
                print(f"  UPDATE  {p['email']:<40}  role={p['role'].value}")
                updated += 1
            else:
                u = User(
                    id=generate_uuid(),
                    email=p["email"],
                    name=p["name"],
                    password_hash=hash_password(p["password"]),
                    role=p["role"],
                    is_active=True,
                )
                db.add(u)
                if p["password_was_generated"]:
                    generated_passwords.append((p["email"], p["password"]))
                print(f"  CREATE  {p['email']:<40}  role={p['role'].value}")
                created += 1
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"ERROR: rolled back after {created} creates / {updated} updates: {e}",
              file=sys.stderr)
        return 1
    finally:
        db.close()

    print()
    print(f"Done. created={created}  updated={updated}  skipped={skipped}")
    if generated_passwords:
        print()
        print("=" * 70)
        print("GENERATED PASSWORDS — SHOWN ONCE — RECORD THESE NOW")
        print("=" * 70)
        for email, pw in generated_passwords:
            print(f"  {email:<40}  {pw}")
        print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
