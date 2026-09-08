#!/usr/bin/env bash
#
# Guard the assumptions the SBOM exclusions rest on.
#
# ============================================================================
# WHY THIS SCRIPT EXISTS
# ============================================================================
# Six findings from the 2026-08-27 A2A compliance report were closed by
# excluding devDependencies from the SBOM (js-yaml, minimatch, zod, doctrine,
# prop-types, json-buffer). That exclusion is CORRECT — none of them ships —
# but it is a CLAIM ABOUT THE DEPENDENCY GRAPH, and claims rot.
#
# The failure mode is specific and realistic: someone imports `js-yaml` in a
# Vite config that gets bundled, or moves a package from devDependencies to
# dependencies to fix a build error. The moment that happens, a package with a
# CVSS 7.5 advisory is shipping to users AND our SBOM is configured to not
# mention it. That is strictly worse than the original finding, because now
# nobody is looking.
#
# So the exclusion is paired with this gate. An annotation backed by an
# enforced invariant survives staff turnover; one backed by a memory does not.
#
# Also checks the two Python-side assumptions:
#   - every pin in requirements.txt is present in both lockfiles (this is the
#     `hygiene-check.sh` lock-freshness check that .github/workflows/
#     secret-scan.yml already invokes but which does not exist in the tree),
#   - torch / sentence-transformers have not crept back into the backend.
#
# Exit non-zero on any violation. Intended to run in CI on every PR.
#
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FAILED=0

fail() { echo "FAIL: $*" >&2; FAILED=1; }
pass() { echo "ok:   $*"; }

echo "=== SBOM scope guard ==="
echo

# ---------------------------------------------------------------------------
# 1. The six excluded npm packages must stay OUT of the production tree.
# ---------------------------------------------------------------------------
echo "--- 1. Excluded npm packages must remain dev-only ---"
EXCLUDED_NPM=(js-yaml minimatch zod doctrine prop-types json-buffer)

if [[ -d "${REPO_ROOT}/frontend/node_modules" ]]; then
  for pkg in "${EXCLUDED_NPM[@]}"; do
    # `npm ls --omit=dev <pkg>` prints "(empty)" when the package is not in the
    # production tree. Any real path means it now ships.
    out="$( cd "${REPO_ROOT}/frontend" && npm ls "${pkg}" --omit=dev 2>/dev/null )"
    if echo "${out}" | grep -q "(empty)"; then
      pass "${pkg} is not a production dependency"
    else
      fail "${pkg} HAS BECOME A PRODUCTION DEPENDENCY. It is excluded from the
      SBOM as dev-only, so it would now ship UNSCANNED. Either revert that
      change, or remove ${pkg} from EXCLUDED_NPM here and from the SBOM
      exclusions, and re-triage its advisories. Tree:
${out}"
    fi
  done
else
  echo "skip: frontend/node_modules absent (run npm ci first)"
fi
echo

# ---------------------------------------------------------------------------
# 2. The built bundle must not contain them either.
#
# Belt and braces over check 1: a package can reach the bundle without being a
# declared production dependency (e.g. imported directly from a config file
# that Vite inlines). This checks the actual shipped JavaScript.
# ---------------------------------------------------------------------------
echo "--- 2. Built bundle must not contain excluded packages ---"
if compgen -G "${REPO_ROOT}/frontend/dist/assets/*.js" >/dev/null 2>&1; then
  for pkg in "${EXCLUDED_NPM[@]}"; do
    if grep -qs "${pkg}" "${REPO_ROOT}"/frontend/dist/assets/*.js; then
      fail "${pkg} appears in the built bundle (frontend/dist/assets/*.js)
      despite being excluded from the SBOM as dev-only."
    else
      pass "${pkg} absent from built bundle"
    fi
  done
else
  echo "skip: no build output in frontend/dist/assets (run npm run build first)"
fi
echo

# ---------------------------------------------------------------------------
# 3. torch / sentence-transformers must NOT return to the backend.
#
# They were moved to services/reranker/ to clear six findings. Re-pinning them
# in the backend re-imports all six into the component that holds the
# platform's data.
# ---------------------------------------------------------------------------
echo "--- 3. Model stack must stay out of the backend ---"
for pkg in torch sentence-transformers transformers; do
  # Match a real pin at line start, not the prose comments explaining the
  # removal (which necessarily mention these names).
  if grep -Eq "^${pkg}(\[|==|>=|~=)" "${REPO_ROOT}/backend/requirements.txt"; then
    fail "${pkg} is pinned in backend/requirements.txt again. This re-imports
      the advisories the 2026-08-28 split removed. Put it in
      services/reranker/requirements.txt instead — and if you truly need the
      in-process backend, move the --extra-index-url line with it or pip will
      pull ~3 GB of CUDA wheels."
  else
    pass "${pkg} not pinned in backend/requirements.txt"
  fi
done
for lock in amd64 arm64; do
  f="${REPO_ROOT}/backend/requirements.${lock}.lock"
  [[ -f "${f}" ]] || continue
  if grep -Eq "^torch==" "${f}"; then
    fail "torch is present in requirements.${lock}.lock — regenerate it from
      the updated requirements.txt (command is in the lock's own header)."
  else
    pass "torch absent from requirements.${lock}.lock"
  fi
done
echo

# ---------------------------------------------------------------------------
# 4. Removed packages must be truly gone.
# ---------------------------------------------------------------------------
echo "--- 4. Removed packages must stay removed ---"
for pkg in qrcode async-timeout playwright; do
  if grep -Eq "^${pkg}(\[|==|>=|~=)" "${REPO_ROOT}/backend/requirements.txt"; then
    fail "${pkg} is pinned in backend/requirements.txt again — it was removed
      to clear an SBOM finding. See the comment at its former location."
  else
    pass "${pkg} not pinned in backend/requirements.txt"
  fi
done

# qrcode must not be imported anywhere either — the segno swap is only complete
# if no call site still reaches for the old library.
if grep -rniqs --include=*.py "^\s*import qrcode\|^\s*from qrcode" "${REPO_ROOT}/backend/app"; then
  fail "backend/app still imports 'qrcode'. It was replaced by 'segno'
      (SBOM finding 1 — banned-licence classifier). See app/core/mfa.py."
else
  pass "no 'qrcode' imports remain in backend/app"
fi

# 4b. …and they must be gone from the LOCKS, which is what actually ships.
#
# WHY THIS IS SEPARATE FROM CHECK 5: check 5 catches pins that are in
# requirements.txt but MISSING from the lock (a fix that never reaches the
# image). This catches the opposite and more dangerous direction — packages
# REMOVED from requirements.txt that are still IN the lock. The Dockerfile
# installs from the lock and explicitly notes that "editing requirements.txt
# alone now has NO effect on the image", so a deletion that stops at
# requirements.txt is documentation, not remediation: the vulnerable package
# keeps shipping while the source of truth claims it is gone. That gap is how
# ecdsa (CVE-2024-23342, no upstream fix) survived a documented removal.
for pkg in qrcode async-timeout playwright ecdsa python-jose paramiko pynacl rsa; do
  norm="$(echo "${pkg}" | tr 'A-Z' 'a-z' | sed -E 's/[-_.]+/-/g')"
  for lock in amd64 arm64; do
    f="${REPO_ROOT}/backend/requirements.${lock}.lock"
    [[ -f "${f}" ]] || continue
    if grep -Eqi "^${norm}(\[|==)" "${f}"; then
      fail "${pkg} is STILL IN requirements.${lock}.lock although it is not in
      requirements.txt. Installs come from the lock (both the container build
      and the host virtualenv), so this package is still being deployed.
      Regenerate: scripts/ci/regenerate-locks.sh"
    else
      pass "${pkg} absent from requirements.${lock}.lock"
    fi
  done
done
echo

# ---------------------------------------------------------------------------
# 5. Lock freshness — every pin in requirements.txt must be in both locks.
#
# This is the check .github/workflows/secret-scan.yml already calls
# `hygiene-check.sh` for. That script is MISSING from the tree, so the step is
# currently failing (or silently passing, depending on runner shell). The
# lock-freshness half of its job is implemented here because the SBOM now
# reads the lock: a pin that is in requirements.txt but not in the lock is
# invisible to the scan, which is precisely how async-timeout was reported
# against an image that never contained it.
# ---------------------------------------------------------------------------
echo "--- 5. Lock freshness (requirements.txt pins present in locks) ---"
REQ="${REPO_ROOT}/backend/requirements.txt"
for lock in amd64 arm64; do
  f="${REPO_ROOT}/backend/requirements.${lock}.lock"
  [[ -f "${f}" ]] || { echo "skip: requirements.${lock}.lock not found"; continue; }
  missing=""
  # Extract bare package names from `name==version` lines, ignoring comments,
  # pip flags (--extra-index-url) and extras markers.
  #
  # Names are normalised per PEP 503 (lowercase, runs of [-_.] collapsed to a
  # single '-') because pip-compile writes the normalised form into the lock
  # while requirements.txt uses the human spelling. Without this, `PyJWT`
  # reports as missing even though the lock contains `pyjwt` — a false
  # positive that would train people to ignore this gate.
  while IFS= read -r pin; do
    name="$(echo "${pin}" | sed -E 's/\[.*\]//' | cut -d= -f1 \
            | tr 'A-Z' 'a-z' | sed -E 's/[-_.]+/-/g')"
    [[ -z "${name}" ]] && continue
    if ! grep -Eqi "^${name}(\[|==)" "${f}"; then
      missing="${missing} ${name}"
    fi
  done < <(grep -E '^[A-Za-z0-9_.-]+(\[[A-Za-z0-9,_-]+\])?==' "${REQ}")

  if [[ -n "${missing}" ]]; then
    fail "requirements.${lock}.lock is STALE — missing pins:${missing}
      Regenerate: scripts/ci/regenerate-locks.sh
      Installs come from the lock — the Dockerfile for containers, and
      --require-hashes into the venv for a host deployment — so an
      un-regenerated lock means these pins never reach the running
      environment and the SBOM will disagree with reality."
  else
    pass "requirements.${lock}.lock covers every requirements.txt pin"
  fi
done
echo

# ---------------------------------------------------------------------------
# 6. Raw SQL hygiene — backs the SQLAlchemy VEX statement.
#
# The VEX for sonatype-2021-0025 asserts "all dynamic SQL binds data via
# parameters and builds identifiers from an allowlist". That advisory has no
# fix version, so the annotation is the ONLY way to clear it — which means the
# annotation must stay defensible as the code changes. This is the mechanical
# check that keeps it so.
#
# HOW IT WORKS: f-string interpolation into text() is not automatically wrong —
# it is legitimate for IDENTIFIERS (table names, tsearch config names), which
# cannot be bound as parameters by the DB driver. It is only wrong for DATA.
# A regex cannot tell those apart, so this gate works off an AUDITED ALLOWLIST:
# the three sites below were each read line-by-line during the 2026-08-28 SBOM
# pass and confirmed to interpolate only allowlisted identifiers while binding
# every piece of data. Any NEW site fails the gate and must be audited (and
# added here) before it can merge.
#
# Audited sites and why each is safe:
#   api/code_indexing.py    - interpolates `tbl` from _REPO_CHILD_TABLES, a
#                             hardcoded module-level tuple of 9 literals. The
#                             repo_id is bound (:rid).
#   rag/bm25_search.py      - interpolates `cfg` from _tsv_config_name(), which
#                             returns only 'english'/'simple'/'simple_code' and
#                             falls back to 'english' on anything else. The
#                             user's query is bound (:q).
#   rag/hybrid_search.py    - interpolates EMBEDDING_DIM (an int constant) and
#                             `cat_filter`, itself built only from generated
#                             `:catN` placeholder names. Category VALUES are
#                             bound, never inlined.
# ---------------------------------------------------------------------------
echo "--- 6. No NEW interpolated text() sites (backs the SQLAlchemy VEX) ---"
AUDITED_SQL_SITES=(
  "backend/app/api/code_indexing.py"
  "backend/app/rag/bm25_search.py"
  "backend/app/rag/hybrid_search.py"
)
unaudited=""
while IFS= read -r hit; do
  [[ -z "${hit}" ]] && continue
  file="${hit%%:*}"
  # Normalise to a repo-relative path with forward slashes so the comparison
  # works identically on Windows (Git Bash) and Linux runners.
  rel="${file#"${REPO_ROOT}/"}"
  rel="${rel//\\//}"
  known=false
  for site in "${AUDITED_SQL_SITES[@]}"; do
    [[ "${rel}" == *"${site}"* ]] && known=true && break
  done
  [[ "${known}" == false ]] && unaudited="${unaudited}
${hit}"
done < <(grep -rn --include=*.py -E 'text\(\s*f["'"'"']' "${REPO_ROOT}/backend/app" 2>/dev/null || true)

if [[ -n "${unaudited}" ]]; then
  fail "NEW f-string interpolation into text() at an unaudited site. This
      breaks the VEX statement for sonatype-2021-0025 (SQLAlchemy), which
      asserts every dynamic query binds its DATA and allowlists its
      IDENTIFIERS. Bind data with :params. If this site genuinely interpolates
      only an allowlisted identifier, audit it and add it to
      AUDITED_SQL_SITES in this script with a note on why it is safe:${unaudited}"
else
  pass "no new interpolated text() sites (${#AUDITED_SQL_SITES[@]} audited sites unchanged)"
fi
echo

if [[ "${FAILED}" -ne 0 ]]; then
  echo "=== SBOM scope guard FAILED ===" >&2
  exit 1
fi
echo "=== SBOM scope guard passed ==="
