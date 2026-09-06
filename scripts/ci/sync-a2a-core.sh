#!/usr/bin/env bash
# Vendor the canonical A2A wire code into every service tree (QUAL-4).
#
# WHY VENDORING AND NOT A PACKAGE: each Python service builds from its OWN
# Docker context (./backend, ./partner-platform/backend, ./cert-agent,
# ./bank-agent). A Dockerfile cannot COPY outside its context, so an installable
# package at the repo root cannot reach any of these images without moving every
# build context to the repo root. cert-agent's own a2a_common/__init__.py already
# states the constraint: "separate images and cannot share on-disk code."
#
# So the copies stay — but they become GENERATED ARTIFACTS with exactly one
# editable source. scripts/ci/hygiene-check.sh hard-fails if any copy drifts.
#
# Usage:
#   scripts/ci/sync-a2a-core.sh           # write the copies
#   scripts/ci/sync-a2a-core.sh --check   # exit 1 if any copy is stale (no writes)
set -euo pipefail

cd "$(dirname "$0")/../.."
CANON_DIR="packages/a2a-core/a2a_common"
MANIFEST="packages/a2a-core/MANIFEST"

check_only=0
[ "${1:-}" = "--check" ] && check_only=1

# The banner is added to the COPIES only — the canonical file must not tell its
# own editor not to edit it. hygiene-check strips it back off before comparing,
# keyed on these two sentinels, so the banner text can change freely.
banner() {
    cat <<EOF
# >>> a2a-core vendored header >>>
# GENERATED FILE — DO NOT EDIT HERE. Your change will be overwritten.
#
# Canonical source: $CANON_DIR/$1
# Edit there, then run: scripts/ci/sync-a2a-core.sh
#
# This is security-critical A2A wire code shared byte-for-byte across services
# that cannot import each other (separate Docker build contexts). A fix applied
# to one copy and forgotten on the others is the failure mode this guards.
# <<< a2a-core vendored header <<<
EOF
}

stale=0
written=0
while read -r file dests; do
    [ -z "${file:-}" ] && continue
    case "$file" in \#*) continue ;; esac

    canon="$CANON_DIR/$file"
    if [ ! -f "$canon" ]; then
        printf 'ERROR canonical file missing: %s\n' "$canon" >&2
        exit 2
    fi

    for dest_dir in $dests; do
        dest="$dest_dir/$file"
        if [ ! -d "$dest_dir" ]; then
            printf 'ERROR destination dir missing: %s\n' "$dest_dir" >&2
            exit 2
        fi
        tmp="$(mktemp)"
        { banner "$file"; cat "$canon"; } > "$tmp"

        if [ -f "$dest" ] && cmp -s "$tmp" "$dest"; then
            rm -f "$tmp"
            continue
        fi
        if [ "$check_only" = 1 ]; then
            printf 'STALE %s\n' "$dest"
            stale=1
            rm -f "$tmp"
        else
            mv "$tmp" "$dest"
            printf 'wrote %s\n' "$dest"
            written=$((written + 1))
        fi
    done
done < <(grep -vE '^\s*(#|$)' "$MANIFEST")

if [ "$check_only" = 1 ]; then
    [ "$stale" = 1 ] && { echo "a2a-core copies are STALE — run scripts/ci/sync-a2a-core.sh"; exit 1; }
    echo "a2a-core copies are in sync"
    exit 0
fi

echo "a2a-core sync complete ($written file(s) updated)"
