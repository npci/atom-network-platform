# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Integration-testing tunnel — the wire contract. PURE: no I/O, no settings.

One encapsulated HTTP exchange, carried over A2A between two platforms
(ITA plan §5). This module is the half that decides whether the tunnel is
TRANSPARENT: encoding, header classification and digest verification. Its twin
`integration_allowlist.py` decides whether it is SAFE.

Both are vendored byte-for-byte into every service that speaks this wire —
a tunnel whose two ends disagree about header rules is the silent-corruption
case the vendoring convention exists for. Which is also why NOTHING here may
import a service's settings, models or logger: the file has to be identical on
both sides, and the two sides have different everything else.

THE THREE INVARIANTS, each of which has a failure mode worth naming:

1. **The query string is opaque.** It is carried as a STRING and never parsed,
   re-encoded, reordered or filtered. Contract selection rides on it
   (`?pack=CHG-4711%403`), so a tunnel that "helpfully" normalises it presents
   as "the run certified against baseline" — a false pass, not an error
   (ITA §12.5).
2. **Headers are a LIST OF PAIRS, never a map.** HTTP permits repeats
   (`Set-Cookie`, `Via`); a dict silently drops all but one.
3. **Bodies are base64 with a SHA-256 digest.** JSON cannot hold arbitrary
   bytes, and a test tunnel that corrupts a binary payload is worse than no
   tunnel. The digest is verified on arrival so a corrupted exchange fails
   loudly instead of replaying wrong bytes.
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "TunnelError", "ErrorCode", "TUNNEL_TASK_TYPES",
    "HOP_BY_HOP_HEADERS", "RECOMPUTED_HEADERS",
    "HttpRequestSpec", "HttpResponseSpec", "DecodedRequest", "DecodedResponse",
    "classify_headers", "body_digest",
    "encode_request", "decode_request",
    "encode_response", "encode_error", "decode_response",
]

# The wire task types that carry tunnelled exchanges. Named HERE, in the
# shared contract, because BOTH platforms' retry sweepers must exclude them
# (ITA I-5): a tunnelled POST is a business call on the far side, and a
# sweeper replay is a duplicate business call, not a harmless redelivery. The
# tunnel does its own bounded, idempotency-aware retry or none at all.
TUNNEL_TASK_TYPES = frozenset({"http_exchange_request", "http_exchange_response"})

# Structured so the far side can ASSERT on the failure, not scrape prose.
class ErrorCode:
    UNKNOWN_ALIAS = "unknown_alias"
    PATH_NOT_ALLOWED = "path_not_allowed"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    TARGET_TIMEOUT = "target_timeout"
    TARGET_UNREACHABLE = "target_unreachable"
    HOP_LIMIT_EXCEEDED = "hop_limit_exceeded"
    DIGEST_MISMATCH = "digest_mismatch"
    TUNNEL_DISABLED = "tunnel_disabled"
    MALFORMED_EXCHANGE = "malformed_exchange"
    # ITA I-5 — the egress's own resilience refusals, distinct codes so the
    # caller can tell "the target is slow" from "the tunnel is protecting the
    # target": too many concurrent calls to one alias, or an alias whose
    # recent calls all failed (circuit open; retry after the cooldown).
    BULKHEAD_SATURATED = "bulkhead_saturated"
    CIRCUIT_OPEN = "circuit_open"

    ALL = frozenset({
        UNKNOWN_ALIAS, PATH_NOT_ALLOWED, PAYLOAD_TOO_LARGE, TARGET_TIMEOUT,
        TARGET_UNREACHABLE, HOP_LIMIT_EXCEEDED, DIGEST_MISMATCH,
        TUNNEL_DISABLED, MALFORMED_EXCHANGE,
        BULKHEAD_SATURATED, CIRCUIT_OPEN,
    })


class TunnelError(Exception):
    """A tunnel failure with a machine-readable code from `ErrorCode`."""

    def __init__(self, code: str, detail: str = ""):
        if code not in ErrorCode.ALL:
            raise ValueError(f"unknown tunnel error code {code!r}")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


# RFC 9110 §7.6.1 — these describe ONE connection and are meaningless after a
# hop. Forwarding `Transfer-Encoding` in particular desynchronises the next
# connection's framing.
HOP_BY_HOP_HEADERS = frozenset({
    "connection", "keep-alive", "te", "trailer", "transfer-encoding",
    "upgrade", "proxy-authenticate", "proxy-authorization", "proxy-connection",
})

# These describe the NEW connection; the egress side recomputes them. Carrying
# the origin's `Content-Length` across a hop is how a truncated body reaches a
# target and reads as a malformed request rather than a tunnel bug.
RECOMPUTED_HEADERS = frozenset({"host", "content-length"})

_MAX_HOPS_DEFAULT = 1


@dataclass(frozen=True)
class HttpRequestSpec:
    """One HTTP request, decoupled from any client library."""

    method: str
    path: str
    # VERBATIM. A string, never a parsed structure — see invariant 1.
    query: str = ""
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""


@dataclass(frozen=True)
class HttpResponseSpec:
    status: int
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""


@dataclass(frozen=True)
class DecodedRequest:
    exchange_id: str
    hop: int
    alias: str
    request: HttpRequestSpec
    deadline_ms: int | None = None
    cert_context: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class DecodedResponse:
    exchange_id: str
    response: HttpResponseSpec | None = None
    elapsed_ms: int | None = None
    error: Mapping[str, Any] | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None


def body_digest(body: bytes) -> str:
    """Lowercase hex SHA-256 of the exact bytes on the wire."""
    return hashlib.sha256(body or b"").hexdigest()


def classify_headers(
    headers: Iterable[Sequence[str]],
    *,
    strip: Iterable[str] = (),
) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
    """Split headers into (forwarded, dropped), preserving ORDER and REPEATS.

    Dropped = hop-by-hop, plus the recomputed pair, plus this alias's
    `strip_headers`. Everything else is forwarded — INCLUDING `Authorization`
    and `Cookie`, which is a deliberate decision (ITA §5.3): a tunnel that
    strips credentials cannot test an authenticated API, so transparency wins
    and `strip_headers` is the per-alias escape hatch for targets where that is
    unacceptable.

    Dropped headers are returned rather than discarded so the caller can log
    them per exchange — "my header vanished" is otherwise undiagnosable.
    """
    strip_lower = {h.strip().lower() for h in strip if h and h.strip()}
    forwarded: list[tuple[str, str]] = []
    dropped: list[tuple[str, str]] = []
    for pair in headers:
        name, value = pair[0], pair[1]
        lowered = name.strip().lower()
        blocked = (
            lowered in HOP_BY_HOP_HEADERS
            or lowered in RECOMPUTED_HEADERS
            or lowered in strip_lower
            # `Connection: X` names further hop-by-hop headers, but honouring
            # that requires the Connection value; the fixed set above covers
            # the standard names, which is what a test tunnel meets.
            or lowered.startswith("proxy-")
        )
        (dropped if blocked else forwarded).append((name, value))
    return tuple(forwarded), tuple(dropped)


def _b64(body: bytes) -> str:
    return base64.b64encode(body or b"").decode("ascii")


def _unb64(value: Any, *, what: str) -> bytes:
    if value in (None, ""):
        return b""
    if not isinstance(value, str):
        raise TunnelError(ErrorCode.MALFORMED_EXCHANGE, f"{what} must be a base64 string")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        # `binascii.Error` subclasses ValueError; TypeError covers a non-str
        # slipping past the check above. Named rather than caught broadly so a
        # genuinely unexpected failure still surfaces as itself.
        raise TunnelError(ErrorCode.MALFORMED_EXCHANGE, f"{what} is not valid base64: {exc}") from None


def _header_pairs(raw: Any, *, what: str) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    if isinstance(raw, Mapping):
        # Explicitly refused: a map cannot represent repeated headers, so
        # accepting one here would silently drop `Set-Cookie`s at the boundary.
        raise TunnelError(ErrorCode.MALFORMED_EXCHANGE,
                          f"{what} must be a list of [name, value] pairs, not an object")
    out: list[tuple[str, str]] = []
    for item in raw:
        if isinstance(item, (str, bytes)) or len(item) != 2:
            raise TunnelError(ErrorCode.MALFORMED_EXCHANGE,
                              f"{what} entries must be [name, value] pairs")
        out.append((str(item[0]), str(item[1])))
    return tuple(out)


def encode_request(
    *,
    exchange_id: str,
    alias: str,
    request: HttpRequestSpec,
    deadline_ms: int | None = None,
    hop: int = 1,
    cert_context: Mapping[str, Any] | None = None,
    max_body_bytes: int | None = None,
) -> dict:
    """Build the `http_exchange_request` payload.

    `alias` — never a URL. The receiving side resolves it against its OWN
    allowlist; a URL on the wire is the SSRF the whole design refuses
    (ITA §2).
    """
    if not exchange_id:
        raise TunnelError(ErrorCode.MALFORMED_EXCHANGE, "exchange_id is required")
    if not alias:
        raise TunnelError(ErrorCode.MALFORMED_EXCHANGE, "target alias is required")
    body = request.body or b""
    if max_body_bytes is not None and len(body) > max_body_bytes:
        raise TunnelError(ErrorCode.PAYLOAD_TOO_LARGE,
                          f"request body {len(body)}B exceeds {max_body_bytes}B")
    payload: dict[str, Any] = {
        "exchange_id": exchange_id,
        "hop": int(hop),
        "target": {"alias": alias},
        "request": {
            "method": (request.method or "GET").upper(),
            "path": request.path or "/",
            # Verbatim, and always present as a string so the far side never
            # has to distinguish "absent" from "empty".
            "query": request.query or "",
            "headers": [list(h) for h in request.headers],
            "body_b64": _b64(body),
            "body_sha256": body_digest(body),
        },
    }
    if deadline_ms is not None:
        payload["deadline_ms"] = int(deadline_ms)
    if cert_context:
        payload["cert_context"] = dict(cert_context)
    return payload


def decode_request(
    payload: Mapping[str, Any],
    *,
    max_hops: int = _MAX_HOPS_DEFAULT,
    max_body_bytes: int | None = None,
) -> DecodedRequest:
    """Parse and VERIFY an inbound `http_exchange_request`.

    Raises `TunnelError` with the code the far side should see. The digest is
    checked here, before any bytes are replayed at a target.
    """
    if not isinstance(payload, Mapping):
        raise TunnelError(ErrorCode.MALFORMED_EXCHANGE, "payload must be an object")
    exchange_id = str(payload.get("exchange_id") or "")
    if not exchange_id:
        raise TunnelError(ErrorCode.MALFORMED_EXCHANGE, "exchange_id is required")

    hop = payload.get("hop", 1)
    try:
        hop = int(hop)
    except (TypeError, ValueError):
        raise TunnelError(ErrorCode.MALFORMED_EXCHANGE, "hop must be an integer") from None
    if hop < 1:
        # The bound was upper-only, so hop=0 and hop=-1 were accepted. `hop` is
        # 1-based and names a real position in a chain, so anything below 1 is
        # not merely odd — it is a caller awarding itself extra hops. A chain
        # opening at hop=-1 runs -1 → 0 → 1 → 2 before tripping max_hops=1,
        # three forwards where zero were allowed.
        raise TunnelError(ErrorCode.MALFORMED_EXCHANGE,
                          f"hop {hop} is below 1; hop is 1-based")
    if hop > max_hops:
        # A tunnel that forwards into another tunnel is an amplification loop.
        raise TunnelError(ErrorCode.HOP_LIMIT_EXCEEDED,
                          f"hop {hop} exceeds max_hops {max_hops}")

    target = payload.get("target") or {}
    if not isinstance(target, Mapping):
        raise TunnelError(ErrorCode.MALFORMED_EXCHANGE, "target must be an object")
    if "url" in target:
        # Loud, not ignored: a URL on the wire means the far side is speaking a
        # different (unsafe) contract, and silently using the alias instead
        # would hide that.
        raise TunnelError(ErrorCode.MALFORMED_EXCHANGE,
                          "target.url is not accepted — the tunnel resolves an alias locally")
    alias = str(target.get("alias") or "")
    if not alias:
        raise TunnelError(ErrorCode.MALFORMED_EXCHANGE, "target.alias is required")

    raw = payload.get("request")
    if not isinstance(raw, Mapping):
        raise TunnelError(ErrorCode.MALFORMED_EXCHANGE, "request must be an object")
    body = _unb64(raw.get("body_b64"), what="request.body_b64")
    if max_body_bytes is not None and len(body) > max_body_bytes:
        raise TunnelError(ErrorCode.PAYLOAD_TOO_LARGE,
                          f"request body {len(body)}B exceeds {max_body_bytes}B")
    declared = raw.get("body_sha256")
    if declared and declared != body_digest(body):
        raise TunnelError(ErrorCode.DIGEST_MISMATCH,
                          f"body digest mismatch for exchange {exchange_id}")

    deadline = payload.get("deadline_ms")
    cert_context = payload.get("cert_context")
    return DecodedRequest(
        exchange_id=exchange_id,
        hop=hop,
        alias=alias,
        request=HttpRequestSpec(
            method=str(raw.get("method") or "GET").upper(),
            path=str(raw.get("path") or "/"),
            query=str(raw.get("query") or ""),
            headers=_header_pairs(raw.get("headers"), what="request.headers"),
            body=body,
        ),
        deadline_ms=int(deadline) if deadline is not None else None,
        cert_context=dict(cert_context) if isinstance(cert_context, Mapping) else None,
    )


def encode_response(
    *,
    exchange_id: str,
    response: HttpResponseSpec,
    elapsed_ms: int | None = None,
    max_body_bytes: int | None = None,
) -> dict:
    body = response.body or b""
    if max_body_bytes is not None and len(body) > max_body_bytes:
        raise TunnelError(ErrorCode.PAYLOAD_TOO_LARGE,
                          f"response body {len(body)}B exceeds {max_body_bytes}B")
    payload: dict[str, Any] = {
        "exchange_id": exchange_id,
        "response": {
            "status": int(response.status),
            "headers": [list(h) for h in response.headers],
            "body_b64": _b64(body),
            "body_sha256": body_digest(body),
        },
    }
    if elapsed_ms is not None:
        payload["elapsed_ms"] = int(elapsed_ms)
    return payload


def encode_error(*, exchange_id: str, code: str, detail: str = "") -> dict:
    """The error shape — mutually exclusive with `response`, so a caller can
    never read a success out of a failure by reaching for the wrong key."""
    if code not in ErrorCode.ALL:
        raise ValueError(f"unknown tunnel error code {code!r}")
    return {"exchange_id": exchange_id, "error": {"code": code, "detail": detail}}


def decode_response(
    payload: Mapping[str, Any],
    *,
    max_body_bytes: int | None = None,
) -> DecodedResponse:
    if not isinstance(payload, Mapping):
        raise TunnelError(ErrorCode.MALFORMED_EXCHANGE, "payload must be an object")
    exchange_id = str(payload.get("exchange_id") or "")
    error = payload.get("error")
    if error is not None:
        if not isinstance(error, Mapping):
            raise TunnelError(ErrorCode.MALFORMED_EXCHANGE, "error must be an object")
        if "response" in payload and payload["response"] is not None:
            raise TunnelError(ErrorCode.MALFORMED_EXCHANGE,
                              "response and error are mutually exclusive")
        return DecodedResponse(exchange_id=exchange_id, error=dict(error))

    raw = payload.get("response")
    if not isinstance(raw, Mapping):
        raise TunnelError(ErrorCode.MALFORMED_EXCHANGE, "response must be an object")
    body = _unb64(raw.get("body_b64"), what="response.body_b64")
    if max_body_bytes is not None and len(body) > max_body_bytes:
        raise TunnelError(ErrorCode.PAYLOAD_TOO_LARGE,
                          f"response body {len(body)}B exceeds {max_body_bytes}B")
    declared = raw.get("body_sha256")
    if declared and declared != body_digest(body):
        raise TunnelError(ErrorCode.DIGEST_MISMATCH,
                          f"body digest mismatch for exchange {exchange_id}")
    elapsed = payload.get("elapsed_ms")
    return DecodedResponse(
        exchange_id=exchange_id,
        response=HttpResponseSpec(
            status=int(raw.get("status") or 0),
            headers=_header_pairs(raw.get("headers"), what="response.headers"),
            body=body,
        ),
        elapsed_ms=int(elapsed) if elapsed is not None else None,
    )
