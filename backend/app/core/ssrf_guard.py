# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""SSRF guard for operator-supplied outbound URLs.

Replaces the string-prefix blocklist that used to live in
``app/api/partners.py``. That check had three defects, all confirmed by probing
it directly:

1. **Scheme bypass.** Every blocklist entry was a literal ``http://`` prefix, so
   simply switching to ``https://`` walked past all 21 of them.
   ``https://169.254.169.254/latest/meta-data/`` — the cloud metadata endpoint
   that hands out IAM credentials — was allowed in production.
2. **Notation gaps.** Prefix matching missed IPv6 (``http://[::1]``), integer
   notation (``http://2130706433`` = 127.0.0.1), and any hostname that merely
   *resolves* to a private address (``169.254.169.254.nip.io``).
3. **Disabled where it runs.** The whole check was skipped when ``ENVIRONMENT``
   was ``development``, ``uat`` or ``staging`` — and ``docker-compose.yml`` sets
   ``ENVIRONMENT: ${ENVIRONMENT:-uat}``, so on the default stack it did nothing
   at all. (``ENVIRONMENT`` is also a *different* variable from the ``app_env``
   used everywhere else, which is how the divergence went unnoticed.)

This module resolves the hostname and classifies the resulting IP addresses,
so the decision no longer depends on how the address was spelled.

Rollout is staged, because tightening this correctly is a BREAKING change:
``https://10.20.30.40`` and ``https://192.168.5.10:8443`` are accepted by the
current code, and internal-network partners are clearly intended — ``partner_verify()``
supports a per-partner CA and a global CA bundle precisely so internally-issued
certificates work. So:

* ``ssrf_guard_mode="enforce"`` (default) — private/loopback/link-local targets raise,
  unless the host is on the allowlist.
* ``ssrf_guard_mode="observe"`` — nothing is rejected; every URL that
  *would* be blocked is logged. Deploy here first and read the logs to discover
  which real partner endpoints are affected.
* ``ssrf_guard_mode="off"`` — no checking at all (escape hatch).

``ssrf_allowed_internal_hosts`` is the allowlist that makes enforcement
survivable: approved internal partners are named explicitly and keep working.

Public surface:
    SsrfBlocked                  — raised (enforce mode) when a URL is refused
    check_outbound_url(url, ...) — the guard; returns None, raises SsrfBlocked
    classify_url(url)            — (verdict, reason) with no side effects
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Schemes we are willing to fetch at all. `file://`, `gopher://` and friends are
# classic SSRF escalation vectors and have no business here.
ALLOWED_SCHEMES = ("http", "https")


def _sanitize_url_for_log(url: str) -> str:
    """Strip query parameters and credentials from a URL for safe logging.

    A URL may carry sensitive data in query parameters (API keys, tokens, PII)
    or embedded credentials (user:pass@host). This helper removes both before
    the URL reaches a log line.

    NOTE ON THE REBUILD: `username`/`password` are READ-ONLY properties derived
    from `netloc`, not fields of the named tuple, so they cannot be cleared via
    `ParseResult._replace(username=None, password=None)` — that raises
    `TypeError: Got unexpected field names`. Credentials are therefore removed
    by rebuilding `netloc` from `hostname` (+ `port`), which is the only form
    that actually drops the `user:pass@` prefix.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        # urlparse raises ValueError on a malformed IPv6 literal / bad port.
        return "<unparseable url>"

    # Rebuild netloc WITHOUT credentials. `hostname` is lower-cased and has any
    # `user:pass@` and brackets stripped, so re-bracket IPv6 literals.
    try:
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        # `.port` raises when the port is out of range or non-numeric.
        host, port = "", None
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port else host

    scheme = parsed.scheme or ""
    path = parsed.path or ""

    # CREDENTIALS CAN HIDE IN `path`, NOT ONLY IN `netloc`.
    #
    # `netloc` is only populated when the URL has a `//`. Without it, urlparse
    # puts the ENTIRE authority into `path`:
    #
    #     urlparse("https:user:pw@host/x")  -> netloc='', path='user:pw@host/x'
    #     urlparse("https:/user:pw@host/x") -> netloc='', path='/user:pw@host/x'
    #     urlparse("user:pw@host/x")        -> netloc='', path='pw@host/x'
    #
    # The rebuild below only strips credentials out of `netloc`, so in these
    # shapes the password was previously returned VERBATIM by the very function
    # whose job is to remove it. It also went unnoticed because
    # `parsed.username`/`parsed.password` are derived from `netloc` too, so they
    # were both None and no "[credentials redacted]" marker was added.
    #
    # This is reachable from untrusted input: `_validate_endpoint_url` in
    # api/partners.py gates on `parsed.scheme != "https"`, and
    # `urlparse("https:user:pw@host")` reports scheme 'https', so the shape
    # passes the scheme check and then gets logged on the refusal path.
    #
    # A userinfo segment is terminated by `@`, and a legitimate http(s) path can
    # legally contain `@` (e.g. `/users/@me`) — but only AFTER a real host has
    # been established in `netloc`. With no netloc there is no host, so anything
    # preceding an `@` here cannot be a meaningful path segment and is dropped.
    path_had_credentials = False
    if not netloc and "@" in path:
        path = path.rsplit("@", 1)[1]
        path_had_credentials = True

    redacted = f"{scheme}://{netloc}{path}" if netloc else (path or "<unparseable url>")

    # Report what was withheld so a log reader knows the URL was trimmed rather
    # than genuinely bare.
    dropped = []
    if parsed.username or parsed.password or path_had_credentials:
        dropped.append("credentials")
    if parsed.query:
        dropped.append("query")
    if parsed.fragment:
        dropped.append("fragment")
    if dropped:
        return f"{redacted} [{'/'.join(dropped)} redacted]"
    return redacted


class SsrfBlocked(Exception):
    """A URL was refused by the guard. Carries an operator-readable reason."""

    def __init__(self, url: str, reason: str):
        super().__init__(reason)
        self.url = url
        self.reason = reason


@dataclass(frozen=True)
class Verdict:
    """Outcome of classifying a URL."""
    allowed: bool
    reason: str
    # The addresses the hostname resolved to, for logging. Empty when the
    # hostname could not be resolved (which is NOT treated as a block — see
    # classify_url).
    addresses: tuple[str, ...] = ()


def _is_disallowed_ip(ip: ipaddress._BaseAddress,
                      *, allow_private: bool = False) -> str | None:
    """Return a reason string when `ip` is an SSRF-relevant target, else None.

    Two tiers, because they carry different risk and different legitimacy:

    * **Never legitimate** — loopback, link-local, multicast, unspecified,
      reserved. No partner is ever reachable at 127.0.0.1 or 169.254.169.254
      from this service's perspective, so these are refused unconditionally and
      ``allow_private`` does NOT re-enable them. Link-local in particular is the
      cloud metadata service, which hands out IAM credentials.
    * **Site-local (RFC-1918)** — 10/8, 172.16/12, 192.168/16, and the IPv6
      equivalents. These may be genuine: partners on an internal network are a
      supported deployment, which is why ``partner_verify()`` supports a
      per-partner CA. Refused by default, re-enabled per deployment with
      ``ssrf_allow_private_networks=true`` or per host via the allowlist.

    The IPv4-mapped IPv6 form is unwrapped first so ``::ffff:127.0.0.1`` cannot
    slip through as "just an IPv6 address".
    """
    # Unwrap IPv4-mapped IPv6 (::ffff:10.0.0.1) before classifying.
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped  # type: ignore[assignment]

    # ── tier 1: never legitimate, not overridable by allow_private ──
    if ip.is_loopback:
        return f"loopback address ({ip})"
    if ip.is_link_local:
        return f"link-local address ({ip}) — this range includes the cloud metadata service"
    if ip.is_multicast:
        return f"multicast address ({ip})"
    if ip.is_unspecified:
        return f"unspecified address ({ip})"

    # ── tier 2: site-local, legitimately used by internal partners ──
    if ip.is_private:
        # `is_private` is a superset that also covers the tier-1 ranges above;
        # by here those have been handled, so what remains is site-local space.
        if allow_private:
            return None
        return (f"private address ({ip}) — set SSRF_ALLOW_PRIVATE_NETWORKS=true or add the "
                "host to SSRF_ALLOWED_INTERNAL_HOSTS if this is an internal partner")
    if ip.is_reserved:
        return f"reserved address ({ip})"
    return None


def _coerce_numeric_host(host: str) -> ipaddress._BaseAddress | None:
    """Interpret the legacy numeric spellings of an IPv4 address.

    ``ipaddress.ip_address("2130706433")`` raises, but glibc's resolver happily
    turns that (and ``0x7f000001``, ``0177.0.0.1``, ``127.1``) into 127.0.0.1 —
    the classic ``inet_aton`` forms. Whether the OS resolver accepts them is
    PLATFORM-DEPENDENT: it fails on Windows and succeeds on Linux, which is
    where this service actually runs in Docker. So they are decoded here rather
    than left to resolution, otherwise the guard would pass on a developer's
    Windows box and be bypassable in the deployed container.

    Returns the address, or None when `host` is not a numeric form.
    """
    h = host.strip()
    if not h or any(c.isspace() for c in h):
        return None

    def _part(token: str) -> int | None:
        """Parse one octet: decimal, 0x-hex, or 0-prefixed octal."""
        try:
            if token.lower().startswith("0x"):
                return int(token, 16)
            if len(token) > 1 and token.startswith("0"):
                return int(token, 8)
            return int(token, 10)
        except ValueError:
            return None

    parts = h.split(".")
    if len(parts) > 4:
        return None
    values: list[int] = []
    for token in parts:
        v = _part(token)
        if v is None or v < 0:
            return None
        values.append(v)

    # inet_aton packing: the LAST part absorbs the remaining low-order bytes,
    # so "127.1" is 127.0.0.1 and a bare "2130706433" is the whole 32 bits.
    n = len(values)
    if n == 1:
        packed = values[0]
    elif n == 2:
        if values[0] > 0xFF or values[1] > 0xFFFFFF:
            return None
        packed = (values[0] << 24) | values[1]
    elif n == 3:
        if values[0] > 0xFF or values[1] > 0xFF or values[2] > 0xFFFF:
            return None
        packed = (values[0] << 24) | (values[1] << 16) | values[2]
    else:
        if any(v > 0xFF for v in values):
            return None
        packed = (values[0] << 24) | (values[1] << 16) | (values[2] << 8) | values[3]

    if packed > 0xFFFFFFFF:
        return None
    try:
        return ipaddress.ip_address(packed)
    except ValueError:
        return None


def _resolve(host: str) -> tuple[str, ...]:
    """Resolve `host` to every address it maps to.

    ALL results are checked, not just the first: a hostname with both a public
    and a private A record would otherwise pass on a lucky ordering.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return ()
    out: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in out:
            out.append(addr)
    return tuple(out)


def classify_url(url: str, *, allowlist: frozenset[str] | None = None,
                 allow_private: bool = False,
                 block_on_resolution_failure: bool = True) -> Verdict:
    """Decide whether `url` is safe to fetch. No logging, no raising — pure.

    Resolution failures are BLOCKED by default: a hostname that cannot be
    resolved at check time could resolve to a private address moments later
    (DNS rebinding or transient DNS manipulation).

    Pass ``block_on_resolution_failure=False`` to allow an unresolvable host
    instead. That is the pre-hardening behaviour, kept as an explicit opt-in
    for deployments where a flaky resolver caused more outages than the
    rebinding risk justifies — it is surfaced through
    ``settings.ssrf_block_on_resolution_failure`` so it is an auditable
    configuration choice rather than a hidden default.
    """
    if not url or not url.strip():
        return Verdict(True, "empty url — nothing to fetch")

    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return Verdict(False, f"scheme {scheme!r} is not allowed (only http/https)")

    host = parsed.hostname  # strips brackets from [::1] and any credentials
    if not host:
        return Verdict(False, "url has no host")

    host_l = host.lower().rstrip(".")   # trailing dot is a valid FQDN form

    # Allowlist wins: this is how an approved internal partner keeps working
    # once enforcement is on.
    if allowlist and host_l in allowlist:
        return Verdict(True, f"host {host_l!r} is explicitly allowlisted")

    # A literal IP needs no resolution. Dotted-quad and IPv6 first, then the
    # legacy inet_aton spellings (2130706433 / 0x7f000001 / 127.1) that the old
    # prefix check missed and that ipaddress.ip_address(str) also rejects.
    try:
        ip = ipaddress.ip_address(host_l)
    except ValueError:
        ip = _coerce_numeric_host(host_l)

    if ip is not None:
        reason = _is_disallowed_ip(ip, allow_private=allow_private)
        if reason:
            return Verdict(False, f"blocked: {reason}", (str(ip),))
        return Verdict(True, f"public address ({ip})", (str(ip),))

    addresses = _resolve(host_l)
    if not addresses:
        # Fail CLOSED by default: a hostname that cannot be resolved at check
        # time could resolve to a private address moments later (DNS rebinding
        # or transient DNS manipulation). Callers may opt out per-call via
        # `block_on_resolution_failure=False` when a transient DNS outage
        # should not block traffic.
        if not block_on_resolution_failure:
            return Verdict(
                True,
                f"host {host_l!r} did not resolve; allowed because "
                "block_on_resolution_failure is disabled",
            )
        return Verdict(False, f"host {host_l!r} did not resolve — refusing on resolution failure")

    for addr in addresses:
        try:
            resolved = ipaddress.ip_address(addr)
        except ValueError:
            continue
        reason = _is_disallowed_ip(resolved, allow_private=allow_private)
        if reason:
            return Verdict(
                False,
                f"blocked: host {host_l!r} resolves to a {reason}",
                addresses,
            )
    return Verdict(True, f"host {host_l!r} resolves to public address(es)", addresses)


def parse_allowlist(raw: str | None) -> frozenset[str]:
    """Parse a comma-separated allowlist into a normalised set of hostnames."""
    if not raw:
        return frozenset()
    return frozenset(
        h.strip().lower().rstrip(".")
        for h in raw.split(",")
        if h and h.strip()
    )


def check_outbound_url(
    url: str,
    *,
    mode: str = "enforce",
    allowlist: frozenset[str] | None = None,
    allow_private: bool = False,
    context: str = "outbound url",
    block_on_resolution_failure: bool = True,
) -> Verdict:
    """Guard an operator-supplied URL.

    Returns the :class:`Verdict` in every mode. Raises :class:`SsrfBlocked` only
    when ``mode == "enforce"`` and the verdict is a block, so switching the mode
    is the single lever that turns this from advisory into a control.

    ``block_on_resolution_failure`` is passed through to :func:`classify_url` —
    see that function for why it defaults to True.
    """
    normalised = (mode or "enforce").strip().lower()
    if normalised == "off":
        return Verdict(True, "ssrf guard disabled (mode=off)")

    verdict = classify_url(url, allowlist=allowlist, allow_private=allow_private,
                           block_on_resolution_failure=block_on_resolution_failure)
    if verdict.allowed:
        return verdict

    if normalised != "observe":
        # Anything that is not explicitly "observe" or "off" enforces. A typo in
        # the setting now fails SAFE (refuse) rather than silently permitting.
        if normalised != "enforce":
            logger.warning(
                "ssrf_guard: unrecognised mode %r — treating as 'enforce'. Valid values: "
                "enforce | observe | off.", mode)
        logger.warning("ssrf_guard[enforce] %s refused: %s (%s)", context, _sanitize_url_for_log(url), verdict.reason)
        raise SsrfBlocked(url, verdict.reason)

    # observe — report what enforcement WOULD have done, change nothing.
    logger.warning(
        "ssrf_guard[observe] %s WOULD BE REFUSED once enforcement is on: %s (%s). "
        "Allow it via SSRF_ALLOWED_INTERNAL_HOSTS if it is a legitimate internal partner.",
        context, _sanitize_url_for_log(url), verdict.reason,
    )
    return verdict


# ── DNS rebinding: resolve once, verify, then connect to that address ─────────

@dataclass(frozen=True)
class PinnedTarget:
    """A URL whose hostname has been resolved and vetted.

    ``url`` is the address-substituted URL to fetch, and ``headers`` carries the
    ``Host`` header that must accompany it so virtual hosting and TLS SNI still
    work. See :func:`pin_url` for why this exists.
    """
    url: str
    host_header: str
    address: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Host": self.host_header}


def pin_url(
    url: str,
    *,
    mode: str = "enforce",
    allowlist: frozenset[str] | None = None,
    allow_private: bool = False,
    context: str = "outbound url",
    block_on_resolution_failure: bool = True,
) -> PinnedTarget | None:
    """Vet `url` and return a target pinned to the address that was vetted.

    This closes the check-then-use (DNS rebinding) window. Validating a hostname
    and then handing the *hostname* to an HTTP client means the client performs
    its OWN resolution moments later — and an attacker controlling the DNS answer
    can return a public address for our check and 127.0.0.1 for the real fetch.

    So we resolve once, classify that specific address, then build a URL that
    targets the address literally. The original hostname travels in the ``Host``
    header, which keeps virtual-hosted servers working.

    Returns ``None`` when pinning is not applicable (already an IP literal, or
    the host is allowlisted, or mode is ``off``) — the caller then fetches the
    original URL, which is correct because there is no name left to rebind.

    Raises :class:`SsrfBlocked` in enforce mode when the resolved address is
    disallowed.

    Caveat, stated plainly: TLS certificate validation is performed against the
    address in the URL. For an https:// target whose certificate is issued to the
    hostname, pinning to a literal IP would fail verification. The caller must
    therefore only apply pinning where that is acceptable, or pass the hostname
    for SNI explicitly. `partner_test` uses it because a probe failure is
    reported to the operator rather than silently downgraded.
    """
    normalised = (mode or "enforce").strip().lower()
    if normalised == "off":
        return None

    parsed = urlparse((url or "").strip())
    host = parsed.hostname
    if not host:
        return None

    host_l = host.lower().rstrip(".")

    # Allowlisted hosts are exempt by policy; nothing to pin.
    if allowlist and host_l in allowlist:
        return None

    # Already a literal address: there is no DNS answer to rebind.
    try:
        ipaddress.ip_address(host_l)
        return None
    except ValueError:
        pass
    if _coerce_numeric_host(host_l) is not None:
        return None

    addresses = _resolve(host_l)
    if not addresses:
        # Fail CLOSED: an unresolvable hostname at pin time means we cannot
        # vet the target address. In enforce mode this must block rather than
        # letting the caller fall through to the original URL (DNS rebinding).
        msg = f"host {host_l!r} did not resolve — cannot pin; refusing"
        if not block_on_resolution_failure:
            # Opt-out: nothing to pin, so the caller fetches the original URL.
            logger.warning(
                "ssrf_guard %s: %s — allowed because block_on_resolution_failure "
                "is disabled", context, msg)
            return None
        if normalised == "observe":
            logger.warning(
                "ssrf_guard[observe] %s WOULD BE REFUSED once enforcement is on: %s (%s)",
                context, _sanitize_url_for_log(url), msg)
            return None
        logger.warning("ssrf_guard[enforce] %s refused: %s (%s)", context, _sanitize_url_for_log(url), msg)
        raise SsrfBlocked(url, msg)

    # Verify EVERY answer, then pin to the first. Checking only the one we pin to
    # would let a multi-record answer hide a private address behind a public one.
    for addr in addresses:
        try:
            resolved = ipaddress.ip_address(addr)
        except ValueError:
            continue
        reason = _is_disallowed_ip(resolved, allow_private=allow_private)
        if reason:
            msg = f"host {host_l!r} resolves to a {reason}"
            if normalised == "observe":
                logger.warning(
                    "ssrf_guard[observe] %s WOULD BE REFUSED once enforcement is on: %s (%s)",
                    context, _sanitize_url_for_log(url), msg)
                return None
            logger.warning("ssrf_guard[enforce] %s refused: %s (%s)", context, _sanitize_url_for_log(url), msg)
            raise SsrfBlocked(url, msg)

    pinned_addr = addresses[0]
    # Bracket IPv6 literals for the URL authority.
    literal = f"[{pinned_addr}]" if ":" in pinned_addr else pinned_addr
    netloc = f"{literal}:{parsed.port}" if parsed.port else literal
    pinned = parsed._replace(netloc=netloc).geturl()

    # Preserve the port in the Host header, as a browser would.
    host_header = f"{host}:{parsed.port}" if parsed.port else host
    logger.debug("ssrf_guard: pinned %s -> %s (Host: %s)", url, pinned, host_header)
    return PinnedTarget(url=pinned, host_header=host_header, address=pinned_addr)


# ── Cleartext transport policy (CWE-319) ─────────────────────────────────────
#
# SEPARATE CONCERN, DELIBERATELY. Everything above answers "could this URL be
# used to reach somewhere I shouldn't?" — for which public is fine and private
# is suspect. What follows answers the INVERTED question: "would using this URL
# put plaintext on a network I don't control?" — for which private is the only
# acceptable answer and public cleartext is the thing being refused.
#
#     SSRF policy       public → allow,        private → block
#     cleartext policy  private + http → allow, public + http → block
#
# They share the resolution machinery but must never share a switch. Loosening
# SSRF for a legitimate internal partner (SSRF_ALLOW_PRIVATE_NETWORKS) should
# not silently also permit cleartext to the open internet, which is exactly what
# would happen if this rode on the same mode flag.
#
# This rule has to hold on every service that sends on this wire, not just
# this one. A separate service cannot import this module — different dependency
# tree, different image — so each one carries its own equivalent implementation
# and the logic is intentionally duplicated. Keep them in step: a service that
# relaxes the rule locally reopens the hole for everyone.


class ClearTextBlocked(Exception):
    """Raised (enforce mode) when a URL would send cleartext off-host."""

    def __init__(self, url: str, reason: str):
        self.url = url
        self.reason = reason
        super().__init__(f"{url}: {reason}")


# Networks where cleartext is acceptable, enumerated EXPLICITLY.
#
# WHY NOT ``ipaddress.is_private``: that property means "not globally routable",
# a much broader set than "a network we control" — it returns True for the
# documentation/TEST-NET blocks (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24).
# A guard built on it would send certification traffic in the clear to those
# ranges. For a security decision the permitted set must be stated, not inherited.
#
# Notably absent: 169.254.0.0/16 + fe80::/10 (link-local, includes the cloud
# metadata service) and 100.64.0.0/10 (CGNAT, shared with other tenants).
_CLEARTEXT_OK_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),      # IPv4 loopback
    ipaddress.ip_network("::1/128"),          # IPv6 loopback
    ipaddress.ip_network("10.0.0.0/8"),       # RFC 1918
    ipaddress.ip_network("172.16.0.0/12"),    # RFC 1918
    ipaddress.ip_network("192.168.0.0/16"),   # RFC 1918
    ipaddress.ip_network("fc00::/7"),         # RFC 4193 unique-local
)


def _is_cleartext_ok_ip(ip: ipaddress._BaseAddress) -> bool:
    """True when plaintext to this address stays on a network we already trust."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped:
        ip = mapped
    for net in _CLEARTEXT_OK_NETWORKS:
        if ip.version != net.version:
            continue
        if ip in net:
            return True
    return False


def classify_cleartext(url: str, *, allowlist: frozenset[str] | None = None) -> Verdict:
    """Decide whether `url` may be used given the cleartext rule. Pure."""
    if not url or not url.strip():
        return Verdict(True, "empty url — nothing to send")

    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return Verdict(False, f"scheme {scheme!r} is not allowed (only http/https)")
    if scheme == "https":
        return Verdict(True, "https — encrypted in transit")

    host = parsed.hostname
    if not host:
        return Verdict(False, "url has no host")
    host_l = host.lower().rstrip(".")

    if allowlist and host_l in allowlist:
        return Verdict(True, f"host {host_l!r} is allowlisted for cleartext")

    try:
        ip = ipaddress.ip_address(host_l)
    except ValueError:
        ip = _coerce_numeric_host(host_l)

    if ip is not None:
        if _is_cleartext_ok_ip(ip):
            return Verdict(True, f"cleartext to local address ({ip})", (str(ip),))
        return Verdict(False,
                       f"cleartext to non-local address ({ip}) — use https:// or allowlist the host",
                       (str(ip),))

    addresses = _resolve(host_l)
    if not addresses:
        # Fail OPEN: an unresolvable host transmits nothing, so there is no
        # cleartext exposure to prevent and a DNS blip should not read as a
        # security refusal.
        return Verdict(True, f"host {host_l!r} did not resolve; no connection will be made")

    for addr in addresses:
        try:
            resolved = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if not _is_cleartext_ok_ip(resolved):
            return Verdict(
                False,
                f"cleartext to {host_l!r}, which resolves to non-local address {resolved} — "
                "use https:// or allowlist the host",
                addresses,
            )
    return Verdict(True, f"cleartext to {host_l!r} — resolves only to local addresses", addresses)


def check_cleartext_url(
    url: str,
    *,
    mode: str = "enforce",
    allowlist: frozenset[str] | None = None,
    context: str = "",
) -> Verdict:
    """Classify `url`, log, and raise :class:`ClearTextBlocked` in enforce mode.

    An unrecognised mode is treated as ``enforce`` so a typo fails safe.
    """
    normalised = (mode or "enforce").strip().lower()
    if normalised == "off":
        return Verdict(True, "cleartext policy disabled (mode=off)")
    if normalised not in ("enforce", "observe"):
        logger.warning(
            "cleartext_policy: unrecognised mode %r — treating as 'enforce'. "
            "Valid values: enforce | observe | off.", mode)
        normalised = "enforce"

    verdict = classify_cleartext(url, allowlist=allowlist)
    if verdict.allowed:
        return verdict

    label = f" {context}" if context else ""
    if normalised == "observe":
        logger.warning("cleartext_policy[observe]%s WOULD BE REFUSED: %s — %s",
                       label, _sanitize_url_for_log(url), verdict.reason)
        return verdict

    logger.warning("cleartext_policy[enforce]%s refused: %s — %s", label, _sanitize_url_for_log(url), verdict.reason)
    raise ClearTextBlocked(url, verdict.reason)
