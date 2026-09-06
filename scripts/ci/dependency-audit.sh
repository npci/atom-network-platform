#!/usr/bin/env bash
# Dependency CVE audit — Python (pip-audit) + both frontends (npm audit).
#
# Separate from hygiene-check.sh on purpose: hygiene-check is offline, fast and
# runs on the host with nothing but git+grep, so it can gate every commit. This
# one needs a package index and the built image, takes minutes, and its result
# changes when NOTHING in the repo changed (a new advisory drops). Mixing the
# two would make a fast deterministic gate slow and flaky.
#
#   bash scripts/ci/dependency-audit.sh            # report only, always exit 0
#   FAIL_ON_VULN=1 bash scripts/ci/dependency-audit.sh   # exit 1 if any found
#
# Start in report mode. Move to FAIL_ON_VULN=1 once the baseline is clean,
# otherwise the gate is red on day one and everyone learns to ignore it.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"
fail=0

echo "── Python (pip-audit, against the built image) ────────────────────"
if docker compose run --rm -T backend sh -c \
     'pip install -q pip-audit >/dev/null 2>&1; python -m pip_audit --progress-spinner off -f columns' \
     2>/dev/null | tail -n +1 | tee /tmp/pip-audit.out; then
  n=$(grep -cE '^[a-zA-Z0-9_.-]+ +[0-9]' /tmp/pip-audit.out 2>/dev/null || echo 0)
  echo "  advisories: ${n}"
  [ "${n:-0}" -gt 0 ] && fail=1
else
  echo "  SKIPPED — could not run pip-audit (is the backend image built?)"
fi

for fe in frontend partner-platform/frontend; do
  echo
  echo "── npm audit — ${fe} ──────────────────────────────────────────────"
  if [ -f "$fe/package-lock.json" ]; then
    (cd "$fe" && npm audit --production --audit-level=high 2>&1 | tail -15) || fail=1
  else
    echo "  SKIPPED — no package-lock.json (run npm install first)"
  fi
done

echo
if [ "$fail" -eq 0 ]; then
  echo "dependency audit: no HIGH/CRITICAL findings"
else
  echo "dependency audit: findings above — see docs/CODE_QUALITY_SECURITY_REVIEW.md SEC-3"
fi
[ "${FAIL_ON_VULN:-0}" = "1" ] && exit "$fail"
exit 0
