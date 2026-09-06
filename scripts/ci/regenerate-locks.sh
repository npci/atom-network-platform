#!/usr/bin/env bash
#
# Regenerate backend/requirements.<arch>.lock from backend/requirements.txt.
#
# WHY THIS SCRIPT EXISTS
# ---------------------------------------------------------------------------
# The regeneration command used to live only in a comment at the top of each
# lockfile. That is how the locks drifted: `requirements.txt` recorded that
# python-jose, ecdsa, paramiko, pynacl and rsa had been REMOVED, but nobody ran
# the command, so the removals never reached anything that runs. ecdsa's
# CVE-2024-23342 has no fixed version — removal is the only remedy — and it
# stayed in the installed closure for months after being "removed".
#
# A command in a comment is a suggestion. A script is a thing CI can run and a
# thing a human can run without reading 70 lines of header first.
#
# HOW IT RUNS: NATIVE FIRST, CONTAINER ONLY IF ASKED
# ---------------------------------------------------------------------------
# The server applications are deployed ON THE HOST, in a virtualenv, not in a
# container. So the lock that matters is the one resolved by the SAME
# interpreter that will install it, and pip-compile is run natively by default.
# Docker is an OPTIONAL mode (--docker) for cross-compiling the other
# architecture's lock, which cannot be done natively.
#
# This distinction is not cosmetic. pip-compile resolves against the running
# interpreter's version and platform: environment markers
# (python_version < "3.12", sys_platform == "linux") are evaluated at compile
# time, and --generate-hashes records the hashes of the wheels chosen for THAT
# platform. Compiling in a Linux container and installing on a Windows host can
# therefore produce a lock whose hashes do not match any wheel pip will
# download, and every install fails closed under --require-hashes.
#
# Hence: --native (default) writes the lock for the host you are on;
# --docker <arch> writes the lock for a different target. Use whichever matches
# where the code actually runs.
#
# WHAT IT GUARANTEES
#   1. PRESERVES THE HANDWRITTEN HEADER. pip-compile --no-header strips its own
#      banner, but it would also overwrite ours. Those ~75 lines carry real
#      institutional knowledge (why the arch split exists, why --allow-unsafe
#      is mandatory, what the CUDA removal was for). Restored verbatim, with a
#      dated REGENERATED stamp appended.
#   2. --generate-hashes and --allow-unsafe, because installs use
#      --require-hashes: without --allow-unsafe setuptools stays unpinned and
#      pip REJECTS THE WHOLE FILE.
#   3. Compiles to a temporary file first, so a failed resolve cannot leave a
#      half-written lock behind that then breaks every install.
#   4. Verifies the result with check-sbom-scope.sh, so you find out
#      immediately whether the regeneration actually closed the findings.
#
# USAGE
#   scripts/ci/regenerate-locks.sh                    # native, for this host
#   scripts/ci/regenerate-locks.sh --native amd64     # native, name it amd64
#   scripts/ci/regenerate-locks.sh --docker           # both arches via Docker
#   scripts/ci/regenerate-locks.sh --docker arm64     # one arch via Docker
#   scripts/ci/regenerate-locks.sh --check            # verify only, no changes
#
# REQUIREMENTS
#   Native mode: python3 + network access to your package index. pip-tools is
#     installed into a throwaway venv so it never pollutes the app virtualenv.
#   Docker mode: docker, plus QEMU/binfmt for a non-native architecture
#     (docker/setup-qemu-action@v3 on a runner).
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND="${REPO_ROOT}/backend"
REQ="${BACKEND}/requirements.txt"

PIP_TOOLS_VERSION="7.4.1"
PYTHON_IMAGE="python:3.12-slim"

MODE="native"
CHECK_ONLY=0
ARCHES=()

for arg in "$@"; do
  case "${arg}" in
    --check)      CHECK_ONLY=1 ;;
    --native)     MODE="native" ;;
    --docker)     MODE="docker" ;;
    amd64|arm64)  ARCHES+=("${arg}") ;;
    -h|--help)    sed -n '1,62p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *)            echo "unknown argument: ${arg}" >&2; exit 2 ;;
  esac
done

die()  { echo "ERROR: $*" >&2; exit 1; }
info() { echo "==> $*"; }

[[ -f "${REQ}" ]] || die "not found: ${REQ}"

# ---------------------------------------------------------------------------
# --check: report drift without touching anything.
#
# Deliberately reuses check-sbom-scope.sh rather than reimplementing the
# comparison. Two sources of truth for "is the lock fresh" would eventually
# disagree, and the guard already checks both directions (a pin missing from
# the lock, and a package lingering in the lock after removal).
# ---------------------------------------------------------------------------
if [[ ${CHECK_ONLY} -eq 1 ]]; then
  info "checking lock freshness (no changes will be made)"
  exec bash "${REPO_ROOT}/scripts/ci/check-sbom-scope.sh"
fi

# ---------------------------------------------------------------------------
# host_arch — map uname to the lock naming scheme (amd64 / arm64).
#
# Used only to pick a DEFAULT filename in native mode. It describes the machine
# you are compiling on, which in native mode is also the machine that will
# install the result.
# ---------------------------------------------------------------------------
host_arch() {
  case "$(uname -m)" in
    x86_64|amd64)   echo "amd64" ;;
    aarch64|arm64)  echo "arm64" ;;
    *)              echo "unknown" ;;
  esac
}

# ---------------------------------------------------------------------------
# assert_no_credentials <file>
#
# A lockfile is committed to git, so anything pip-compile writes into it is
# published to everyone with repo access — permanently, since git history keeps
# it even after a later "fix" commit.
#
# This matters because private indexes are usually authenticated via a URL of
# the form https://user:password@host/repository/.../simple, and pip-compile's
# DEFAULT behaviour is to record the index it resolved against as an
# `--index-url` line at the top of its output. Passing the mirror via
# PIP_INDEX_URL does not avoid this: pip-tools reads the environment variable
# and emits it just the same. Verified directly against this repo's mirror —
# the password appeared verbatim on line 1 of the generated file.
#
# `--no-emit-index-url` is the real fix and is passed at both call sites. This
# function is the backstop for the day someone removes that flag, upgrades
# pip-tools past a behaviour change, or adds a third call site. It inspects the
# generated body BEFORE it is assembled into the committed lock, so a hit means
# nothing was ever written to a tracked file.
#
# Deliberately matches the `user:pass@` SHAPE rather than any specific hostname
# or secret, so it keeps working for a different mirror or a rotated password.
# ---------------------------------------------------------------------------
assert_no_credentials() {
  local f="$1"
  if grep -qE '://[^/[:space:]]+:[^/[:space:]]+@' "${f}"; then
    rm -f "${f}"
    die "REFUSING TO WRITE: pip-compile emitted a URL containing credentials.
      The generated file was discarded and the committed lock is untouched.
      Nothing was written to a tracked file, so there is nothing to scrub.

      Cause: --no-emit-index-url was not honoured (flag removed, or a
      pip-tools behaviour change). Restore it before regenerating.

      If a credentialed URL ever DID reach a commit, rotating the password is
      the only real remedy — rewriting history does not un-publish a secret."
  fi
}

# ---------------------------------------------------------------------------
# extract_header <lockfile>
#
# Everything up to (not including) the first line that is neither a comment nor
# blank. Verified against both locks: the handwritten header is one contiguous
# leading comment block ending at line 73 (amd64) / 77 (arm64), immediately
# before the `--extra-index-url` line. Emitting nothing when the file is absent
# is intentional — a brand-new lock simply gets no header.
# ---------------------------------------------------------------------------
extract_header() {
  local f="$1"
  [[ -f "${f}" ]] || return 0
  awk '
    /^#/             { print; next }
    /^[[:space:]]*$/ { print; next }
    { exit }
  ' "${f}"
}

# ---------------------------------------------------------------------------
# assemble <lockfile> <header-file> <body-file> <provenance-line>
#
# Header verbatim + dated stamp + freshly compiled body. The stamp is APPENDED
# rather than edited into the existing prose: the header is hand-written
# reasoning, and rewriting arbitrary lines inside it is how you lose the
# knowledge it exists to carry.
# ---------------------------------------------------------------------------
assemble() {
  local lock="$1" header_file="$2" body_file="$3" provenance="$4"

  # Normalise the source path in pip-compile's `# via -r <path>` annotations.
  #
  # pip-compile echoes the path it was GIVEN, so the annotation records where
  # the file happened to live on the machine that ran it: `/w/requirements.txt`
  # from the old Docker mode, `D:/code/.../backend/requirements.txt` from a
  # Windows checkout, `/home/someone/...` from a Linux one. All describe the
  # same file.
  #
  # Left alone, this makes the lock machine-specific: two people regenerating
  # from identical inputs produce different bytes, `--check` reports a spurious
  # diff, and a developer's local directory layout gets published in git.
  #
  # Rewriting to the repo-relative `requirements.txt` makes the output depend
  # only on the inputs. Only the annotation is touched — it is a comment that
  # pip ignores — so hashes and pins are untouched.
  #
  # pip-compile emits the source path in TWO layouts, and both must be handled:
  #
  #   single consumer     # via -r /path/requirements.txt
  #   multiple consumers  # via
  #                       #   -r /path/requirements.txt
  #                       #   some-other-package
  #
  # Missing the second form leaves absolute paths in the file, which is exactly
  # the bug this normalisation exists to prevent. The alternation below matches
  # `# via -r ` and `#   -r ` alike; the trailing `[/\\]` means a path that is
  # ALREADY bare is left as-is rather than mangled, so the pass is idempotent.
  local via_re='^([[:space:]]*#([[:space:]]+via)?[[:space:]]+-r[[:space:]]+).*[/\\](requirements\.txt)$'
  {
    cat "${header_file}"
    echo "# REGENERATED $(date -u +%Y-%m-%d) by scripts/ci/regenerate-locks.sh"
    echo "# ${provenance}"
    echo "#"
    sed -E "s|${via_re}|\1\3|" "${body_file}"
  } > "${lock}"

  local pins
  pins="$(grep -cE '^[A-Za-z0-9_.-]+(\[[A-Za-z0-9,_-]+\])?==' "${lock}" || true)"
  info "  wrote $(basename "${lock}") (${pins} pinned packages)"
}

# ---------------------------------------------------------------------------
# assert_platform_sane <lockfile>
#
# pip-compile resolves for the platform it RUNS on, and by default writes the
# result with NO environment markers. Both lockfiles target linux, so compiling
# them natively on Windows or macOS produces a file that is quietly wrong in
# both directions:
#
#   - it ADDS host-only packages (pywin32, colorama) that cannot install on
#     linux, and
#   - it DROPS linux-only packages (uvloop) that production needs.
#
# Neither failure is visible in a diff review — they look like ordinary
# dependency churn — and the first symptom is a broken deploy. Worse, the
# resulting lock UNDERSTATES the real closure, so the SBOM built from it is
# also wrong, which defeats the purpose of regenerating.
#
# There is no flag that fixes this: markers would have to come from resolving
# on the target platform. So the honest move is to refuse. The lock on disk is
# restored by the caller, leaving the committed file untouched.
# ---------------------------------------------------------------------------
assert_platform_sane() {
  local lock="$1"
  local host_only found=""
  for host_only in pywin32 pypiwin32 colorama; do
    grep -qiE "^${host_only}==" "${lock}" && found="${found} ${host_only}"
  done

  if [[ -n "${found}" ]]; then
    die "PLATFORM MISMATCH: the generated lock contains host-only package(s):${found}

      Both locks target LINUX, but this run resolved on $(uname -s). pip-compile
      resolves for the platform it runs on and emits no environment markers, so
      the result adds Windows/macOS-only packages and silently DROPS linux-only
      ones (uvloop, via uvicorn[standard]). It would fail to install in
      production and would produce a misleading SBOM.

      The lock has NOT been modified. Regenerate on linux instead:
        - a linux build agent or WSL, or
        - scripts/ci/regenerate-locks.sh --docker ${arch:-amd64}"
  fi
}

# ---------------------------------------------------------------------------
# NATIVE MODE — the default, and the one that matches host deployment.
#
# pip-tools goes into a THROWAWAY venv rather than the application virtualenv.
# Installing build tooling into the venv that serves traffic would add packages
# to the very closure this script exists to keep honest — pip-tools and its
# dependencies would show up in the next `pip freeze` and in the SBOM.
# ---------------------------------------------------------------------------
regenerate_native() {
  local arch="${1:-$(host_arch)}"
  local lock="${BACKEND}/requirements.${arch}.lock"

  [[ "${arch}" == "unknown" ]] && die "could not map $(uname -m) to amd64/arm64.
      Pass the name explicitly, e.g. --native amd64."

  # Find a REAL interpreter, not just something named python.
  #
  # Two traps here. On Windows, `python` is often a Microsoft Store "app
  # execution alias" that exists on PATH, prints an advert and exits non-zero —
  # `command -v python` finds it and everything downstream then fails
  # confusingly. And the project's own virtualenv is a perfectly good
  # interpreter that is already the right version. So: probe candidates by
  # actually RUNNING each one, and prefer the app venv when it is present,
  # since in a host deployment that is precisely the interpreter that will
  # install this lock.
  local py=""
  local candidate
  for candidate in \
      "${BACKEND}/venv/bin/python" \
      "${BACKEND}/venv/Scripts/python.exe" \
      python3 python; do
    if command -v "${candidate}" >/dev/null 2>&1 \
       && "${candidate}" -c 'import sys; sys.exit(0)' >/dev/null 2>&1; then
      py="${candidate}"
      break
    fi
  done
  [[ -n "${py}" ]] || die "no working Python interpreter found.
      Tried the project venv (backend/venv), then python3, then python.
      On Windows, a bare 'python' may be the Microsoft Store alias, which is
      not a real interpreter — install Python or activate the project venv."

  local pyver pyplat
  pyver="$("${py}" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
  pyplat="$("${py}" -c 'import platform; print(platform.system(), platform.machine())')"

  info "regenerating requirements.${arch}.lock natively"
  info "  interpreter: python ${pyver} on ${pyplat}"

  # A lock is only valid for the interpreter minor version that resolved it,
  # because environment markers are evaluated at compile time. The Dockerfile
  # and the app venv are both 3.12; warn loudly on a mismatch rather than
  # silently producing a lock that excludes or includes the wrong packages.
  case "${pyver}" in
    3.12.*) ;;
    *) echo "WARNING: compiling with python ${pyver}, but this project targets" >&2
       echo "         3.12. Markers like python_version < '3.12' resolve" >&2
       echo "         differently and the lock may not match the runtime." >&2 ;;
  esac

  local venv_dir header_file body_file
  venv_dir="$(mktemp -d)"
  header_file="$(mktemp)"
  body_file="$(mktemp)"
  # shellcheck disable=SC2064
  trap "rm -rf '${venv_dir}' '${header_file}' '${body_file}'" RETURN

  # Paths handed to a NATIVE Windows python must be Windows paths. Under Git
  # Bash / MSYS, mktemp -d returns something like /tmp/tmp.XXXX and the repo
  # itself is reached as /d/code/..., neither of which a native python can
  # resolve. The two failure modes differ and both are confusing:
  #
  #   - `python -m venv /tmp/x` "succeeds" but creates nothing useful, and the
  #     error only surfaces later as a missing interpreter;
  #   - pip-compile rejects `/d/code/.../requirements.txt` outright with
  #     "Path does not exist" even though the file is plainly there.
  #
  # So EVERY path crossing the bash -> native-python boundary is converted, not
  # just the venv. The shell keeps using the MSYS paths for its own redirects
  # and cleanup; only the arguments handed to python are translated. On
  # Linux/macOS cygpath is absent and all of this is a no-op.
  local venv_arg="${venv_dir}"
  local req_arg="${REQ}"
  local body_arg="${body_file}"
  if command -v cygpath >/dev/null 2>&1; then
    venv_arg="$(cygpath -w "${venv_dir}" | tr '\\' '/')"
    req_arg="$(cygpath -w "${REQ}" | tr '\\' '/')"
    body_arg="$(cygpath -w "${body_file}" | tr '\\' '/')"
  fi

  info "  installing pip-tools==${PIP_TOOLS_VERSION} into a throwaway venv"
  "${py}" -m venv "${venv_arg}" >/dev/null 2>&1 \
    || die "could not create a virtualenv with ${py}."

  # Resolve the interpreter inside the new venv. Check both layouts because a
  # POSIX venv uses bin/ and a Windows one uses Scripts/, and fail with a clear
  # message rather than letting a non-existent path fail obscurely downstream.
  local vpy=""
  local p
  for p in "${venv_dir}/bin/python" "${venv_dir}/bin/python3" \
           "${venv_dir}/Scripts/python.exe"; do
    [[ -x "${p}" ]] && { vpy="${p}"; break; }
  done
  [[ -n "${vpy}" ]] || die "created a virtualenv at ${venv_dir} but found no
      interpreter inside it (looked in bin/ and Scripts/)."

  "${vpy}" -m pip install --quiet --disable-pip-version-check \
      "pip-tools==${PIP_TOOLS_VERSION}" \
    || die "could not install pip-tools.
      Native mode needs access to your package index. If a proxy blocks pip
      (some return 403 to pip's user-agent while allowing browsers), either
      configure pip's index/cert for it, or use an internal mirror:
        PIP_INDEX_URL=https://<mirror>/simple scripts/ci/regenerate-locks.sh"

  extract_header "${lock}" > "${header_file}"
  info "  preserved $(wc -l < "${header_file}" | tr -d ' ') header lines"

  "${vpy}" -m piptools compile \
      --generate-hashes --no-strip-extras --allow-unsafe \
      --no-emit-index-url \
      --no-header --quiet \
      --output-file "${body_arg}" "${req_arg}" \
    || die "pip-compile failed. The existing lock is untouched."

  [[ -s "${body_file}" ]] || die "pip-compile produced an empty file."
  assert_no_credentials "${body_file}"

  # Check the BODY before assembling. The generated content has not touched the
  # committed lock at this point, so a refusal here leaves the tracked file
  # exactly as it was and there is nothing to roll back.
  assert_platform_sane "${body_file}"

  assemble "${lock}" "${header_file}" "${body_file}" \
    "Compiled natively with python ${pyver} on ${pyplat}."
}

# ---------------------------------------------------------------------------
# DOCKER MODE — for producing the OTHER architecture's lock.
#
# Kept because the repository ships two locks and a host can only natively
# resolve its own. Not the default: the servers run on the host, so the
# container's interpreter is not the one that installs the result.
# ---------------------------------------------------------------------------
regenerate_docker() {
  local arch="$1"
  local lock="${BACKEND}/requirements.${arch}.lock"
  local platform="linux/${arch}"

  command -v docker >/dev/null 2>&1 || die "docker not found, and --docker was requested.
      For a host deployment you probably want the default native mode:
        scripts/ci/regenerate-locks.sh"
  docker info >/dev/null 2>&1 || die "docker is installed but the daemon is unreachable."

  info "regenerating requirements.${arch}.lock in ${PYTHON_IMAGE} (${platform})"

  local header_file body_file
  header_file="$(mktemp)"
  body_file="$(mktemp)"
  # shellcheck disable=SC2064
  trap "rm -f '${header_file}' '${body_file}'" RETURN

  extract_header "${lock}" > "${header_file}"
  info "  preserved $(wc -l < "${header_file}" | tr -d ' ') header lines"

  local tmp_name="requirements.${arch}.lock.new"
  docker run --rm --platform "${platform}" --user root \
    -v "${BACKEND}:/w" -w /w --entrypoint sh "${PYTHON_IMAGE}" -c "
      set -e
      pip install --no-cache-dir --quiet pip-tools==${PIP_TOOLS_VERSION}
      pip-compile --generate-hashes --no-strip-extras --allow-unsafe \
                  --no-emit-index-url \
                  --no-header --quiet \
                  --output-file=/w/${tmp_name} /w/requirements.txt
    " || die "pip-compile failed for ${arch}. The existing lock is untouched."

  [[ -s "${BACKEND}/${tmp_name}" ]] \
    || die "pip-compile produced an empty file for ${arch}."

  cp "${BACKEND}/${tmp_name}" "${body_file}"
  rm -f "${BACKEND}/${tmp_name}"
  assert_no_credentials "${body_file}"

  assemble "${lock}" "${header_file}" "${body_file}" \
    "Compiled in ${PYTHON_IMAGE} (${platform})."
}

if [[ "${MODE}" == "native" ]]; then
  if [[ ${#ARCHES[@]} -eq 0 ]]; then
    regenerate_native
  else
    [[ ${#ARCHES[@]} -gt 1 ]] && die "native mode compiles for THIS host only;
      pass at most one name. Use --docker to cross-compile the other arch."
    regenerate_native "${ARCHES[0]}"
  fi
else
  [[ ${#ARCHES[@]} -eq 0 ]] && ARCHES=(amd64 arm64)
  for arch in "${ARCHES[@]}"; do
    regenerate_docker "${arch}"
  done
fi

# ---------------------------------------------------------------------------
# Verify. Regenerating without checking is how you discover in the next
# compliance scan that it did not work.
# ---------------------------------------------------------------------------
echo
info "verifying with check-sbom-scope.sh"
if bash "${REPO_ROOT}/scripts/ci/check-sbom-scope.sh"; then
  echo
  info "locks regenerated and verified."
else
  echo
  die "locks were regenerated but the scope guard still fails (see above).
      Do not commit until this is understood: it means requirements.txt and
      the resulting closure still disagree with what the SBOM expects.
      NOTE: if you regenerated only one architecture, failures about the OTHER
      lock are expected — regenerate it too before committing."
fi
