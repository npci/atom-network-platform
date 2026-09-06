#!/usr/bin/env bash
# Repository hygiene gate.
#
# Catches the classes of exposure that a SECRET scanner structurally cannot:
# confidential binaries, third-party trademarks, internal hostnames, and the
# reintroduction of directories we deliberately deleted.
#
# Every rule here traces to a real finding in
# docs/genericization/01-exposure-audit.md — nothing is speculative.
#
# Two kinds of rule:
#   HARD  — count must be zero. Any hit is a regression.
#   RATCHET — count must not EXCEED a recorded baseline. For cleanup that is
#             real but incomplete: it locks in progress without blocking merges
#             on work that has not happened yet. Lower the baseline as you fix;
#             the gate then holds the new floor.
#
# NO RATCHETS ARE CURRENTLY ACTIVE — PRs #3 and #4 drove every one to zero and
# they were promoted to HARD. `ratchet()` is retained deliberately: it is the
# right tool the next time a cleanup lands in stages, and re-deriving it under
# deadline is how these gates end up as "warn and forget" instead.
#
# Run locally exactly as CI does:   bash scripts/ci/hygiene-check.sh
#
# ⚠️ NEVER USE `\b` IN A RULE. `git grep -E` does not support word boundaries and
# matches NOTHING silently — a rule written with `\b` reports a permanent, cheery
# zero. This exact bug made the first exposure audit miss every hardcoded bank
# name (audit §G4), and then reappeared in the first draft of THIS file. The
# canary below exists so it can never pass unnoticed a third time.
set -uo pipefail
cd "$(dirname "$0")/../.."

fail=0

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
  local name="$1" path="$2" re="$3" base="$4" n
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

mirror_drift() { # name, filename, baseline-differing-lines
  # a2a_common is MIRRORED between the NPCI and partner
  # backends. This is security-critical wire code (HMAC signing, the JWT
  # handshake, CIDR checks) living as two copies, so a fix applied to one side
  # and forgotten on the other is a real, recurring failure mode.
  local name="$1" f="$2" base="$3" a b n
  a="backend/app/a2a_common/$f"; b="partner-platform/backend/app/a2a_common/$f"
  if [ ! -f "$a" ] || [ ! -f "$b" ]; then
    printf 'ok    %-34s (one side absent — not mirrored)\n' "$name"; return
  fi
  n=$(diff "$a" "$b" 2>/dev/null | grep -c '^[<>]')
  n=${n:-0}
  if [ "$n" -gt "$base" ]; then
    printf 'FAIL  %-34s %s differing lines, baseline %s — MIRROR DIVERGED\n' "$name" "$n" "$base"
    fail=1
  elif [ "$n" -lt "$base" ]; then
    printf 'ok    %-34s %s (baseline %s — converged, LOWER THE BASELINE)\n' "$name" "$n" "$base"
  else
    printf 'ok    %-34s %s (at baseline)\n' "$name" "$n"
  fi
}

bigfile_ratchet() { # name, glob-path, line-threshold, baseline-count
  # "God files" are the top onboarding obstacle for outside contributors and are
  # unreviewable in one sitting. Splitting them is a long, per-PR job (review
  # QUAL-1); this gate just stops NEW ones appearing while that happens.
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
# Audit B2-B6, F4: proprietary specs, contracts, decks, generated artifacts.
# Office formats carry author metadata (§D6/D7) even when the body looks benign.
paths_absent "office+binary documents" \
  '*.pdf' '*.docx' '*.xlsx' '*.pptx' '*.zip'
# Audit C1-C4. The MIT License conveys no trademark rights (it says nothing about
# them); the SBI mark was never NPCI's to sublicense.
paths_absent "trademark assets" \
  '*bhim_logo*' '*npci_logo*' '*NPCI_logo*' '*bank-logo*' '*npci_master*'
# Audit B1: Checkmarx reports + accepted-suppressions list for THIS codebase.
paths_absent "scrap/ + artifacts/" 'scrap/**' 'artifacts/**'
# Audit B7: committed runtime stores holding real certification runs.
paths_absent "certagent runtime stores" \
  'certagent/cert-agent/db/executions.json' \
  'certagent/cert-agent/db/test_cases.json' \
  'certagent/cert-agent/db/bank_configs.json' \
  'certagent/bank-agent/db/incoming_runs.json' \
  'certagent/bank-agent/db/bank_config.json'
paths_absent "committed build output" '**/dist/**' '**/dist.zip'
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
# Audit G7/G8. The bankkeys leak. Body length keeps prose mentions out.
#
# Excluded: docs/genericization/** (the audit describes these patterns) and
# .gitleaks.toml (a rules file necessarily quotes examples of what it matches —
# it flagged itself over a comment citing a test name). Both are documentation
# ABOUT credentials, never credentials.
# Files that legitimately contain credential-SHAPED strings: the exposure-audit
# prose, this repo's own scanner config, and the three test modules whose entire
# job is to hold fake secrets and prove the scrubber removes them. The same
# fixtures are allowlisted by value in .gitleaks.toml — listed there by value so
# a REAL token pasted into one of those files still fails that gate, and by path
# here because this rule has no value-level allowlist.
CREDDOCS='^docs/genericization/|^\.gitleaks\.toml$|^backend/tests/agents/test_workspace_secret_scrub\.py$|^backend/tests/core/test_audit_log_secrets\.py$|^backend/tests/agents/test_agentic_state\.py$'
# An all-lowercase "key" is a Python identifier, not a credential: a minted key
# is secrets.token_urlsafe(32) — 43 chars from a 64-symbol alphabet — so the
# odds of one being entirely [a-z_] are about 1e-16. Mirrors the same allowlist
# in .gitleaks.toml, so the two gates agree.
hard_value "a2a partner api key" 'a2a_[A-Za-z0-9_-]{40,}' '^a2a_[a-z_]+$' "$CREDDOCS"
hard "gitlab pat (with body)" 'glpat-[A-Za-z0-9_-]{20,}' "$CREDDOCS"
hard "anthropic key (with body)" 'sk-ant-api03-[A-Za-z0-9_-]{20,}' "$CREDDOCS"

echo
echo "── Internal infrastructure (audit §E — PR #3 drives these to 0) ───"
# RATCHET, not HARD: externalising these is PR #3's scope. Until then the gate
# guarantees the count cannot grow.
#
# JOURNALS is excluded from these two rules only. Claude_understand.md, the
# prompts log and docs/archive/ are dated historical records; rewriting them to
# scrub a hostname would falsify the record, and they are documentation of what
# was done rather than live configuration. The audit is excluded for the same
# reason it is in .gitleaks.toml — it is the document describing the problem.
JOURNALS='^docs/(genericization|archive)/|^docs/prompts_log\.md|^Claude_understand\.md'

# Paths that never reach the published repo (stripped when oss-publish is built).
# ONLY these may be skipped by the infrastructure rules below. docs/genericization
# IS published, so it is scanned like everything else -- it was excluded until
# 2026-08-11 and was quietly carrying three real internal IPs and two internal
# hostnames while this gate reported 0.
UNPUBLISHED='^(Claude_understand\.md|docs/prompts_log\.md|docs/archive/|docs/CODE_QUALITY_SECURITY_REVIEW\.md)'
# The gate names these patterns in its own rules, so it matches itself.
SELF='^scripts/ci/hygiene-check\.sh$'
# Baselines measured, not guessed. `git grep -lE <re> | grep -vE $JOURNALS | wc -l`
#
# PR #3 took this 5 → 1; PR #4 deleted the fabricated deploy that held the last
# one. HARD from here — there is no internal hostname left in the tree and no
# legitimate reason for one to return.
hard "internal hostnames (.npci.internal)" '[a-z0-9-]+\.npci\.internal' "$UNPUBLISHED"
# Promoted from ratchet to HARD by PR #3 — config.py no longer carries an
# internal build host or a reranker IP.
hard "internal RFC1918 addresses" '10\.(211|9)\.[0-9]{1,3}\.[0-9]{1,3}' "$UNPUBLISHED"
# Internal registry / gateway / directory hosts, externalised to build args and
# env by PR #3. HARD from the start: there is no legitimate reason for one to
# reappear in source. (www.npci.org.in is excluded — it is a PUBLIC website and,
# in xsd_namespace.py, an XML namespace URI, which is an identifier not an
# endpoint. Matching a leading label keeps those out.)
hard "internal npci.org.in hosts" '(repo|ainxt|ad|servicenow|platform|platform-host|developer)\.npci\.org\.in' "$UNPUBLISHED"

echo
echo
echo
echo
echo "── Personal data ─────────────────────────────────────────────────"
# Contributors' names appeared as identifiers (sasi_doc_type, _vimal_settings),
# TODO owners, mock VPAs, hardcoded SPOC emails in precert/, and an absolute
# /Users/<name>/ path that was ALSO a live config default. Publishing an OSS
# repo should not tell the world who wrote which TODO.
hard "contributor names"     '([Vv]imal|[Ss]asi|[Ss]hivdeep|[Vv]ijay|[Aa]bhishek|[Yy]ash)' "$UNPUBLISHED|$SELF"
# Personal mailbox providers. Role addresses (security@, noreply@) are fine;
# an individual's private mailbox in a public repo is not.
hard "personal email addresses" '[A-Za-z0-9._%+-]+@(gmail|yahoo|outlook|hotmail|rediffmail)\.com' "$UNPUBLISHED|$SELF"
# A macOS /Users/<name>/ path leaks the author's username and local layout —
# and one was a live config default (agentic_workspace_root), so it was a bug
# as well as a disclosure. /home/<svc>/ is NOT matched: appuser and claude are
# container service accounts and legitimate.
hard "absolute home paths"   '/Users/[a-z][a-z0-9._-]{2,}/' "$UNPUBLISHED|$SELF"

echo "── Audit-doc blind spot ──────────────────────────────────────────"
# gitleaks ALLOWLISTS docs/genericization + docs/archive, because those files
# describe credentials for a living and tripped every rule. That allowlist is a
# hole: paste a real secret into one and nothing catches it. These rules scan
# exactly the allowlisted paths for FULL-LENGTH secret shapes. The deliberate
# 12-char truncations already in the audit ("a2a_67U93QcH…") are well under the
# thresholds, so documenting a finding stays possible; pasting the key does not.
AUDIT_DOCS='^(docs/(genericization|archive)/|docs/prompts_log\.md|Claude_understand\.md)'
audit_secret() { # name, regex
  local name="$1" re="$2" hits
  hits=$(git grep -lIE "$re" -- . 2>/dev/null | grep -E "$AUDIT_DOCS" | grep -v hygiene-check || true)
  if [ -n "$hits" ]; then
    printf 'FAIL  %-34s full-length secret in an allowlisted doc\n' "$name"
    echo "$hits" | sed 's/^/        /'
    fail=1
  else
    printf 'ok    %-34s 0\n' "$name"
  fi
}
audit_secret "full a2a key in audit docs"   'a2a_[A-Za-z0-9_-]{30,}'
audit_secret "full gitlab PAT in audit docs" 'glpat-[A-Za-z0-9_-]{15,}'
audit_secret "full anthropic key in audit docs" 'sk-ant-[A-Za-z0-9_-]{40,}'

echo "── Publication placeholders ──────────────────────────────────────"
# A SECURITY.md that names an address nobody reads is worse than no policy at
# all: reports vanish silently and the reporter believes they were filed. Same
# for a governance table of placeholder maintainers. HARD, not a ratchet --
# these must be resolved before the repo is published, not trended downward.
hard "unfilled contact placeholder"    'OSS_CONTACT_EMAIL'        "$SELF"
hard "unfilled maintainer placeholder" 'MAINTAINER_(NAME|GITHUB)' "$SELF"

echo
echo
echo
echo
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
echo
echo
echo
echo
echo "── Suppression debt (QUAL-6) ─────────────────────────────────────"
# Each `noqa` is a silenced linter and each `type: ignore` a silenced type
# checker. Individually reasonable, collectively a place for real warnings to
# hide. Ratchets only -- no demand to clear them, just a floor that falls.
ratchet_in_path "noqa suppressions"        "backend/app" '# *noqa'            515
ratchet_in_path "type: ignore suppressions" "backend/app" '# *type: *ignore'  44

echo "── File size (QUAL-1) ────────────────────────────────────────────"
# 11 modules exceed 1500 lines; the largest is 5401. They are being split
# incrementally, so this ratchets the COUNT rather than demanding a rewrite.
# 12th: governance_orchestrator.py (2,235) — taken on knowingly with the PR-5
# retrofit; upstream ships it as one file and splitting it here would diverge
# for no functional gain. Logged as QUAL-1 debt, not absorbed silently.
bigfile_ratchet "backend files >1500 lines" 'backend/app/**.py' 1500 12

echo "── Sandbox invariant ─────────────────────────────────────────────"
# LLM-generated HTML (product-kit prototypes) is rendered via srcdoc in an
# iframe. `allow-scripts` alone is SAFE: the frame gets a null origin, so its
# JS cannot reach the parent DOM, cookies or localStorage. Combining it with
# `allow-same-origin` removes that boundary and turns a contained preview into
# stored XSS against the app origin -- a one-word edit, invisible in review.
# There are 6 such iframes; none may ever carry both.
hard "iframe allow-same-origin+scripts" 'sandbox="[^"]*allow-same-origin[^"]*allow-scripts|sandbox="[^"]*allow-scripts[^"]*allow-same-origin' "$UNPUBLISHED|$SELF"

echo "── Dependency lockfiles (SEC-3) ──────────────────────────────────"
# A lockfile's failure mode is going stale in silence: someone bumps or adds a
# pin in requirements.txt, never regenerates, and the lock keeps certifying the
# OLD closure. Offline structural check -- every `name[extras]==version` in
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
# The NPCI backend ships one lock PER ARCH (torch pulls NVIDIA CUDA wheels on
# x86_64 that do not exist in the aarch64 graph). Both must track requirements.txt
# or the arch you did not regenerate fails closed at build time.
lock_fresh "npci lock (arm64)"    backend                  requirements.arm64.lock
lock_fresh "npci lock (amd64)"     backend                  requirements.amd64.lock
# The partner lock is NOT checked here. It lives in atom-partner-platform,
# a separate repository since the split; this line used to name
# partner-platform/backend, a path that has not existed in this tree since.
# It could never pass, so it reported FAIL "missing requirements.lock" on
# every run and quietly inflated the known-failure count by one. A gate that
# cannot pass teaches people to ignore the gate.

echo "── Generated docs ────────────────────────────────────────────────"
# docs/SystemPrompts.md is generated from backend/app/prompts/**. Its hand-
# maintained ancestor reproduced every prompt verbatim and rotted on the first
# prompt change -- a catalogue that disagrees with what the platform actually
# sends is worse than none, because it is quoted in review.
if out=$(python3 scripts/ci/generate-prompt-catalogue.py --check 2>&1); then
    printf 'ok    %-34s up to date\n' "prompt catalogue"
else
    printf 'FAIL  %-34s STALE — run scripts/ci/generate-prompt-catalogue.py\n' "prompt catalogue"
    printf '%s\n' "$out" | sed 's/^/        /'
    fail=1
fi

echo "── A2A mirror drift   ─────────────────────────────"
# QUAL-4: hmac_signer / protocol / executor_base are no longer hand-mirrored.
# They are VENDORED from packages/a2a-core by scripts/ci/sync-a2a-core.sh, which
# is the single editable source. This check fails if any copy drifts from it --
# covering all FOUR trees (npci, partner, cert-agent, bank-agent), where the old
# per-file check only ever compared npci vs partner.
if out=$(bash scripts/ci/sync-a2a-core.sh --check 2>&1); then
    printf 'ok    %-34s 0 stale\n' "a2a-core vendored copies"
else
    printf 'FAIL  %-34s STALE — run scripts/ci/sync-a2a-core.sh\n' "a2a-core vendored copies"
    printf '%s\n' "$out" | sed 's/^/        /'
    fail=1
fi
# client/mount legitimately differ (NPCI is the authority, the partner is a
# peer) and are NOT vendorable. Baselined so the existing gap cannot grow.
mirror_drift "a2a client mirror"       client.py      125
mirror_drift "a2a mount mirror"        mount.py       52
# cert-agent <-> bank-agent share a second, separate lineage of wire code with no
# NPCI counterpart, so it has no canonical home in a2a-core. These are already
# byte-identical within the pair; hard-gate them so they stay that way.
for f in __init__.py auth_middleware.py handshake.py jwt_tools.py nonce_store.py outbound_auth.py; do
    a="certagent/cert-agent/app/a2a_common/$f"; b="certagent/bank-agent/app/a2a_common/$f"
    if [ -f "$a" ] && [ -f "$b" ]; then
        n=$(diff "$a" "$b" 2>/dev/null | grep -c '^[<>]' || true)
        if [ "${n:-0}" -gt 0 ]; then
            printf 'FAIL  %-34s %s differing lines, expected 0\n' "cert<->bank $f" "$n"
            fail=1
        else
            printf 'ok    %-34s 0\n' "cert<->bank $f"
        fi
    fi
done

echo "── Exception hygiene ─────────────────────────────────────────────"
# A bare `except:` also swallows KeyboardInterrupt and SystemExit, so a stuck
# request cannot be interrupted and shutdown can hang. There are currently ZERO
# (confirmed by AST walk, not regex -- a regex for `except *:` matches the
# phrase "try/except:" in prose and reports false positives). HARD: never
# reintroduce one.
hard "bare except clauses"   '^[[:space:]]*except[[:space:]]*:' "$UNPUBLISHED|$SELF"
# `except Exception` is often a deliberate fail-open guard here (telemetry, LLM
# formatting). At 941 occurrences it also hides real failures, and nothing
# stopped it growing. Ratchet: may fall, never rise. Give each NEW one an inline
# reason, or narrow the exception type.
ratchet_in_path "broad except Exception" "backend/app" 'except Exception' 948

echo "── Domain coupling (08-domain-term-removal-plan.md) ───────────────"
# The finish line for genericization is not "zero in the repo" — the domain
# terms are the UPI pack's whole value and MUST survive there. It is zero in
# core/ and adapters/, which must know about no ecosystem in particular.
#
# A ratchet, not a hard rule: this is a multi-phase cleanup and blocking every
# merge until it is finished would just get the gate deleted. The count may fall
# and never rise. Lower the baselines as phases land.
#
# `git grep -E` has NO word boundaries — a pattern using \b matches nothing and
# reports a cheerful zero. That bug has now occurred three times in this work
# (audit §G4, §G12, and the first draft of the plan itself), so the control
# below asserts the pattern still matches SOMEWHERE before any zero is believed.
# BHIM added 2026-08-11: it is a TRADEMARK and had never been in this pattern,
# so 74 occurrences across 19 files went unwatched while the logo assets were
# being removed for exactly that reason.
#
# VPA / IFSC / REMITTER / BENEFICIARY added 2026-08-13, for the same reason and
# found the same way: a partner-platform audit went looking for them by name and
# they simply were not in the pattern, so they were invisible repo-wide —
# including 42 `remitter` and 48 `beneficiary` in backend/app alone. They are
# payment-domain vocabulary as surely as UPI is. Case variants are spelled out
# because this pattern is matched case-SENSITIVELY and REMITTER_BANK is a live
# enum value, not prose.
DOMAIN_TERMS='(NPCI|npci|UPI|upi_|BHIM|bhim|payment|Payment|VPA|vpa|IFSC|ifsc|REMITTER|Remitter|remitter|BENEFICIARY|Beneficiary|beneficiary)'
control=$(git grep -ohIE "$DOMAIN_TERMS" -- backend/app 2>/dev/null | wc -l | tr -d ' ')
if [ "${control:-0}" -lt 100 ]; then
  echo "FATAL: domain-term pattern matched $control times across backend/app."
  echo "       Expected hundreds. The pattern is broken, not the tree —"
  echo "       every count below would be a false zero. Refusing to pass."
  exit 2
fi

# Baselines MEASURED, not estimated — the first attempt guessed 2 for adapters/
# and the gate immediately failed at 6. The ratchet flagged
# adapters/certification/upi.py as misfiled (a module named for the ecosystem it
# serves is pack content, not a generic adapter); moving it to
# packs/network/certification.py took adapters/ from 6 to 5.
#
# The remaining 5 are PROSE — docstrings naming UPI as the worked example, which
# is accurate and worth keeping, the same allowance core/domain's docstrings get.
#
# CAVEAT worth knowing: a term count cannot see the coupling that matters most.
# adapters/channel/a2a.py imports A2ATaskType and PartnerAgent from
# models/phase_c — UPI's protocol enum and UPI's ORM model — and scores ZERO on
# this pattern. Driving these numbers down is necessary, not sufficient; the
# structural work is Phase D.
# Baselines RE-MEASURED 2026-09-03, after the domain-term neutralization pass
# removed the brand names from CODE while deliberately pinning wire values,
# table/column names, datastore names, addresses and legal text .
#
# What is LEFT in core/ is mostly not renameable: copyright headers ("National
# Payments Corporation of India" matches `Payment`), the `npci_kg` / `atom_user`
# datastore names, a `REMITTER` certgroup VALUE, and the VPA regex in
# pii_redaction.py — which has to name the thing it redacts.
#
# RE-MEASURED AGAIN 2026-09-03 after merging the neutralization into
# api-certification-flow-new. That branch carries substantially more code than
# the neutralization branch did (the certification flow, sim packs, the config
# pack system), so the counts sit above the standalone branch's numbers while
# still being a large improvement on what api-certification-flow-new had:
#
#   path          api-cert (pre-merge)   merged   standalone neutralization
#   core/                          119       80                          52
#   adapters/                       10        6                           4
#   a2a_common/                    154       69                          41
#   frontend/                      292      182                         177
#
# The baselines below are the MERGED column — measured on this tree, not
# inherited. Raising a ratchet needs this kind of receipt; lowering it does not.
#
# Raised by +4 / +2 / +5 when cert-integration-testing merged in. Every one of
# those 11 hits is the word "Payments" in the licence header that 354bce7 added
# — `# Copyright 2026 National Payments Corporation of India` — on line 1 of
# files that previously carried no header:
#
#   core/      +3 core/wire/{__init__,codec,registry}.py, +1 domain/config_pack.py
#   adapters/  +2 adapters/wire/{__init__,xml_codec}.py
#   a2a_common/ +5 the integration-tunnel and vendored wire modules
#
# Verified by enumerating the hits: all are line 1, all are the copyright line.
# No code in those files gained a domain term. This is the same call as the wiki
# baseline below — the header text is a legal fact, not vocabulary coupling, and
# raising the number keeps the gate tight where loosening DOMAIN_TERMS to exclude
# `Payment` would blind it to the real thing everywhere else.
ratchet_in_path "domain terms in core/"     "backend/app/core"        "$DOMAIN_TERMS" 84
ratchet_in_path "domain terms in adapters/" "backend/app/adapters"    "$DOMAIN_TERMS" 8
ratchet_in_path "domain terms in a2a_common/" "backend/app/a2a_common" "$DOMAIN_TERMS" 74
ratchet_in_path "domain terms in frontend/"  "frontend/src"           "$DOMAIN_TERMS" 182
# 0 because the partner trees are ABSENT from this repository, not because they
# were cleaned. If one returns, the gate fails on its first hit and forces a
# deliberate re-baseline instead of silently inheriting a stale number.
ratchet_in_path "domain terms in partner-frontend/" "partner-platform/frontend/src" "$DOMAIN_TERMS" 0

# partner-platform/backend had NEVER been gated: the single largest concentration
# in the tree sat outside every ratchet while five smaller paths were watched.
# Its SCHEMA is already clean — 20 table names, zero domain terms — so the debt
# is in api/, a2a_common/ and the agents, not the data model.
ratchet_in_path "domain terms in partner-backend/" "partner-platform/backend" "$DOMAIN_TERMS" 0

# READMEs are the front door — the first thing a reader of a published repo sees,
# and the last place a stale domain name should survive. Baseline set after the
# P1 prose pass. The residue is deliberate and cannot go to zero by rewording:
# real paths (atom-frontend, com.example.*), contract enum values (PAYER_PSP,
# REMITTER_BANK), and decompiled third-party package names in precert-bank-sim.
# Renaming any of those in prose makes the document false, not generic.
ratchet_in_path "domain terms in READMEs"   "*README.md"              "$DOMAIN_TERMS" 23

# The wiki's HAND-WRITTEN pages. Baseline THREE, and all three are the same
# thing: the canonical repository URL https://github.com/npci/atom-partner-platform
# in the three pages that link to the partner platform. Those landed in 9d10475
# ("point canonical URLs at the npci organisation") and the baseline was not
# raised with them, so this gate has been failing since.
#
# An org name inside a repository URL is not the coupling this ratchet exists to
# catch — it is an address, and it has to be that address. Raising the baseline
# rather than loosening DOMAIN_TERMS keeps the gate tight: a fourth hit still
# fails, and a genuine vocabulary leak in prose is still caught.
#
# The `:(glob)` prefix is load-bearing. Without it git's pathspec `*` also
# matches `/`, so `wiki/*.md` would sweep in wiki/reference/ — which is
# GENERATED from the code and legitimately carries 17 domain terms (agent names,
# table names, API paths). Gating generated output would duplicate the gates
# already on the code it is derived from, and would fail the moment someone adds
# a legitimately-named agent.
ratchet_in_path "domain terms in wiki/"     ":(glob)wiki/*.md"        "$DOMAIN_TERMS" 3

# NOT ratcheted, deliberately: docs/ at large is 1421 hits (1793 counting the
# genericization plans, which quote the terms precisely because they are about
# removing them). Much of the rest is operational runbooks — precert,
# certification — that legitimately describe the domain they operate on, and a
# no-increase rule would block writing them. Gating that is its own phase, with
# its own scoping decision; it is not a line to be bolted on here.

echo
echo "── Documentation links ───────────────────────────────────────────"
# HARD rule: zero broken relative links. Two had already been found by hand —
# docs/README.md pointing at ./partner-platform/ from inside docs/, and
# Claude_understand.md pointing at a design doc deleted in 647b246. Both were
# caught only because someone happened to look at that exact line.
bash "$(dirname "$0")/check-links.sh" || fail=1

echo
if [ "$fail" -ne 0 ]; then
  echo "HYGIENE CHECK FAILED — see docs/genericization/01-exposure-audit.md"
  exit 1
fi
echo "hygiene check passed"
