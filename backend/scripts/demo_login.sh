#!/usr/bin/env bash
# Log the demo operator in and leave a usable session cookie in ./cj.txt.
#
# The account is an ADMIN, and admin MFA is enforced by design
# (`admin_mfa_required`), so a plain username/password POST returns
# `mfa_enrollment_required` rather than a session. This walks the real
# enrolment flow — setup, then activate with a computed TOTP — instead of
# turning the control off. After the first run the account is enrolled, and
# subsequent logins take the shorter `mfa_required` branch.
#
# Usage:  bash backend/scripts/demo_login.sh [base_url]
#         curl -b cj.txt "$BASE/api/sim/packs" | python3 -m json.tool
set -euo pipefail

BASE="${1:-http://localhost:18000}"
USERNAME="${DEMO_USER:-demo}"
PASSWORD="${DEMO_PASS:-demo1234}"
COOKIE="cj.txt"
rm -f "$COOKIE"

step() { printf '\n== %s\n' "$1"; }

step "login"
LOGIN=$(curl -s -c "$COOKIE" -X POST "$BASE/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USERNAME\",\"password\":\"$PASSWORD\"}")
MFA_TOKEN=$(printf '%s' "$LOGIN" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("mfa_token") or "")')

if [ -z "$MFA_TOKEN" ]; then
  echo "session established without MFA"
  exit 0
fi

NEEDS_ENROL=$(printf '%s' "$LOGIN" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("mfa_enrollment_required") or "")')

if [ -n "$NEEDS_ENROL" ]; then
  step "mfa/setup — mint the pending secret"
  SECRET=$(curl -s -X POST "$BASE/api/auth/mfa/setup" \
    -H "Authorization: Bearer $MFA_TOKEN" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("secret") or d.get("otp_secret") or "")')
  [ -n "$SECRET" ] || { echo "no secret returned from /auth/mfa/setup"; exit 1; }
  echo "secret issued (${#SECRET} chars) — keep it if you want to add this to an authenticator"

  step "mfa/activate — first TOTP code"
  CODE=$(python3 "$(dirname "$0")/_totp.py" "$SECRET")
  curl -s -c "$COOKIE" -X POST "$BASE/api/auth/mfa/activate" \
    -H "Authorization: Bearer $MFA_TOKEN" -H 'Content-Type: application/json' \
    -d "{\"code\":\"$CODE\"}" >/dev/null
  printf '%s\n' "$SECRET" > .demo_mfa_secret
  echo "enrolled; secret cached in .demo_mfa_secret for future logins"
else
  step "mfa/verify — existing enrolment"
  [ -f .demo_mfa_secret ] || { echo "already enrolled but .demo_mfa_secret is missing"; exit 1; }
  SECRET=$(cat .demo_mfa_secret)
  CODE=$(python3 "$(dirname "$0")/_totp.py" "$SECRET")
  # /mfa/verify takes mfa_token in the JSON body (MfaVerifyRequest), NOT as a
  # Bearer header — unlike /mfa/setup and /mfa/activate, which use the
  # EnrollingUser header-based dependency. Passing it as a header here silently
  # 401s on the unauthenticated /auth/me check below instead of failing loudly.
  curl -s -c "$COOKIE" -X POST "$BASE/api/auth/mfa/verify" \
    -H 'Content-Type: application/json' \
    -d "{\"mfa_token\":\"$MFA_TOKEN\",\"code\":\"$CODE\"}" >/dev/null
fi

step "check"
curl -s -b "$COOKIE" "$BASE/api/auth/me" | head -c 200
printf '\n\nsession cookie written to %s\n' "$COOKIE"
