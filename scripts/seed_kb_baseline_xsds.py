# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""One-time chore — seed knowledge_base/existing_xsds/ from precert-bank-sim.

The cert-testcase engine's `xsd_diff.compute_xsd_change` falls back to
`_kb_schema_content()` for changes that don't carry an agentic workspace
clone (i.e. UI-uploaded XSDs). That helper queries `document_chunks`
filtered on `DocCategory.XSD | AUTHORITY_XML_SPEC`. In a fresh clone those
folders are empty on disk, so the differ has nothing to diff against and
every field looks "added".

This script copies the 55 canonical network schemas out of
`precert-bank-sim/src/main/resources/schema/` into
`knowledge_base/existing_xsds/` and triggers `ingest_all()`, which is
idempotent — safe to re-run.

Run inside the backend container so `settings.knowledge_base_dir` and
the DB are wired:

    docker compose run --rm backend python scripts/seed_kb_baseline_xsds.py

Pass `--force` to re-ingest already-ingested files (useful when the
canonical schemas change upstream).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source",
        default="/app/precert-bank-sim/src/main/resources/schema",
        help="Directory holding the canonical .xsd files "
             "(default: /app/precert-bank-sim/src/main/resources/schema)."
    )
    parser.add_argument(
        "--dest-subfolder",
        default="existing_xsds",
        help="Subfolder under knowledge_base/ to seed (default: existing_xsds)."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force reingest of already-ingested chunks."
    )
    parser.add_argument(
        "--no-ingest",
        action="store_true",
        help="Copy the files but skip ingest_all()."
    )
    args = parser.parse_args()

    # Late import so `--help` works without the app being fully wired.
    from app.core.config import settings
    kb_dir = Path(settings.knowledge_base_dir)
    dest = kb_dir / args.dest_subfolder
    dest.mkdir(parents=True, exist_ok=True)

    src = Path(args.source)
    if not src.exists():
        # In-container run without precert-bank-sim mounted is common —
        # if the destination already has content the operator staged
        # on the host, proceed to ingest instead of erroring.
        already_staged = sorted(dest.glob("*.xsd"))
        if already_staged:
            print(
                f"source {src} not visible; found {len(already_staged)} "
                f".xsd already staged in {dest} — proceeding to ingest."
            )
        else:
            print(f"error: source directory not found: {src}", file=sys.stderr)
            print(
                "hint: either bind-mount precert-bank-sim into the container, or "
                "copy the .xsd files onto the host into knowledge_base/existing_xsds/ "
                "and re-run this script.",
                file=sys.stderr,
            )
            return 2
    else:
        xsds = sorted(src.glob("*.xsd"))
        if not xsds:
            print(f"error: no .xsd files found in {src}", file=sys.stderr)
            return 2
        copied = 0
        for xsd in xsds:
            target = dest / xsd.name
            if target.exists() and target.stat().st_mtime >= xsd.stat().st_mtime and not args.force:
                continue
            shutil.copy2(xsd, target)
            copied += 1
        print(f"copied {copied}/{len(xsds)} baseline XSD(s) → {dest}")

    if args.no_ingest:
        print("skipped ingest (--no-ingest); run ingest_all() manually to index.")
        return 0

    from app.core.database import SessionLocal
    from app.rag.ingestion import ingest_all

    db = SessionLocal()
    try:
        summary = ingest_all(db, force=args.force)
        db.commit()
    finally:
        db.close()

    print("ingest summary:", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
