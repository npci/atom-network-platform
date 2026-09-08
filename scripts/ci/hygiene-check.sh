#!/usr/bin/env bash
# Repository hygiene gate.
#
# Catches the classes of exposure that a SECRET scanner structurally cannot:
# confidential binaries, third-party trademarks, internal hostnames, personal
# data, and the reintroduction of directories we deliberately deleted.
#
# Two kinds of rule:
#   HARD  — count must be zero. Any hit is a regression.
#   RATCHET — count must not EXCEED a recorded baseline. For cleanup that is
#             real but incomplete: it locks in progress without blocking merges
#             on work that has not happened yet. Lower the baseline as you fix;
#             the gate then holds the new floor.
#
# SITE-SPECIFIC PATTERNS LIVE OUTSIDE THIS FILE. An operator's internal
# hostnames, subnets and trademarks are themselves sensitive: a gate that
# hardcodes them publishes the very list it exists to protect. Put them in an
# untracked `scripts/ci/hygiene-patterns.local`; see
# `scripts/ci/hygiene-patterns.local.example` for the format. Rules with no
# configured pattern report `skip` rather than a reassuring `ok`.
#
# Run locally exactly as CI does:   bash scripts/ci/hygiene-check.sh
#
# ⚠️ NEVER USE `\b` IN A RULE. `git grep -E` does not support word boundaries and
# matches NOTHING silently — a rule written with `\b` reports a permanent, cheery
# zero. This exact bug made the first exposure sweep miss every hardcoded bank
# name, and then reappeared in the first draft of THIS file. The canary below
# exists so it can never pass unnoticed a third time.
set -uo pipefail
cd "$(dirname "$0")/../.."

fail=0

# ── Site-specific patterns (optional, untracked) ─────────────────────────────
# Defaults are EMPTY. An unset pattern disables its rule with a visible `skip`,
# never a silent pass.
INTERNAL_HOSTNAME_RE=''      # e.g. '[a-z0-9-]+\.corp\.internal'
INTERNAL_SUBNET_RE=''        # e.g. '10\.(211|9)\.[0-9]{1,3}\.[0-9]{1,3}'
INTERNAL_HOST_RE=''          # e.g. '(repo|gateway|directory)\.example\.com'
TRADEMARK_GLOBS=()           # e.g. ('*acme_logo*')
RUNTIME_STORE_GLOBS=()       # committed runtime stores specific to your deploy
CONTRIB_NAME_IGNORE=''       # names that collide with ordinary English
# shellcheck source=/dev/null
[ -f scripts/ci/hygiene-patterns.local ] && . scripts/ci/hygiene-patterns.local

# ── Canary ───────────────────────────────────────────────────────────────────
# A rule that MUST match. If the regex engine silently stops matching (the `\b`
# class of failure, a git-grep behaviour change, running outside the repo), every
# other rule would report a false, reassuring zero. Fail loudly instead.
#
# Matches "Copyright" rather than the licence NAME. The previous version grepped
# LICENSE for 'apache|Apache', which made the canary itself licence-specific: the
# move from Apache-2.0 to MIT turned a working scanner into a FATAL abort, with a
# message blaming the regex engine for a licence change. Every licence carries a
# copyright line, so this version survives the next one too.
canary=$(git grep -lIE 'Copyright' -- LICENSE 2>/dev/null | wc -l | tr -d ' ')
if [ "$canary" -eq 0 ]; then
  echo "FATAL: canary rule matched nothing — the scanner is broken, not the tree."
  echo "       Every result below would be a false negative. Refusing to pass."
  exit 2
fi

# tracked() greps only files git tracks, so build output and node_modules can
# never trip or mask a rule.
tracked_count() { git grep -lIE "$1" -- . 2>/dev/null | grep -vcE "${2:-^$}" || true; }

hard() { # name, regex, [exclude-regex]
  local name="$1" re="$2" exc="${3:-^$}" n
  n=$(tracked_count "$re" "$exc")
  if [ "$n" -gt 0 ]; then
    printf 'FAIL  %-34s %s file(s), expected 0\n' "$name" "$n"
    git grep -lIE "$re" -- . 2>/dev/null | grep -vE "$exc" | sed 's/^/        /'
    fail=1
  else
    printf 'ok    %-34s 0\n' "$name"
  fi
}

# Like hard(), but the pattern is site-specific and may be unconfigured. An
# empty pattern SKIPS loudly. It must never read as a pass: a rule that cannot
# match anything reporting `ok` is the exact failure this gate exists to catch.
hard_local() { # name, regex-or-empty, [exclude-regex]
  local name="$1" re="$2" exc="${3:-^$}"
  if [ -z "$re" ]; then
    printf 'skip  %-34s no pattern in hygiene-patterns.local\n' "$name"
    return
  fi
  hard "$name" "$re" "$exc"
}

ratchet() { # name, regex, baseline, [exclude-regex]
  local name="$1" re="$2" base="$3" exc="${4:-^$}" n
  n=$(tracked_count "$re" "$exc")
  if [ "$n" -gt "$base" ]; then
    printf 'FAIL  %-34s %s file(s), baseline %s — NEW occurrences added\n' "$name" "$n" "$base"
    git grep -lIE "$re" -- . 2>/dev/null | grep -vE "$exc" | sed 's/^/        /'
    fail=1
  elif [ "$n" -lt "$base" ]; then
    printf 'ok    %-34s %s (baseline %s — improved, LOWER THE BASELINE)\n' "$name" "$n" "$base"
  else
    printf 'ok    %-34s %s (at baseline)\n' "$name" "$n"
  fi
}

ratchet_in_path() { # name, path, regex, baseline
  # Counts OCCURRENCES inside one path. ratchet() counts FILES across the whole
  # repo, which is the wrong unit here: this cleanup is measured in hits, and it
  # is scoped to the directories that must end at zero.
  #
  # The path is ASSERTED to exist first. Five rules in an earlier version of this
  # file gated directories that are not in this repository — partner-platform/**
  # and certagent/** — and every one printed a confident `ok  0` for a check that
  # could not fail. A zero you cannot distinguish from "nothing was scanned" is
  # worse than no rule, because it is quoted in review as evidence.
  local name="$1" path="$2" re="$3" base="$4" n
  if [ -z "$(git ls-files -- "$path" 2>/dev/null | head -1)" ]; then
    printf 'FAIL  %-34s path %s matches no tracked file — rule is dead\n' "$name" "$path"
    fail=1; return
  fi
  n=$(git grep -ohIE "$re" -- "$path" 2>/dev/null | wc -l | tr -d ' ')
  n=${n:-0}
  if [ "$n" -gt "$base" ]; then
    printf 'FAIL  %-34s %s hits in %s, baseline %s — coupling INCREASED\n' \
           "$name" "$n" "$path" "$base"
    git grep -lIE "$re" -- "$path" 2>/dev/null | sed 's/^/        /'
    fail=1
  elif [ "$n" -lt "$base" ]; then
    printf 'ok    %-34s %s (baseline %s — improved, LOWER THE BASELINE)\n' "$name" "$n" "$base"
  else
    printf 'ok    %-34s %s (at baseline)\n' "$name" "$n"
  fi
}

bigfile_ratchet() { # name, glob-path, line-threshold, baseline-count
  # "God files" are the top onboarding obstacle for outside contributors and are
  # unreviewable in one sitting. Splitting them is a long, per-PR job; this gate
  # just stops NEW ones appearing while that happens.
  local name="$1" path="$2" thresh="$3" base="$4" n
  n=$(git ls-files "$path" | xargs wc -l 2>/dev/null \
       | awk -v t="$thresh" '$2!="total" && $1>t {c++} END{print c+0}')
  n=${n:-0}
  if [ "$n" -gt "$base" ]; then
    printf 'FAIL  %-34s %s file(s) over %s lines, baseline %s — NEW god file\n' "$name" "$n" "$thresh" "$base"
    git ls-files "$path" | xargs wc -l 2>/dev/null | awk -v t="$thresh" '$2!="total" && $1>t {printf "        %6s  %s\n",$1,$2}' | sort -rn
    fail=1
  elif [ "$n" -lt "$base" ]; then
    printf 'ok    %-34s %s (baseline %s — improved, LOWER THE BASELINE)\n' "$name" "$n" "$base"
  else
    printf 'ok    %-34s %s (at baseline)\n' "$name" "$n"
  fi
}

hard_value() { # name, regex, benign-match-regex, [exclude-path-regex]
  # Like hard(), but filters on the MATCHED TEXT rather than the file.
  #
  # Needed because a credential pattern can collide with ordinary identifiers:
  # `a2a_[A-Za-z0-9_-]{40,}` matches the test function
  # `a2a_rejects_a_message_kind_the_transport_cannot_carry`. RE2/ERE have no
  # lookahead, so "40+ chars including an uppercase or digit" cannot be written
  # as one expression — filtering the matches afterwards is the equivalent.
  local name="$1" re="$2" benign="$3" exc="${4:-^$}" hits n
  hits=$(git grep -onIE "$re" -- . 2>/dev/null | grep -vE "$exc" \
         | awk -F: -v b="$benign" '$3 !~ b' || true)
  n=$(printf '%s' "$hits" | grep -c . || true)
  if [ "$n" -gt 0 ]; then
    printf 'FAIL  %-34s %s match(es), expected 0\n' "$name" "$n"
    printf '%s\n' "$hits" | sed 's/^/        /'
    fail=1
  else
    printf 'ok    %-34s 0\n' "$name"
  fi
}

paths_absent() { # name, one-or-more path globs
  local name="$1"; shift
  local hits; hits=$(git ls-files -- "$@" | head -20)
  if [ -n "$hits" ]; then
    printf 'FAIL  %-34s tracked, expected none\n' "$name"
    printf '%s\n' "$hits" | sed 's/^/        /'
    fail=1
  else
    printf 'ok    %-34s none tracked\n' "$name"
  fi
}

echo "── Confidential / IP ──────────────────────────────────────────────"
# Proprietary specs, contracts, decks and generated artifacts arrive as office
# documents, and those formats carry author metadata even when the body looks
# benign.
paths_absent "office+binary documents" \
  '*.pdf' '*.docx' '*.xlsx' '*.pptx' '*.zip'
# The MIT License conveys no trademark rights (it says nothing about them); a
# third party's mark was never this project's to sublicense. Site-specific: the
# marks an operator must not ship are the operator's to name.
if [ "${#TRADEMARK_GLOBS[@]}" -gt 0 ]; then
  paths_absent "trademark assets" "${TRADEMARK_GLOBS[@]}"
else
  printf 'skip  %-34s no globs in hygiene-patterns.local\n' "trademark assets"
fi
# `:!**/.gitignore` exempts the self-ignoring placeholder that makes a runtime
# directory exist in a fresh clone without its CONTENTS being tracked. The rule
# is about committed runtime artifacts; a lone .gitignore is the mechanism that
# PREVENTS them, so flagging it inverted the intent.
paths_absent "scrap/ + artifacts/" 'scrap/**' 'artifacts/**' ':!**/.gitignore'
# Committed runtime stores hold real production or certification data. Which
# paths those are depends on the deployment, so they are site-configured.
if [ "${#RUNTIME_STORE_GLOBS[@]}" -gt 0 ]; then
  paths_absent "runtime data stores" "${RUNTIME_STORE_GLOBS[@]}"
else
  printf 'skip  %-34s no globs in hygiene-patterns.local\n' "runtime data stores"
fi
paths_absent "committed build output" '**/dist/**' '**/dist.zip'
# A committed SQLite file is a runtime store that looks like source. Even at
# zero bytes it invites `git add` of the populated version later, and the
# populated version is whatever the last developer happened to be testing with.
paths_absent "tracked databases" '*.db' '*.sqlite' '*.sqlite3'
# The counterpart to the .env allowlist in .gitleaks.toml. Untracked .env files
# hold real secrets by design and gitleaks is told to skip them; a TRACKED one
# is the actual incident, and that is what this catches. `.env.example` is
# committed on purpose and must not trip it.
env_tracked=$(git ls-files -- '*.env' '.env' '**/.env' 2>/dev/null | grep -v '\.example$' || true)
if [ -n "$env_tracked" ]; then
  printf 'FAIL  %-34s tracked, expected none\n' "tracked .env files"
  printf '%s\n' "$env_tracked" | sed 's/^/        /'
  fail=1
else
  printf 'ok    %-34s none tracked\n' "tracked .env files"
fi

echo
echo "── Credentials (shapes gitleaks does not ship) ────────────────────"
# Body length keeps prose mentions of a credential format out of the results.
#
# Excluded: .gitleaks.toml (a rules file necessarily quotes examples of what it
# matches — it flagged itself over a comment citing a test name) and the three
# test modules whose entire job is to hold fake secrets and prove the scrubber
# removes them. The same fixtures are allowlisted BY VALUE in .gitleaks.toml, so
# a REAL token pasted into one of those files still fails that gate; they are
# listed by path here only because this rule has no value-level allowlist.
CREDDOCS='^\.gitleaks\.toml$|^backend/tests/agents/test_workspace_secret_scrub\.py$|^backend/tests/core/test_audit_log_secrets\.py$|^backend/tests/agents/test_agentic_state\.py$'
# An all-lowercase "key" is a Python identifier, not a credential: a minted key
# is secrets.token_urlsafe(32) — 43 chars from a 64-symbol alphabet — so the
# odds of one being entirely [a-z_] are about 1e-16. Mirrors the same allowlist
# in .gitleaks.toml, so the two gates agree.
hard_value "a2a partner api key" 'a2a_[A-Za-z0-9_-]{40,}' '^a2a_[a-z_]+$' "$CREDDOCS"
hard "gitlab pat (with body)" 'glpat-[A-Za-z0-9_-]{20,}' "$CREDDOCS"
hard "llm api key (with body)" 'sk-(ant-api03|proj)-[A-Za-z0-9_-]{20,}' "$CREDDOCS"

echo
echo "── Internal infrastructure ────────────────────────────────────────"
# Internal hostnames, RFC1918 addresses and corporate registry/gateway hosts.
# There is no legitimate reason for one to sit in published source: they are
# reconnaissance for an attacker and dead references for everyone else.
#
# All three patterns are site-specific and live in hygiene-patterns.local — an
# operator's internal topology is exactly what should not be committed to a
# public repo, including inside the rule that forbids it.
hard_local "internal hostnames"      "$INTERNAL_HOSTNAME_RE"
hard_local "internal RFC1918 hosts"  "$INTERNAL_SUBNET_RE"
hard_local "internal corporate hosts" "$INTERNAL_HOST_RE"

echo
echo "── Personal data ─────────────────────────────────────────────────"
# Contributors' names have shown up here as identifiers, TODO owners, mock
# addressing handles and absolute /Users/<name>/ paths — one of which was also a
# live config default, so it was a bug as well as a disclosure. Publishing an
# OSS repo should not tell the world who wrote which TODO.
#
# The names are DERIVED FROM `git log` AT RUNTIME and never written down here.
# The previous version hardcoded seven of them, which was wrong twice over: the
# gate published the roster it existed to protect (and needed a self-exclusion
# to survive its own rule), and it was an ALLOWLIST — a contributor whose name
# was not on it stayed invisible no matter how often the gate ran. Deriving from
# history is self-maintaining and discloses nothing new: the same names are
# already in every clone's commit metadata.
#
# Matching is plain and case-insensitive, deliberately. Requiring a non-letter
# on either side would drop real hits like a `gitlab.com/<Name>N/...` URL. The
# cost is that a contributor whose name is also an ordinary English word will
# produce noise; CONTRIB_NAME_IGNORE in hygiene-patterns.local is the documented
# escape hatch for that, and it takes a name rather than a file path so the
# exemption cannot be quietly widened into a whole directory.
if [ "$(git rev-parse --is-shallow-repository 2>/dev/null)" = "true" ]; then
  printf 'FAIL  %-34s shallow clone — needs full history (fetch-depth: 0)\n' "contributor names"
  fail=1
else
  contrib_names=$(git log --format='%an%n%cn' 2>/dev/null \
    | tr ' .-' '\n\n\n' | tr -d '"'\''“”‘’' \
    | grep -E '^[A-Za-z]{5,}$' \
    | grep -viE '^(bot|noreply|github|actions|admin|user|root|none|unknown|dependabot|renovate|claude)$' \
    | { if [ -n "$CONTRIB_NAME_IGNORE" ]; then grep -viE "^($CONTRIB_NAME_IGNORE)$"; else cat; fi; } \
    | sort -u | paste -sd'|' -)
  if [ -z "$contrib_names" ]; then
    printf 'FAIL  %-34s derived no names from git log — rule is dead\n' "contributor names"
    fail=1
  else
    hits=$(git grep -lIiE "($contrib_names)" -- . 2>/dev/null || true)
    n=$(printf '%s' "$hits" | grep -c . || true)
    if [ "${n:-0}" -gt 0 ]; then
      printf 'FAIL  %-34s %s file(s), expected 0\n' "contributor names" "$n"
      printf '%s\n' "$hits" | sed 's/^/        /'
      fail=1
    else
      printf 'ok    %-34s 0\n' "contributor names"
    fi
  fi
fi
# Personal mailbox providers. Role addresses (security@, noreply@) are fine;
# an individual's private mailbox in a public repo is not.
hard "personal email addresses" '[A-Za-z0-9._%+-]+@(gmail|yahoo|outlook|hotmail|rediffmail|proton(mail)?)\.com'
# A macOS /Users/<name>/ path leaks the author's username and local layout —
# and one was a live config default (agentic_workspace_root), so it was a bug
# as well as a disclosure. /home/<svc>/ is NOT matched: appuser and claude are
# container service accounts and legitimate.
hard "absolute home paths"   '/Users/[a-z][a-z0-9._-]{2,}/'

echo
echo "── Publication placeholders ──────────────────────────────────────"
# A SECURITY.md that names an address nobody reads is worse than no policy at
# all: reports vanish silently and the reporter believes they were filed. Same
# for a governance table of placeholder maintainers. HARD, not a ratchet —
# these must be resolved before the repo is published, not trended downward.
#
# `SELF` is still needed here, and only here: these rules name their own
# placeholder tokens, so the gate necessarily contains the strings it forbids.
SELF='^scripts/ci/hygiene-check\.sh$'
hard "unfilled contact placeholder"    'OSS_CONTACT_EMAIL'        "$SELF"
hard "unfilled maintainer placeholder" 'MAINTAINER_(NAME|GITHUB)' "$SELF"
#
# `<SET-A-MONITORED-ADDRESS>` in SECURITY.md / CODE_OF_CONDUCT.md / TRADEMARKS.md
# is NOT gated here, and that is a deliberate call rather than an oversight.
# Whoever publishes this repository has to supply a mailbox; nobody in CI can.
# Failing every pull request on a fact only the publisher can change is how a
# gate gets marked `continue-on-error` and then ignored — which is exactly the
# state this file was rescued from. The rule that DOES bite is
# "undeliverable contact address" below: an address that looks real and silently
# discards mail is the dangerous form, and it is hard-gated. A visibly unfilled
# placeholder is honest, and a reader can see at a glance that email is not yet
# a route.
# RFC 2606 reserves example.com/.org/.net as guaranteed-undeliverable. In sample
# data that is exactly right and this rule must not touch it — seed scripts, CSV
# templates and test fixtures SHOULD use a reserved domain.
#
# It is only a defect in the documents that publish a REPORTING CHANNEL. There a
# reserved address is not a placeholder the reader can recognise: it looks real,
# accepts nothing, and discards every report in silence, so the reporter believes
# they have filed something. That is strictly worse than having no policy, and it
# quietly contradicts any coordinated-disclosure promise made on the same page.
#
# Scoped to the policy files by path, because the distinction is about the
# document's job and cannot be read off the address itself. CONTRIBUTING.md and
# README.md are deliberately NOT in this list: they carry sample addresses (a
# `Signed-off-by:` trailer, a `create-user` invocation) where a reserved domain
# is the correct choice, and including them would make the rule cry wolf.
POLICY_DOCS=$(git ls-files -- 'SECURITY.md' 'CODE_OF_CONDUCT.md' 'GOVERNANCE.md' \
                             'TRADEMARKS.md' 'SUPPORT.md' '.github/*.md' 2>/dev/null)
if [ -z "$POLICY_DOCS" ]; then
  printf 'FAIL  %-34s no policy documents found — rule is dead\n' "undeliverable contact address"
  fail=1
else
  # shellcheck disable=SC2086
  bad_contact=$(git grep -lIE '[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)*example\.(com|org|net)' -- $POLICY_DOCS 2>/dev/null || true)
  n=$(printf '%s' "$bad_contact" | grep -c . || true)
  if [ "${n:-0}" -gt 0 ]; then
    printf 'FAIL  %-34s %s policy file(s) name an address that discards mail\n' "undeliverable contact address" "$n"
    printf '%s\n' "$bad_contact" | sed 's/^/        /'
    fail=1
  else
    printf 'ok    %-34s 0\n' "undeliverable contact address"
  fi
fi

echo
echo "── Licence headers ───────────────────────────────────────────────"
# Every Python file must declare its licence. The repository is MIT; a file
# with no SPDX line makes no claim at all, and one that claimed Apache-2.0
# contradicted LICENSE outright — 22 such files reached this branch before
# anyone noticed, because nothing looked.
#
# HARD, and inverted: the other checks count occurrences of something bad, so
# a ratchet works. This counts files MISSING something required, which has
# exactly one acceptable value. A baseline here would just be a number to
# creep upward.
spdx_missing=$(
  git ls-files -- 'backend/app/**/*.py' 'backend/tests/**/*.py' 'packages/**/*.py' 2>/dev/null \
    | while read -r f; do
        grep -qF 'SPDX-License-Identifier' "$f" 2>/dev/null || printf '%s\n' "$f"
      done | wc -l | tr -d ' '
)
spdx_wrong=$(git grep -lIF 'SPDX-License-Identifier: Apache-2.0' -- '*.py' 2>/dev/null | wc -l | tr -d ' ')
if [ "${spdx_missing:-0}" -gt 0 ]; then
  printf 'FAIL  %-34s %s python file(s) carry no SPDX header\n' "missing licence header" "$spdx_missing"
  git ls-files -- 'backend/app/**/*.py' 'backend/tests/**/*.py' 'packages/**/*.py' 2>/dev/null \
    | while read -r f; do grep -qF 'SPDX-License-Identifier' "$f" 2>/dev/null || printf '        %s\n' "$f"; done
  fail=1
else
  printf 'ok    %-34s every python file declares one\n' "missing licence header"
fi
if [ "${spdx_wrong:-0}" -gt 0 ]; then
  printf 'FAIL  %-34s %s file(s) still declare Apache-2.0; LICENSE is MIT\n' "wrong licence header" "$spdx_wrong"
  git grep -lIF 'SPDX-License-Identifier: Apache-2.0' -- '*.py' 2>/dev/null | sed 's/^/        /'
  fail=1
else
  printf 'ok    %-34s none contradict LICENSE\n' "wrong licence header"
fi

echo
echo "── Suppression debt ──────────────────────────────────────────────"
# Each `noqa` is a silenced linter and each `type: ignore` a silenced type
# checker. Individually reasonable, collectively a place for real warnings to
# hide. Ratchets only — no demand to clear them, just a floor that falls.
ratchet_in_path "noqa suppressions"        "backend/app" '# *noqa'            589
ratchet_in_path "type: ignore suppressions" "backend/app" '# *type: *ignore'  46

echo "── File size ─────────────────────────────────────────────────────"
# Sixteen modules exceed 1500 lines; the largest is over 5000. They are being
# split incrementally, so this ratchets the COUNT rather than demanding a
# rewrite. Splitting the governance orchestrator is deliberately NOT attempted:
# upstream ships it as one file and diverging here buys nothing functional.
bigfile_ratchet "backend files >1500 lines" 'backend/app/**.py' 1500 16

echo "── Sandbox invariant ─────────────────────────────────────────────"
# LLM-generated HTML (product-kit prototypes) is rendered via srcdoc in an
# iframe. `allow-scripts` alone is SAFE: the frame gets a null origin, so its
# JS cannot reach the parent DOM, cookies or localStorage. Combining it with
# `allow-same-origin` removes that boundary and turns a contained preview into
# stored XSS against the app origin — a one-word edit, invisible in review.
# There are 6 such iframes; none may ever carry both.
hard "iframe allow-same-origin+scripts" 'sandbox="[^"]*allow-same-origin[^"]*allow-scripts|sandbox="[^"]*allow-scripts[^"]*allow-same-origin' "$SELF"

echo "── Dependency lockfiles ──────────────────────────────────────────"
# A lockfile's failure mode is going stale in silence: someone bumps or adds a
# pin in requirements.txt, never regenerates, and the lock keeps certifying the
# OLD closure. Offline structural check — every `name[extras]==version` in
# requirements.txt must appear verbatim in the lock. It cannot verify the
# hashes (that needs the network); regeneration is the operation that does.
lock_fresh() {
    local name="$1" txt="$2/requirements.txt" lock="$2/$3" missing n
    if [ ! -f "$lock" ]; then
        printf 'FAIL  %-34s missing %s\n' "$name" "$3"; fail=1; return
    fi
    # Strip comments/blank lines, normalise case (pip-compile lowercases names).
    missing=$(grep -vE '^\s*(#|$)' "$txt" | sed 's/#.*//' | tr -d ' ' \
              | grep -E '==' | tr 'A-Z_' 'a-z-' | sort -u \
              | while read -r pin; do
                    # Escape EVERY ERE metacharacter, not just '['. A PEP 440
                    # local-version segment like torch==2.9.1+cpu contains '+',
                    # which is a quantifier in ERE — the pattern then matches
                    # "2.9.11cpu" and NOT the literal string, so a correctly
                    # locked pin was reported as absent. Cost an hour; escape
                    # the lot.
                    esc=$(printf '%s' "$pin" | sed 's/[][\\.*^$+?{}|()]/\\&/g')
                    grep -qiE "^${esc}( |\\\\|$)" "$lock" 2>/dev/null || echo "$pin"
                done)
    n=$(printf '%s' "$missing" | grep -c . || true)
    if [ "${n:-0}" -gt 0 ]; then
        printf 'FAIL  %-34s %s pin(s) absent from the lock — REGENERATE (see the lock header)\n' "$name" "$n"
        printf '%s\n' "$missing" | sed 's/^/        /'
        fail=1
    else
        printf 'ok    %-34s in sync\n' "$name"
    fi
}
# One lock PER ARCH: torch pulls NVIDIA CUDA wheels on x86_64 that do not exist
# in the aarch64 graph. Both must track requirements.txt or the arch you did not
# regenerate fails closed at build time.
lock_fresh "backend lock (arm64)"    backend                  requirements.arm64.lock
lock_fresh "backend lock (amd64)"    backend                  requirements.amd64.lock

echo "── Generated docs ────────────────────────────────────────────────"
# The prompt catalogue is generated from backend/app/prompts/**. Its hand-
# maintained ancestor reproduced every prompt verbatim and rotted on the first
# prompt change — a catalogue that disagrees with what the platform actually
# sends is worse than none, because it is quoted in review.
if out=$(python3 scripts/ci/generate-prompt-catalogue.py --check 2>&1); then
    printf 'ok    %-34s up to date\n' "prompt catalogue"
else
    printf 'FAIL  %-34s STALE — run scripts/ci/generate-prompt-catalogue.py\n' "prompt catalogue"
    printf '%s\n' "$out" | sed 's/^/        /'
    fail=1
fi

echo "── A2A vendored copies ───────────────────────────────────────────"
# hmac_signer / protocol / executor_base are VENDORED from packages/a2a-core by
# scripts/ci/sync-a2a-core.sh, which is the single editable source. This is
# security-critical wire code (HMAC signing, the JWT handshake, CIDR checks)
# living as several copies, so a fix applied to one and forgotten on the others
# is a real, recurring failure mode. The check fails if any copy has drifted.
if out=$(bash scripts/ci/sync-a2a-core.sh --check 2>&1); then
    printf 'ok    %-34s 0 stale\n' "a2a-core vendored copies"
else
    printf 'FAIL  %-34s STALE — run scripts/ci/sync-a2a-core.sh\n' "a2a-core vendored copies"
    printf '%s\n' "$out" | sed 's/^/        /'
    fail=1
fi
# The per-file mirror-drift rules that used to sit here compared this backend
# against partner-platform/** and cert-agent against bank-agent. Neither tree
# is in this repository — they are separate repos since the split — so all five
# rules reported `ok` for a comparison they never performed. Deleted rather than
# left as decoration; the vendored-copy check above is the real control, and it
# covers every tree that IS here.

echo "── Exception hygiene ─────────────────────────────────────────────"
# A bare `except:` also swallows KeyboardInterrupt and SystemExit, so a stuck
# request cannot be interrupted and shutdown can hang. There are currently ZERO
# (confirmed by AST walk, not regex — a regex for `except *:` matches the
# phrase "try/except:" in prose and reports false positives). HARD: never
# reintroduce one.
hard "bare except clauses"   '^[[:space:]]*except[[:space:]]*:' "$SELF"
# `except Exception` is often a deliberate fail-open guard here (telemetry, LLM
# formatting). At this volume it also hides real failures, and nothing stopped
# it growing. Ratchet: may fall, never rise. Give each NEW one an inline
# reason, or narrow the exception type.
ratchet_in_path "broad except Exception" "backend/app" 'except Exception' 1035

echo "── Internal vocabulary ───────────────────────────────────────────"
# Labels that mean something inside one organisation and nothing outside it:
# an internal spec document, sprint numbers, a commercial SAST vendor's name
# with its raw finding volumes, internal security-finding IDs, and pointers to
# a remediation programme whose FILENAMES alone publish its table of contents.
#
# These are not secrets. They are worse in one specific way: a reader cannot
# tell a real limitation from an internal ticket reference, so every one of them
# is a dead end that looks like documentation.
hard "internal spec label"     'THE BOOK'                    "$SELF"
# `backend/tests/eval/code_change_gold.jsonl` is excluded BY PATH and only that
# path: the label sits inside the `description` and `notes` of gold-set rows that
# are fed to the agent and scored. Editing them changes the eval INPUTS, so the
# scores either side of the change are not comparable. Retiring those rows is a
# gold-set decision, not a text edit.
hard "internal sprint label"   'Slice [0-9]'                 "$SELF|^backend/tests/eval/code_change_gold\.jsonl$"
hard "SAST vendor attribution" 'Checkmarx'                   "$SELF"
# RATCHET, not HARD, and the reason is scope rather than tolerance. The six
# remaining files are in backend/app and frontend/, and each hit is attached to a
# real engineering explanation of why a weaker option was removed — the ID is the
# only part that has to go, and rewriting those comments belongs with the people
# changing that code. Zero is the target; the count may fall and never rise.
ratchet "internal finding ids"    'CBOM-[A-Z]'  0                "$SELF"
hard "remediation-plan links"  'docs/genericization'         "$SELF"
# A competitor's product named as the thing being copied ("grok parity",
# "Aider-style"), which is what an agent docstring said and what the generated
# public agent catalogue therefore published. NOT a blanket ban on the word:
# app/services/video/grok.py is a legitimate provider integration and the
# provider is called what it is called. Only the comparison is forbidden.
# RATCHET for the same reason: what is left sits in agent bodies and
# core/config.py, where the phrase is load-bearing to the comment around it
# ("the convergence machinery <product> has and our legacy loop lacks") and the
# rewrite is a judgement call for whoever owns that module, not a substitution.
ratchet "competitor-parity claims" '[Gg]rok[ -](parity|build)|[Aa]ider-style' 6 "$SELF"
# Provenance stamps from a generator that named the tool and the operator.
hard "generator provenance"    'generated_by:'               "$SELF"

echo "── Domain coupling ───────────────────────────────────────────────"
# The finish line for genericization is not "zero in the repo" — the domain
# terms are the payments pack's whole value and MUST survive there. It is zero
# in core/ and adapters/, which must know about no ecosystem in particular.
#
# A ratchet, not a hard rule: this is a multi-phase cleanup and blocking every
# merge until it is finished would just get the gate deleted. The count may fall
# and never rise. Lower the baselines as phases land.
#
# `git grep -E` has NO word boundaries — a pattern using \b matches nothing and
# reports a cheerful zero. That bug has occurred three times in this work, so
# the control below asserts the pattern still matches SOMEWHERE before any zero
# is believed.
#
# Case variants are spelled out because this pattern is matched
# case-SENSITIVELY and REMITTER_BANK is a live enum value, not prose.
DOMAIN_TERMS='(NPCI|npci|UPI|upi_|BHIM|bhim|payment|Payment|VPA|vpa|IFSC|ifsc|REMITTER|Remitter|remitter|BENEFICIARY|Beneficiary|beneficiary)'
control=$(git grep -ohIE "$DOMAIN_TERMS" -- backend/app 2>/dev/null | wc -l | tr -d ' ')
if [ "${control:-0}" -lt 100 ]; then
  echo "FATAL: domain-term pattern matched $control times across backend/app."
  echo "       Expected hundreds. The pattern is broken, not the tree —"
  echo "       every count below would be a false zero. Refusing to pass."
  exit 2
fi

# Baselines MEASURED, not estimated. A first attempt guessed 2 for adapters/ and
# the gate immediately failed at 6 — which is also how it found that
# adapters/certification/upi.py was misfiled (a module named for the ecosystem
# it serves is pack content, not a generic adapter).
#
# What is LEFT in core/ is mostly not renameable: the copyright headers (which
# match `Payment`), the `npci_kg` / `atom_user` datastore names, a `REMITTER`
# certgroup VALUE, and the addressing-handle regex in pii_redaction.py — which
# has to name the thing it redacts.
#
# CAVEAT worth knowing: a term count cannot see the coupling that matters most.
# adapters/channel/a2a.py imports A2ATaskType and PartnerAgent from
# models/phase_c — the payments protocol enum and ORM model — and scores ZERO on
# this pattern. Driving these numbers down is necessary, not sufficient.
#
# MEASURE AND COMMIT IN ONE STEP. An earlier baseline said 61 and the gate failed
# on its own commit: the number was measured, another change landed in core/
# while the rest of the sweep finished, and the stale reading went in. A ratchet
# read at a different moment than it is committed is not a baseline, it is a
# guess — re-run the count immediately before staging this file.
ratchet_in_path "domain terms in core/"      "backend/app/core"       "$DOMAIN_TERMS" 18
ratchet_in_path "domain terms in adapters/"  "backend/app/adapters"   "$DOMAIN_TERMS" 1
ratchet_in_path "domain terms in a2a_common/" "backend/app/a2a_common" "$DOMAIN_TERMS" 12
ratchet_in_path "domain terms in frontend/"  "frontend/src"           "$DOMAIN_TERMS" 34
# The partner-platform ratchets that used to sit here gated
# partner-platform/frontend/src and partner-platform/backend, neither of which
# is in this repository. They reported `ok  0 (at baseline)` for directories
# that were never scanned. ratchet_in_path now FAILS on a path that matches no
# tracked file, so a rule like that cannot be added back silently.

# READMEs are the front door — the first thing a reader of a published repo sees,
# and the last place a stale domain name should survive. The residue is
# deliberate and cannot go to zero by rewording: real paths, contract enum values
# (PAYER_PSP, REMITTER_BANK) and third-party package names. Renaming any of those
# in prose makes the document false, not generic.
ratchet_in_path "domain terms in READMEs"    "*README.md"             "$DOMAIN_TERMS" 11

# The wiki's HAND-WRITTEN pages. The residue is the canonical repository URL in
# the pages that link to the partner platform. An org name inside a repository
# URL is not the coupling this ratchet exists to catch — it is an address, and it
# has to be that address. Keeping the baseline tight rather than loosening
# DOMAIN_TERMS means a genuine vocabulary leak in prose is still caught.
#
# The `:(glob)` prefix is load-bearing. Without it git's pathspec `*` also
# matches `/`, so `wiki/*.md` would sweep in wiki/reference/ — which is
# GENERATED from the code and legitimately carries domain terms (agent names,
# table names, API paths). Gating generated output would duplicate the gates
# already on the code it is derived from.
ratchet_in_path "domain terms in wiki/"      ":(glob)wiki/*.md"       "$DOMAIN_TERMS" 3

echo
echo "── Documentation links ───────────────────────────────────────────"
# HARD rule: zero broken relative links. Two had already been found by hand.
# Both were caught only because someone happened to inspect the exact line.
bash "$(dirname "$0")/check-links.sh" || fail=1

echo
if [ "$fail" -ne 0 ]; then
  echo "HYGIENE CHECK FAILED — each FAIL above names the rule and the files."
  exit 1
fi
echo "hygiene check passed"
