#!/usr/bin/env bash
#
# Generate the CycloneDX SBOM for a compliance scan.
#
# ============================================================================
# WHY THIS SCRIPT EXISTS
# ============================================================================
# The 2026-08-27 A2A compliance report contained 18 violations. SEVEN of them
# were artifacts of HOW THE SBOM WAS GENERATED, not of what this platform
# actually ships:
#
#   - js-yaml 4.3.1     CVSS 7.5   <- eslint's config loader   (devDependency)
#   - minimatch 3.1.5   CVSS 8.7   <- eslint's glob matcher    (devDependency)
#   - zod 4.3.6         CVSS 5.3   <- eslint-plugin-react-hooks(devDependency)
#   - doctrine 2.1.0    EOL        <- eslint-plugin-react      (devDependency)
#   - prop-types 15.8.1 EOL        <- eslint-plugin-react      (devDependency)
#   - json-buffer 3.0.1 EOL        <- eslint's cache via keyv  (devDependency)
#   - async-timeout     EOL        <- a dead pin in requirements.txt that is
#                                     not in either lockfile and therefore not
#                                     in the image
#
# None of those six npm packages is in the production dependency tree
# (`npm ls --omit=dev` reports "(empty)" for all six) and none appears in the
# built bundle (grepped dist/assets/*.js — zero hits). They exist so that
# `npm run lint` works on a developer's laptop. A linter's glob matcher is not
# part of the attack surface of a deployed application.
#
# The previous SBOM was built from SOURCE MANIFESTS (package.json,
# requirements.txt). This script builds it from what is ACTUALLY SHIPPED. The
# distinction is the whole point: requirements.txt pins versions, but the
# Dockerfile installs from requirements.${ARCH}.lock, so a package can sit in
# requirements.txt and never reach the image — which is exactly what
# async-timeout did.
#
# ============================================================================
# USAGE
# ============================================================================
#   scripts/ci/generate-sbom.sh                 # from source manifests (fast)
#   scripts/ci/generate-sbom.sh --from-venv     # from the installed venv
#   scripts/ci/generate-sbom.sh --from-images   # from built container images
#
# WHICH MODE FOR A COMPLIANCE SUBMISSION: the one that matches how you deploy.
#
#   Deployed ON THE HOST (virtualenv + systemd/service manager)  -> --from-venv
#   Deployed as CONTAINERS                                       -> --from-images
#
# Both scan a real installed closure, so neither can be wrong about what is
# present. The default (manifest) mode is for quick local iteration and CI
# drift-detection only: it reads the lockfile, which is a CLAIM about what will
# be installed. That claim is exactly what drifted — the lock still listed
# torch 2.9.1+cpu while the running venv had 2.13.0+cpu.
#
# --from-venv needs cyclonedx-bom in the target venv and honours BACKEND_VENV
# (and optionally RERANKER_VENV). --from-images needs the images built and
# `syft` on PATH.
#
# Output: docs/sbom/*.cdx.json  (CycloneDX 1.6 JSON)
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="${REPO_ROOT}/docs/sbom"
FROM_IMAGES=false
FROM_VENV=false
case "${1:-}" in
  --from-images) FROM_IMAGES=true ;;
  --from-venv)   FROM_VENV=true ;;
  ""|--fast)     ;;
  *) echo "unknown argument: $1" >&2
     echo "usage: $0 [--from-venv | --from-images | --fast]" >&2
     exit 2 ;;
esac

mkdir -p "${OUT_DIR}"

# ---------------------------------------------------------------------------
# SBOM metadata. The 2026-08-27 report's header read:
#     Author: NONE   Manufacturer: NONE   Supplier: NONE
# For a document titled "A2A Compliance Report" that is weak evidence — anyone
# can produce a JSON file; provenance is what makes it meaningful. Override
# these via the environment in CI so the values reflect the real publisher.
# ---------------------------------------------------------------------------
SBOM_AUTHOR="${SBOM_AUTHOR:-AtOM Platform Team}"
SBOM_SUPPLIER="${SBOM_SUPPLIER:-AtOM}"
SBOM_VERSION="${SBOM_VERSION:-$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo unknown)}"

echo "==> SBOM metadata: author='${SBOM_AUTHOR}' supplier='${SBOM_SUPPLIER}' version='${SBOM_VERSION}'"

if [[ "${FROM_VENV}" == "true" ]]; then
  # -------------------------------------------------------------------------
  # HOST-DEPLOYMENT MODE — scan the virtualenv that actually serves traffic.
  #
  # USE THIS WHEN THE SERVERS RUN ON THE HOST, NOT IN CONTAINERS. It is the
  # host-deployment equivalent of --from-images: it reports the real installed
  # closure rather than what a manifest claims, so it cannot be wrong about
  # what is present.
  #
  # It is strictly better evidence than the lockfile for a host deployment,
  # because a venv drifts from the lock in both directions: packages installed
  # ad hoc are present but unlisted, and packages later removed from the lock
  # may still be sitting in site-packages. On this machine the venv held 169
  # packages against the lock's 172, including torch 2.13.0+cpu where the lock
  # still said 2.9.1+cpu — so scanning the lock would have described an
  # environment that does not exist.
  #
  # Point BACKEND_VENV at the deployed environment. Defaults to backend/venv.
  # -------------------------------------------------------------------------
  VENV="${BACKEND_VENV:-${REPO_ROOT}/backend/venv}"
  [[ -d "${VENV}" ]] || { echo "ERROR: no virtualenv at ${VENV}." >&2
                          echo "       Set BACKEND_VENV to the deployed one." >&2
                          exit 1; }

  VPY=""
  for p in "${VENV}/bin/python" "${VENV}/bin/python3" "${VENV}/Scripts/python.exe"; do
    [[ -x "${p}" ]] && { VPY="${p}"; break; }
  done
  [[ -n "${VPY}" ]] || { echo "ERROR: no interpreter inside ${VENV}." >&2; exit 1; }

  echo "==> backend (from the INSTALLED virtualenv: ${VENV})"
  echo "    interpreter: $("${VPY}" -c 'import sys,platform; print("python", sys.version.split()[0], platform.system(), platform.machine())')"

  # `cyclonedx_py environment` introspects installed distributions rather than
  # parsing a manifest — the same class of evidence syft gives for an image.
  # Run it FROM the target interpreter so it describes that environment.
  "${VPY}" -m cyclonedx_py environment \
      --spec-version 1.6 \
      --output-format JSON \
      --outfile "${OUT_DIR}/backend.cdx.json" \
    || { echo "ERROR: cyclonedx-py failed. Install it into the venv:" >&2
         echo "       ${VPY} -m pip install cyclonedx-bom" >&2; exit 1; }

  # The frontend is static files once built; there is no npm runtime on the
  # host. Production npm tree is still the right source for it.
  echo "==> frontend (production dependencies only)"
  ( cd "${REPO_ROOT}/frontend" && npx --yes @cyclonedx/cyclonedx-npm@latest \
      --omit dev \
      --spec-version 1.6 \
      --output-format JSON \
      --output-file "${OUT_DIR}/frontend.cdx.json" )

  # The reranker sidecar has its own environment. If it is deployed on the host
  # too, point RERANKER_VENV at it; otherwise fall back to its manifest.
  if [[ -n "${RERANKER_VENV:-}" && -d "${RERANKER_VENV}" ]]; then
    RPY=""
    for p in "${RERANKER_VENV}/bin/python" "${RERANKER_VENV}/bin/python3" \
             "${RERANKER_VENV}/Scripts/python.exe"; do
      [[ -x "${p}" ]] && { RPY="${p}"; break; }
    done
    echo "==> reranker sidecar (from ${RERANKER_VENV})"
    "${RPY}" -m cyclonedx_py environment \
        --spec-version 1.6 --output-format JSON \
        --outfile "${OUT_DIR}/reranker.cdx.json"
  else
    echo "==> reranker sidecar (from manifest — set RERANKER_VENV to scan its venv)"
    ( cd "${REPO_ROOT}/services/reranker" && "${VPY}" -m cyclonedx_py requirements \
        requirements.txt \
        --spec-version 1.6 \
        --output-format JSON \
        --outfile "${OUT_DIR}/reranker.cdx.json" )
  fi

elif [[ "${FROM_IMAGES}" == "true" ]]; then
  # -------------------------------------------------------------------------
  # PREFERRED MODE — scan the built artifacts.
  #
  # This is the only mode that cannot lie. It sees the installed closure, so:
  #   - dev npm packages are absent (the frontend image is nginx:alpine
  #     serving static files; it contains NO npm packages at all),
  #   - async-timeout is absent (never was in the lock),
  #   - torch/sentence-transformers are absent from the backend (they now live
  #     in the reranker sidecar, scanned as its own component).
  # -------------------------------------------------------------------------
  command -v syft >/dev/null 2>&1 || {
    echo "ERROR: syft not found. Install from https://github.com/anchore/syft" >&2
    exit 1
  }

  for img_spec in \
      "atom-backend:latest|backend" \
      "atom-frontend:latest|frontend" \
      "atom-reranker:latest|reranker"; do
    img="${img_spec%%|*}"
    name="${img_spec##*|}"
    if ! docker image inspect "${img}" >/dev/null 2>&1; then
      echo "WARN: image ${img} not built — skipping ${name}. Build it first for a complete SBOM." >&2
      continue
    fi
    echo "==> syft scan ${img}"
    syft scan "docker:${img}" \
      --output "cyclonedx-json=${OUT_DIR}/${name}.cdx.json" \
      --source-name "${name}" \
      --source-version "${SBOM_VERSION}"
  done
else
  # -------------------------------------------------------------------------
  # FAST MODE — from source manifests, with dev dependencies EXCLUDED.
  #
  # `--omit dev` is the flag that closes findings 3, 4, 8, 9, 10 and 15. It is
  # not a suppression: those packages genuinely are not part of the deployed
  # application, and the assertion is verified by the guard script
  # scripts/ci/check-sbom-scope.sh, which fails CI if any of them ever becomes
  # a production dependency.
  # -------------------------------------------------------------------------
  echo "==> frontend (production dependencies only)"
  ( cd "${REPO_ROOT}/frontend" && npx --yes @cyclonedx/cyclonedx-npm@latest \
      --omit dev \
      --spec-version 1.6 \
      --output-format JSON \
      --output-file "${OUT_DIR}/frontend.cdx.json" )

  echo "==> backend (from the LOCKFILE — the file the image actually installs)"
  # From the lock, NOT requirements.txt. requirements.txt carries pins that the
  # image does not install (that is how async-timeout reached the last report)
  # and extensive prose comments that are not dependency data.
  ARCH="${SBOM_ARCH:-amd64}"
  LOCK="${REPO_ROOT}/backend/requirements.${ARCH}.lock"
  [[ -f "${LOCK}" ]] || { echo "ERROR: ${LOCK} not found" >&2; exit 1; }
  ( cd "${REPO_ROOT}/backend" && python -m cyclonedx_py requirements \
      "${LOCK}" \
      --spec-version 1.6 \
      --output-format JSON \
      --outfile "${OUT_DIR}/backend.cdx.json" )

  echo "==> reranker sidecar (scanned separately — holds the torch stack)"
  ( cd "${REPO_ROOT}/services/reranker" && python -m cyclonedx_py requirements \
      requirements.txt \
      --spec-version 1.6 \
      --output-format JSON \
      --outfile "${OUT_DIR}/reranker.cdx.json" )
fi

echo
echo "==> SBOMs written to ${OUT_DIR}"
echo "    Attach docs/sbom/vex.json alongside these when submitting: it carries"
echo "    the analysis for findings that cannot be fixed by a version change"
echo "    (disputed CVEs, advisories with no fix, and one upstream metadata bug)."
