#!/usr/bin/env python3
# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""CI guard: every route in an `/admin`-prefixed router MUST declare the
`AdminUser` (or `require_admin`) FastAPI dependency in its signature.

Closes THREAT_MODEL.md T9 ("No automated check that every admin route
declares AdminUser") — "a missing dependency on one route is a silent
privilege-escalation path that code review alone may not catch."

Usage:
    python backend/scripts/ci/check_admin_routes.py

Exits non-zero (failing the CI job that invokes it) if any route handler
under an `/admin`-prefixed router is missing the dependency. Discovers
admin-prefixed routers automatically (`APIRouter(prefix="/admin...")`) so
it does not need to be updated by hand every time a new admin router is
added — only routers that ADD the admin prefix need this guard, and any
new one is picked up the next time this script runs.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[2] / "app" / "api"

_PREFIX_RE = re.compile(r'APIRouter\([^)]*prefix\s*=\s*["\'](/admin[^"\']*)["\']', re.DOTALL)
_ROUTE_RE = re.compile(r'@router\.(get|post|patch|put|delete)\(')
_DEF_RE = re.compile(r'^\s*(?:async\s+)?def\s+(\w+)\s*\(')
_SIG_END_RE = re.compile(r'\)\s*(->[^:]*)?:\s*$')


def find_admin_routers() -> list[Path]:
    """Any .py file under app/api/ whose APIRouter() declares a prefix
    starting with /admin. This is a heuristic (regex, not an AST/import
    walk) — deliberately simple so it has no import-time dependency on the
    application itself (fast, no DB/Redis/env needed to run in CI)."""
    hits = []
    for fp in sorted(API_DIR.glob("*.py")):
        text = fp.read_text(encoding="utf-8")
        if _PREFIX_RE.search(text):
            hits.append(fp)
    return hits


def check_file(fp: Path) -> list[str]:
    """Returns a list of violation strings (empty if the file is clean)."""
    violations = []
    lines = fp.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        if _ROUTE_RE.search(lines[i]):
            route_line = i + 1
            j = i + 1
            sig_lines: list[str] = []
            # Collect lines until we see the signature's closing `):` or
            # `) -> ReturnType:` — bounded lookahead so a malformed file
            # can't spin this loop forever.
            while j < len(lines) and j < i + 60:
                sig_lines.append(lines[j])
                if _SIG_END_RE.search(lines[j]) and any(_DEF_RE.match(x) for x in sig_lines):
                    break
                j += 1
            sig_text = "\n".join(sig_lines)
            if "AdminUser" not in sig_text and "require_admin" not in sig_text:
                snippet = sig_text.strip().splitlines()[0][:120] if sig_text.strip() else "<no signature found>"
                violations.append(f"{fp}:{route_line}: missing AdminUser/require_admin dependency — {snippet!r}")
            i = j
        i += 1
    return violations


def main() -> int:
    if not API_DIR.exists():
        print(f"ERROR: {API_DIR} not found — run from the repo root or adjust API_DIR.")
        return 2

    admin_files = find_admin_routers()
    if not admin_files:
        print("No admin-prefixed routers found — nothing to check (this is suspicious "
              "if the platform is expected to have an admin API; verify API_DIR is correct).")
        return 0

    all_violations: list[str] = []
    for fp in admin_files:
        all_violations.extend(check_file(fp))

    print(f"Checked {len(admin_files)} admin-prefixed router file(s).")
    if all_violations:
        print(f"\nFAIL — {len(all_violations)} route(s) missing AdminUser/require_admin:\n")
        for v in all_violations:
            print(f"  {v}")
        return 1

    print("PASS — every route in every /admin-prefixed router declares AdminUser/require_admin.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
