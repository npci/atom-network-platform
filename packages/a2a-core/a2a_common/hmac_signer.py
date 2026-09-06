# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""HMAC envelope for A2A messages — sign + verify + replay protection.

Slice 5 of the A2A security hardening. Distinct from the Bearer JWT
(Slice 2/3): the JWT proves WHO is calling; the HMAC envelope proves
WHAT was sent (non-repudiation) and that it has not been replayed
(timestamp window + nonce uniqueness via redis).

Wire shape — three additional headers on every JSON-RPC request:

    X-NPCI-Timestamp   integer Unix seconds (UTC)
    X-NPCI-Nonce       32-char lowercase hex (16 random bytes)
    X-NPCI-Signature   lowercase hex of
                       HMAC-SHA256(secret, f"{ts}.{nonce}.{sha256_hex(body)}")

Both sides hash the raw HTTP request body bytes — no canonicalisation
needed because both sides see the same bytes on the wire (the SDK's
JSON-RPC envelope is already deterministic enough; we don't reorder
keys).

Receiver verification logic in `verify()`:
    1. Headers present and parsable                      → invalid_envelope
    2. |now - ts| <= MAX_SKEW_S                          → timestamp_skew
    3. Nonce unseen in redis (SETNX with TTL = NONCE_TTL_S)
                                                          → replay_detected
    4. constant-time-equal recomputed signature          → signature_mismatch

If `redis_client` is None (caller couldn't reach redis), step 3 is
skipped. If redis raises during the SETNX, the request is ALWAYS
REJECTED (fail-closed) — there is no env-var escape hatch. A previous
`HMAC_FAIL_OPEN` toggle allowed skipping the nonce check on a Redis
failure; it was removed because it silently reduced replay protection
to the timestamp-skew window alone, which is not acceptable for a
security control that can be flipped by an unreviewed environment
variable.

Both sides ship this same file. NPCI:
    backend/app/a2a_common/hmac_signer.py
Partner mirror:
    backend/app/a2a_common/hmac_signer.py in the atom-partner-platform
    repository. It is outside this repo's sync + hygiene gate, so a change
    here must be landed there in the same release or signatures stop matching
    across the trust boundary.

Public surface:
    HEADER_TIMESTAMP, HEADER_NONCE, HEADER_SIGNATURE
    sign(body, secret, *, ts=None, nonce=None)            -> dict[str, str]
    verify(headers, body, secret, *, redis_client=None,
           max_skew_s=300, nonce_ttl_s=600)               -> tuple[bool, str|None]
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Header names — kept under the X-NPCI-* prefix so nginx + ops dashboards
# can filter by namespace (banks have their own X-Bank-* heritage).
HEADER_TIMESTAMP = "X-NPCI-Timestamp"
HEADER_NONCE     = "X-NPCI-Nonce"
HEADER_SIGNATURE = "X-NPCI-Signature"

# Defaults. Tuned conservatively:
#   5-min skew window covers most NTP drift between data-centres
#   while still bounding worst-case replay if redis is down.
#   10-min nonce TTL is 2x skew → guarantees uniqueness over the
#   window even with clock drift.
DEFAULT_MAX_SKEW_S = 300
DEFAULT_NONCE_TTL_S = 600

# REMOVED: HMAC_FAIL_OPEN env var. A redis outage now ALWAYS causes the
# request to be rejected (fail-closed). The old degraded-mode behaviour
# (skip nonce check, rely on timestamp window alone) was removed because
# it silently reduced replay protection to a 5-minute window — acceptable
# for a deliberate operational choice but not as a silent env-var-driven
# downgrade.


def _body_hash(body: bytes) -> str:
    """SHA-256 hex of the raw body bytes. Both sides hash the same wire
    bytes, so no canonicalisation step is needed."""
    return hashlib.sha256(body).hexdigest()


def _string_to_sign(ts: str, nonce: str, body: bytes) -> bytes:
    """The bytes fed into HMAC. Format: `ts.nonce.body_sha256_hex`.
    Stable, simple, and unambiguous (none of the three components can
    contain a `.` — ts is integer, nonce is hex, hash is hex)."""
    return f"{ts}.{nonce}.{_body_hash(body)}".encode()


def sign(
    body: bytes,
    secret: str,
    *,
    ts: Optional[int] = None,
    nonce: Optional[str] = None,
) -> dict[str, str]:
    """Compute the three envelope headers for outbound `body`.

    `ts` and `nonce` are overridable for tests; production callers
    should pass neither and let the helper fill them.
    """
    if not secret:
        raise ValueError("hmac_signer.sign: empty signing secret")
    ts_str = str(ts if ts is not None else int(time.time()))
    nonce_str = nonce if nonce is not None else secrets.token_hex(16)
    sig = hmac.new(
        secret.encode(),
        _string_to_sign(ts_str, nonce_str, body),
        hashlib.sha256,
    ).hexdigest()
    return {
        HEADER_TIMESTAMP: ts_str,
        HEADER_NONCE:     nonce_str,
        HEADER_SIGNATURE: sig,
    }


def verify(
    headers: dict[str, str],
    body: bytes,
    secret: str,
    *,
    redis_client=None,
    max_skew_s: int = DEFAULT_MAX_SKEW_S,
    nonce_ttl_s: int = DEFAULT_NONCE_TTL_S,
) -> Tuple[bool, Optional[str]]:
    """Verify the envelope. Returns `(ok, error_code)`.

    `headers` is a case-insensitive mapping (Starlette's `Headers` and
    standard dict both work; we lowercase-lookup).

    `redis_client` is a `redis.Redis` (sync) or compatible. Pass None
    to skip nonce uniqueness — the timestamp window then bounds replay
    risk to `max_skew_s` seconds. If redis raises during SETNX the
    request is REJECTED (fail-closed). The old HMAC_FAIL_OPEN env var
    that allowed skipping the nonce check on redis failure has been
    removed — it silently reduced replay protection to the timestamp
    window alone, which is not acceptable for a security control.
    """
    if not secret:
        return False, "missing_secret"

    # Case-insensitive header lookup.
    def _h(name: str) -> Optional[str]:
        if hasattr(headers, "get"):
            return headers.get(name) or headers.get(name.lower())
        return None

    ts_str = _h(HEADER_TIMESTAMP)
    nonce  = _h(HEADER_NONCE)
    sig    = _h(HEADER_SIGNATURE)
    if not (ts_str and nonce and sig):
        return False, "missing_envelope_headers"

    # Timestamp parse + skew check.
    try:
        ts = int(ts_str)
    except ValueError:
        return False, "invalid_envelope"
    now = int(time.time())
    if abs(now - ts) > max_skew_s:
        return False, "timestamp_skew"

    # Recompute and constant-time compare.
    expected = hmac.new(
        secret.encode(),
        _string_to_sign(ts_str, nonce, body),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False, "signature_mismatch"

    # Nonce uniqueness — last because it has a side effect (the SETNX
    # records the nonce as seen). We don't want to record nonces for
    # requests that fail earlier checks.
    if redis_client is not None:
        try:
            # SETNX semantics: returns True if key was set, False if it
            # already existed. python-redis exposes this as `set(..., nx=True)`.
            ok = redis_client.set(
                f"a2a:nonce:{nonce}", "1",
                nx=True, ex=nonce_ttl_s,
            )
            if not ok:
                return False, "replay_detected"
        except Exception as exc:  # noqa: BLE001
            # Type only: redis-py builds its error messages from the
            # connection URL, so `str(exc)` on a ConnectionError routinely
            # contains `redis://:<password>@host:port` — a credential leak
            # into the log (CWE-209). The class name still distinguishes the
            # operational cases that matter here (ConnectionError vs.
            # TimeoutError vs. ResponseError). Full traceback at DEBUG.
            logger.critical(
                "hmac_signer.verify: redis unavailable (%s); "
                "REJECTING request (fail-closed).", type(exc).__name__,
            )
            logger.debug("nonce store failure detail", exc_info=True)
            return False, "nonce_check_unavailable"

    return True, None
