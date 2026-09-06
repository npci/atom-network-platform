# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Seed ``app_configs`` from the current .env / settings for schema-managed keys.

One-shot cutover helper for moving operator-tunable config + secrets from ``.env``
into the DB (the new single source of truth). For every key in the admin Config
schema that currently has a value in ``settings`` (i.e. from ``.env``/env), it
writes an ``app_configs`` row — encrypting secrets with ``CONFIG_ENCRYPTION_KEY``.

Idempotent: existing rows are left alone unless ``--force`` is passed. Secrets
require ``CONFIG_ENCRYPTION_KEY`` to be set (outside development).

Usage (inside the backend container)::

    docker compose run --rm backend python -m app.scripts.seed_config_from_env
    docker compose run --rm backend python -m app.scripts.seed_config_from_env --force
    docker compose run --rm backend python -m app.scripts.seed_config_from_env --dry-run

After verifying the values appear in Admin -> Configuration, remove the migrated
keys from ``backend/.env`` so there is exactly one source per property.
"""
import argparse
import sys

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.app_config_sync import encrypt_secret, encryption_available, is_encrypted
from app.models.app_config import AppConfig
from app.api.app_config import CONFIG_SCHEMA


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing app_configs rows too")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would change without writing")
    parser.add_argument("--encrypt-existing", action="store_true",
                        help="Also encrypt-in-place any secret rows already in the DB "
                             "that are still plaintext (uses the DB value, never .env)")
    args = parser.parse_args()

    secrets_present = any(
        c["is_secret"] and getattr(settings, c["key"], "") for c in CONFIG_SCHEMA
    )
    is_dev = (settings.app_env or "").lower() == "development"
    if not args.dry_run and secrets_present and not encryption_available() and not is_dev:
        print("ERROR: CONFIG_ENCRYPTION_KEY is required to seed secrets "
              "(app_env != development). Set it and re-run.", file=sys.stderr)
        return 2

    if args.encrypt_existing and not args.dry_run and not encryption_available():
        print("ERROR: --encrypt-existing needs CONFIG_ENCRYPTION_KEY set, or it would "
              "just rewrite plaintext. Set it and re-run.", file=sys.stderr)
        return 2

    secret_keys = {c["key"] for c in CONFIG_SCHEMA if c["is_secret"]}
    db = SessionLocal()
    seeded, skipped, cleared, reencrypted = [], [], [], []
    try:
        for schema in CONFIG_SCHEMA:
            key = schema["key"]
            is_secret = schema["is_secret"]
            value = str(getattr(settings, key, "") or "")
            if not value:
                cleared.append(key)  # nothing in env to seed
                continue

            existing = db.get(AppConfig, key)
            # Skip only rows that already hold a value — an empty existing row is
            # "unset", so filling it from .env is safe (nothing to clobber).
            if existing and existing.value and not args.force:
                skipped.append(key)
                continue

            label = f"{key} (secret, encrypted)" if is_secret else f"{key}={value}"
            if args.dry_run:
                print(f"  would write {label}")  # preview only — no encryption / no write
            else:
                stored = encrypt_secret(value) if is_secret else value
                if existing:
                    existing.value = stored
                else:
                    db.add(AppConfig(key=key, value=stored,
                                     category=schema["category"], is_secret=is_secret))
            seeded.append(key)

        # Re-encrypt plaintext secret rows already in the DB (uses the DB value,
        # never .env) so pre-encryption rows get protected without clobbering.
        if args.encrypt_existing:
            for row in db.query(AppConfig).all():
                if row.key not in secret_keys or not row.value or is_encrypted(row.value):
                    continue
                if args.dry_run:
                    print(f"  would encrypt-in-place {row.key} (secret, {len(row.value)} chars)")
                else:
                    row.value = encrypt_secret(row.value)
                reencrypted.append(row.key)

        if not args.dry_run:
            db.commit()
    finally:
        db.close()

    print(f"\nseeded={len(seeded)} skipped(existing)={len(skipped)} "
          f"no-env-value={len(cleared)} reencrypted={len(reencrypted)}")
    if seeded:
        print("  seeded:", ", ".join(seeded))
    if reencrypted:
        print("  re-encrypted in place:", ", ".join(reencrypted))
    if skipped:
        print("  skipped (row exists, use --force):", ", ".join(skipped))
    if args.dry_run:
        print("\n(dry run — nothing written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
