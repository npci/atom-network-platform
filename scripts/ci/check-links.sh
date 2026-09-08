#!/usr/bin/env bash
# Fail on broken relative links in tracked markdown.
#
# WHY this exists: two dead links had already been found BY HAND in this repo —
# one written as if from the repository root but living a directory down, and
# one pointing at a design document that had since been deleted. Both were found
# only because someone happened to look. A link that resolved when it was
# written and rotted later is invisible until a reader hits it, and in a
# published repo that reader is a stranger deciding whether to trust the docs.
#
# Pure bash + git, no python or node, so it runs anywhere hygiene-check.sh runs.
#
# Checks only RELATIVE links. External URLs are deliberately out of scope:
# verifying them needs network access, turns a deterministic gate into a flaky
# one, and fails on any site that blocks CI egress.
#
# KNOWN LIMITATION, and it bit immediately: this matches link syntax anywhere in
# the file, including inside backtick code spans. A document that *illustrates*
# markdown link syntax will therefore be reported as broken — WIKI_PLAN.md was,
# on this script's first run, for quoting the very false-positive pattern the
# exclusion below exists for. Code-span awareness is not worth the bash; write
# the example so it does not contain a literal `](target)` instead.
set -uo pipefail

cd "$(dirname "$0")/../.."

# If you ever add a tree of verbatim transcripts, exclude it below. Quoted text
# containing markdown-shaped fragments scans as hundreds of broken links, every
# one a false positive, and a gate that cries wolf gets ignored and then deleted.
#
# No `mapfile`: it is bash 4+, and macOS ships bash 3.2. This script must run
# on a developer laptop as well as in CI.
broken=0
scanned=0
while IFS= read -r f; do
  scanned=$((scanned + 1))
  dir=$(dirname "$f")
  while IFS= read -r raw; do
    # strip the surrounding ]( and )
    target=${raw#](}
    target=${target%)}
    case "$target" in
      http://*|https://*|mailto:*|'#'*|'') continue ;;
    esac
    # drop any #fragment and anything after a space (markdown titles)
    target=${target%%#*}
    target=${target%% *}
    [ -z "$target" ] && continue
    if [ ! -e "$dir/$target" ]; then
      printf 'BROKEN  %s -> %s\n' "$f" "$target"
      broken=$((broken + 1))
    fi
  done < <(grep -oE '\]\([^)]+\)' "$f" 2>/dev/null || true)
done < <(git ls-files '*.md' | grep -v '/node_modules/')

if [ "$broken" -ne 0 ]; then
  printf '\n%s broken relative link(s). Fix the target, or the link.\n' "$broken"
  exit 1
fi

printf 'ok    relative markdown links       0 broken (%s files)\n' "$scanned"
