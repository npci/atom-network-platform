#!/usr/bin/env bash
# Sample Phase B build+deploy script for the NLLN (library-loan) fixture repo.
#
# Invoked by the platform as:  bash build_and_deploy.sh <branch_a> <branch_b>
# (the two branch arguments mirror the operator script contract; this sample
# builds the checkout as-is and just echoes them).
#
# What it does — real work, honestly labelled, no network required:
#   build:   validate the NLLN XSD contract, package the contract module into
#            a versioned artifact (a real jar when the JDK's `jar` tool is
#            available, else a tar.gz)
#   deploy:  copy the artifact into <repo-root>/.run/deploy/nlln/releases/
#   startup: re-verify the deployed artifact + contract (no services are
#            started — this is a contract deployment, and the log says so)
#
# Fixture location: $NLLN_FIXTURES_DIR when set, else
# <repo-root>/.run/repos/nllm-fixtures (see docs/LOCAL_RUN_CONTEXT_TRANSFER_PROMPT.md §6).
#
# Output conventions the platform parses (see services/build_runner.py):
#   "== Deploy =="/"== Startup ==" section headers, "Building jar: <path>",
#   "cp <artifact> <dest>", and a final "BUILD SUCCESS"/"BUILD FAILURE".
set -u

BRANCH_A="${1:-master}"
BRANCH_B="${2:-master}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
FIXTURES="${NLLN_FIXTURES_DIR:-$REPO_ROOT/.run/repos/nllm-fixtures}"
XSD="$FIXTURES/nlln-contract/src/main/resources/xsd/nlln-v1.xsd"
DIST="$FIXTURES/nlln-contract/dist"
DEPLOY_DIR="$REPO_ROOT/.run/deploy/nlln/releases"
VERSION="1.0"

fail() {
    echo "[ERROR] $*"
    echo "BUILD FAILURE"
    exit 1
}

echo "NLLN contract build — fixture repo: $FIXTURES"
echo "requested branches: $BRANCH_A / $BRANCH_B (building the checkout as-is)"
[ -d "$FIXTURES" ] || fail "fixture repo not found — clone gitlab.com/NirbhayN/nllm-fixtures to $FIXTURES or set NLLN_FIXTURES_DIR"
[ -f "$XSD" ]      || fail "contract schema missing: $XSD"

echo "Validating contract schema nlln-v1.xsd ..."
if command -v xmllint >/dev/null 2>&1; then
    xmllint --noout "$XSD" || fail "nlln-v1.xsd is not well-formed XML"
else
    python3 - "$XSD" <<'PYEOF' || fail "nlln-v1.xsd is not well-formed XML"
import sys, xml.dom.minidom
xml.dom.minidom.parse(sys.argv[1])
PYEOF
fi
MSGS=$(grep -c 'xs:element name="\(Req\|Resp\)' "$XSD" || true)
echo "Schema OK — $MSGS top-level message declarations found"

echo "Packaging nlln-contract $VERSION ..."
mkdir -p "$DIST"
if command -v jar >/dev/null 2>&1; then
    ARTIFACT="$DIST/nlln-contract-$VERSION.jar"
    rm -f "$ARTIFACT"
    jar cf "$ARTIFACT" -C "$FIXTURES/nlln-contract/src/main/resources" xsd \
        || fail "jar packaging failed"
else
    ARTIFACT="$DIST/nlln-contract-$VERSION.tar.gz"
    tar czf "$ARTIFACT" -C "$FIXTURES/nlln-contract/src/main/resources" xsd \
        || fail "tar packaging failed"
fi
echo "Building jar: $ARTIFACT"

echo "== Deploy =="
mkdir -p "$DEPLOY_DIR" || fail "cannot create deploy dir $DEPLOY_DIR"
echo "cp $ARTIFACT $DEPLOY_DIR/$(basename "$ARTIFACT")"
cp "$ARTIFACT" "$DEPLOY_DIR/" || fail "deploy copy failed"
echo "-- Deploy complete: $DEPLOY_DIR/$(basename "$ARTIFACT")"

echo "== Startup =="
echo "No services are started by this sample — it deploys the NLLN contract artifact only."
echo "Verifying deployed artifact ..."
[ -s "$DEPLOY_DIR/$(basename "$ARTIFACT")" ] || fail "deployed artifact is missing or empty"
echo "Deployed artifact verified ($(wc -c < "$DEPLOY_DIR/$(basename "$ARTIFACT")") bytes)"

echo "BUILD SUCCESS"
exit 0
