# >>> a2a-core vendored header >>>
# GENERATED FILE — DO NOT EDIT HERE. Your change will be overwritten.
#
# Canonical source: packages/a2a-core/a2a_common/integration_allowlist.py
# Edit there, then run: scripts/ci/sync-a2a-core.sh
#
# This is security-critical A2A wire code shared byte-for-byte across services
# that cannot import each other (separate Docker build contexts). A fix applied
# to one copy and forgotten on the others is the failure mode this guards.
# <<< a2a-core vendored header <<<
# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Integration-testing tunnel — alias allowlist. PURE: no I/O, no settings.

THE LOAD-BEARING RULE (ITA §2): *the caller never supplies a URL; it supplies
an ALIAS, which the receiving side resolves against its own allowlist.*

An HTTP tunnel between two security domains is SSRF-as-a-service unless the
target is constrained. Whoever reaches the ingress can otherwise make the far
platform issue requests to anything IT can reach — internal services, database
admin ports, cloud metadata. Weakening this to "validate the URL the caller
sent" reintroduces exactly that hole, so the resolution function below has no
parameter that could carry one.

`dev-only` does NOT retire this: a dev network still reaches internal services,
and metadata endpoints answer the same there as anywhere.

STARTUP VALIDATION, NOT LAZY (ITA §2, security skill §4.3). A malformed
allowlist must stop the app rather than start it permissive — a tunnel that
boots with an unparsable policy and then fails open is worse than one that does
not boot. `load_allowlist` is strict on purpose and every rejection names the
alias and the reason.

Vendored byte-for-byte into both platforms; nothing here may import a service's
settings or logger.

WHY THE IMPORT BELOW IS `app.a2a_common` AND NOT RELATIVE: this file is a
GENERATED artifact whose canonical copy lives outside any importable package.
It runs only from its vendored location, and BOTH platforms vendor it to
`app/a2a_common/`, so the absolute path resolves identically on both sides —
which is the property that lets the bytes stay identical. A relative import
would also work, but the explicit path documents the layout the vendoring
depends on.
"""
from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import unquote

from app.a2a_common.integration_contract import ErrorCode, TunnelError

__all__ = [
    "AllowlistError", "AliasTarget",
    "load_allowlist", "resolve_alias", "build_target_url",
]

_ALLOWED_SCHEMES = ("http", "https")


class AllowlistError(ValueError):
    """The allowlist policy itself is invalid. Raised at STARTUP, never at
    request time — a bad policy is an operator error to fix before serving."""


@dataclass(frozen=True)
class AliasTarget:
    alias: str
    scheme: str
    host: str
    port: int
    # Fail-closed: an alias with no prefixes reaches nothing. Declaring the
    # paths a target may receive is the tunnel's command allowlist.
    path_prefixes: tuple[str, ...]
    strip_headers: tuple[str, ...] = ()

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"


def _reject_metadata_host(alias: str, host: str) -> None:
    """Refuse link-local / metadata addresses even when an operator lists one.

    169.254.169.254 is the address ITA §2 names as the thing an SSRF reaches
    for, and there is no legitimate reason for a test tunnel to target the
    link-local range. Refusing it in the POLICY means a typo or a copied config
    cannot open it; the allowlist remains the control, this is the floor under
    it.
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # a hostname; DNS-time checks belong to the egress, not the policy
    if ip.is_link_local or ip.is_multicast or ip.is_reserved:
        raise AllowlistError(
            f"alias {alias!r}: host {host} is link-local/reserved "
            "(cloud metadata lives here) and may not be a tunnel target"
        )


def _one(alias: str, raw: Mapping[str, Any]) -> AliasTarget:
    if not isinstance(raw, Mapping):
        raise AllowlistError(f"alias {alias!r}: entry must be an object")

    scheme = str(raw.get("scheme") or "").strip().lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise AllowlistError(
            f"alias {alias!r}: scheme must be one of {_ALLOWED_SCHEMES}, got {scheme!r}")

    host = str(raw.get("host") or "").strip()
    if not host:
        raise AllowlistError(f"alias {alias!r}: host is required")
    if "/" in host or ":" in host:
        raise AllowlistError(
            f"alias {alias!r}: host must be a bare hostname or IP "
            f"(no scheme, port or path), got {host!r}")
    _reject_metadata_host(alias, host)

    port_raw = raw.get("port", 443 if scheme == "https" else 80)
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        raise AllowlistError(f"alias {alias!r}: port must be an integer, got {port_raw!r}") from None
    if not 1 <= port <= 65535:
        raise AllowlistError(f"alias {alias!r}: port {port} out of range")

    prefixes_raw = raw.get("path_prefixes")
    if prefixes_raw is None:
        raise AllowlistError(
            f"alias {alias!r}: path_prefixes is required — an alias that declares "
            "no paths would otherwise reach the target's entire surface")
    if isinstance(prefixes_raw, str) or not isinstance(prefixes_raw, (list, tuple)):
        raise AllowlistError(f"alias {alias!r}: path_prefixes must be a list of strings")
    prefixes = tuple(str(p) for p in prefixes_raw)
    if not prefixes:
        raise AllowlistError(f"alias {alias!r}: path_prefixes must not be empty (fail closed)")
    for prefix in prefixes:
        if not prefix.startswith("/"):
            raise AllowlistError(f"alias {alias!r}: path prefix {prefix!r} must start with '/'")

    strip_raw = raw.get("strip_headers") or ()
    if isinstance(strip_raw, str) or not isinstance(strip_raw, (list, tuple)):
        raise AllowlistError(f"alias {alias!r}: strip_headers must be a list of strings")

    return AliasTarget(
        alias=alias, scheme=scheme, host=host, port=port,
        path_prefixes=prefixes,
        strip_headers=tuple(str(h).strip().lower() for h in strip_raw if str(h).strip()),
    )


def load_allowlist(raw: str | Mapping[str, Any] | None) -> dict[str, AliasTarget]:
    """Parse and validate the allowlist policy. Call at STARTUP.

    Accepts a JSON string (how it arrives from the environment) or an already
    parsed mapping. An empty/absent policy yields an empty allowlist, which is
    valid and reaches nothing — the tunnel is off by default, and "no aliases"
    is the correct configuration for that.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise AllowlistError(f"allowlist is not valid JSON: {exc}") from None
    else:
        parsed = raw
    if not isinstance(parsed, Mapping):
        raise AllowlistError("allowlist must be a JSON object keyed by alias")

    out: dict[str, AliasTarget] = {}
    for alias, entry in parsed.items():
        alias_str = str(alias).strip()
        if not alias_str:
            raise AllowlistError("allowlist contains an empty alias name")
        out[alias_str] = _one(alias_str, entry)
    return out


def _prefix_admits(candidate: str, prefix: str) -> bool:
    """Does `prefix` admit `candidate`, matching on PATH SEGMENTS?

    A bare `startswith` is wrong here and the failure is quiet: a prefix of
    "/api/health" would also admit "/api/healthcheck" and "/api/health-admin",
    so an operator scoping an alias to one path silently exposes every sibling
    sharing those leading characters. Matching on the segment boundary means
    the declared prefix selects the set of paths the operator actually wrote.

    A trailing slash on the prefix is optional and does not change the meaning;
    "/api/sim" and "/api/sim/" both admit "/api/sim" and "/api/sim/execute",
    and neither admits "/api/simulator-admin".
    """
    base = prefix.rstrip("/")
    return candidate == base or candidate.startswith(base + "/")


def resolve_alias(
    allowlist: Mapping[str, AliasTarget],
    alias: str,
    path: str,
) -> AliasTarget:
    """Resolve an alias + path, or raise the tunnel error the far side sees.

    Note what this function CANNOT do: there is no parameter through which a
    caller-supplied URL could enter. An unknown alias is a hard rejection with
    no fallback — a default target would make every typo silently reach
    something.
    """
    target = allowlist.get(alias)
    if target is None:
        raise TunnelError(ErrorCode.UNKNOWN_ALIAS, f"alias {alias!r} is not in the allowlist")
    candidate = path or "/"
    if not candidate.startswith("/"):
        candidate = "/" + candidate
    # Control characters never appear in a legitimate path, and a bare CR/LF
    # would be a request-splitting attempt against whatever the egress speaks
    # to. httpx happens to reject these too, but an incidental backstop in the
    # HTTP client is not the control — this is (ITA §2).
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
        raise TunnelError(ErrorCode.PATH_NOT_ALLOWED,
                          f"path {path!r} contains a control character")
    # Reject traversal before prefix matching: "/allowed/../admin" starts with
    # an allowed prefix but does not stay inside it. Check the PERCENT-DECODED
    # form as well, because "%2e%2e" is a traversal segment to any target that
    # decodes before routing (nginx, Apache, most Go/Java servers) even though
    # it is inert to Starlette. The URL we ultimately build still uses the
    # verbatim path — decoding here is for the CHECK only, never for the call.
    for form in (candidate, unquote(candidate)):
        if ".." in form.split("/"):
            raise TunnelError(ErrorCode.PATH_NOT_ALLOWED,
                              f"path {path!r} contains a traversal segment")
    if not any(_prefix_admits(candidate, p) for p in target.path_prefixes):
        raise TunnelError(
            ErrorCode.PATH_NOT_ALLOWED,
            f"path {candidate!r} is outside the prefixes allowed for {alias!r}")
    return target


def build_target_url(target: AliasTarget, path: str, query: str = "") -> str:
    """The concrete URL the egress calls.

    The query string is appended VERBATIM — not parsed, re-encoded or
    reordered. Contract selection rides on it (`?pack=CHG-4711%403`), and
    normalising it would present as "certified against baseline" rather than as
    an error (ITA §12.5).
    """
    candidate = path or "/"
    if not candidate.startswith("/"):
        candidate = "/" + candidate
    url = f"{target.origin}{candidate}"
    if query:
        url = f"{url}?{query}"
    return url
