#!/usr/bin/env bash
# Sample Phase B UAT test script for the NLLN (library-loan) fixture repo —
# the COMBINED test-gen + test-exec step: it derives its checks from the
# fixture's own contract and executes them in one run.
#
# Invoked by the platform as:  bash run_uat_tests.sh [base_url]
# When a base_url is passed, one extra live-probe case runs against
# <base_url>/actuator/health; without it that case is skipped (nothing is
# assumed to be running).
#
# Output contract the platform parses (see services/uat_script.py):
#   one "PASS <id> <title>" / "FAIL <id> <title>" / "SKIP <id> <title>" line
#   per case, then a final "TESTS: total=N passed=N failed=N skipped=N".
#   Exit 0 iff no failures.
set -u

BASE_URL="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
FIXTURES="${NLLN_FIXTURES_DIR:-$REPO_ROOT/.run/repos/nllm-fixtures}"
DEPLOY_DIR="$REPO_ROOT/.run/deploy/nlln/releases"

echo "NLLN UAT suite — fixture repo: $FIXTURES"
[ -n "$BASE_URL" ] && echo "live target: $BASE_URL"

FIXTURES="$FIXTURES" DEPLOY_DIR="$DEPLOY_DIR" BASE_URL="$BASE_URL" python3 - <<'PYEOF'
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

fixtures = os.environ["FIXTURES"]
deploy_dir = os.environ["DEPLOY_DIR"]
base_url = os.environ.get("BASE_URL", "").strip()

xsd_path = os.path.join(fixtures, "nlln-contract/src/main/resources/xsd/nlln-v1.xsd")
error_code_java = os.path.join(fixtures, "nlln-contract/src/main/java/in/nlln/contract/v1/ErrorCode.java")
state_machine_java = os.path.join(fixtures, "nllc-authority/src/main/java/in/nlln/nllc/service/LoanStateMachine.java")

results = []  # (status, case_id, title, detail)


def case(case_id, title):
    def wrap(fn):
        try:
            out = fn()
        except Exception as e:  # noqa: BLE001 — a crashing check is a failing check
            results.append(("FAIL", case_id, title, f"check crashed: {e}"))
            return
        if out is None:
            results.append(("PASS", case_id, title, ""))
        elif out == "SKIP":
            results.append(("SKIP", case_id, title, ""))
        else:
            results.append(("FAIL", case_id, title, str(out)))
    return wrap


@case("NLLN-001", "contract schema parses as XML")
def _():
    ET.parse(xsd_path)


@case("NLLN-002", "contract targets the NLLN v1 namespace")
def _():
    tns = ET.parse(xsd_path).getroot().get("targetNamespace")
    if tns != "http://nlln.in/schema/v1":
        return f"targetNamespace is {tns!r}"


@case("NLLN-003", "all five Req/Resp message pairs are declared")
def _():
    root = ET.parse(xsd_path).getroot()
    names = {el.get("name") for el in root
             if el.tag.endswith("}element") and el.get("name")}
    reqs = {n for n in names if n.startswith("Req")}
    missing = sorted(f"Resp{r[3:]}" for r in reqs if f"Resp{r[3:]}" not in names)
    if len(reqs) < 5:
        return f"expected at least 5 Req messages, found {len(reqs)}: {sorted(reqs)}"
    if missing:
        return f"Req messages without a Resp counterpart: {missing}"


@case("NLLN-004", "error catalogue uses the published E-code format")
def _():
    text = open(error_code_java, encoding="utf-8").read()
    codes = re.findall(r"^\s*(E\d{3})\(", text, re.MULTILINE)
    if len(codes) < 8:
        return f"expected the 8 published E-codes, found {len(codes)}: {codes}"
    dupes = {c for c in codes if codes.count(c) > 1}
    if dupes:
        return f"duplicate error codes: {sorted(dupes)}"


@case("NLLN-005", "loan state machine permits only the published transitions")
def _():
    text = open(state_machine_java, encoding="utf-8").read()
    for event in ("RESERVE", "ISSUE", "CLOSE"):
        if f'"{event}"' not in text:
            return f"event {event} missing from the transition table"
    # The documented forbidden edge: a CLOSE from NONE must not be allowed.
    m = re.search(r'"CLOSE",\s*Set\.of\(([^)]*)\)', text)
    if not m:
        return "CLOSE transition entry not found"
    if "NONE" in m.group(1):
        return "state machine permits CLOSE from NONE (forbidden transition)"


@case("NLLN-006", "build artifact is deployed")
def _():
    if not os.path.isdir(deploy_dir):
        return "SKIP"     # build+deploy has not run yet in this environment
    arts = [f for f in os.listdir(deploy_dir) if f.startswith("nlln-contract-")]
    if not arts:
        return f"no nlln-contract artifact in {deploy_dir}"


@case("NLLN-007", "deployed service health endpoint responds")
def _():
    if not base_url:
        return "SKIP"     # no live target supplied to the script
    url = base_url.rstrip("/") + "/actuator/health"
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 — operator-supplied http(s) URL
        if resp.status != 200:
            return f"{url} returned HTTP {resp.status}"


for status, case_id, title, detail in results:
    line = f"{status} {case_id} {title}"
    if detail:
        line += f" — {detail}"
    print(line)

total = len(results)
passed = sum(1 for r in results if r[0] == "PASS")
failed = sum(1 for r in results if r[0] == "FAIL")
skipped = sum(1 for r in results if r[0] == "SKIP")
print(f"TESTS: total={total} passed={passed} failed={failed} skipped={skipped}")
sys.exit(1 if failed else 0)
PYEOF
