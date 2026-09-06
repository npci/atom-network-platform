# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for app.core.ssrf_guard.

The important cases are the **bypass** ones. The check this replaces was a tuple
of literal ``http://`` prefixes, so every assertion below that uses ``https://``
against a private target would have passed the old code — those are the tests
that actually pin the fix.

No network access: every hostname used either is a literal IP (no resolution) or
is resolved through a monkeypatched ``socket.getaddrinfo``.
"""
from __future__ import annotations

import pytest

from app.core.ssrf_guard import (
    SsrfBlocked,
    _sanitize_url_for_log,
    check_outbound_url,
    classify_url,
    parse_allowlist,
    pin_url,
)


# ── the scheme bypass: the actual defect ─────────────────────────────────────

@pytest.mark.parametrize("url", [
    # Cloud metadata — hands out IAM credentials. The headline bypass.
    "https://169.254.169.254/latest/meta-data/",
    "http://169.254.169.254/latest/meta-data/",
    # Loopback, both schemes.
    "https://127.0.0.1:8000/admin",
    "http://127.0.0.1:8000/admin",
    # RFC-1918, both schemes.
    "https://10.0.0.5/internal",
    "http://10.0.0.5/internal",
    "https://192.168.1.1/router",
    "https://172.16.0.1/",
    # IPv6 loopback — the prefix list had no IPv6 entries at all.
    "https://[::1]:8000/",
    "http://[::1]:8000/",
    # Integer notation for 127.0.0.1 — ipaddress accepts it, prefixes never did.
    "https://2130706433/",
    # IPv4-mapped IPv6.
    "https://[::ffff:10.0.0.1]/",
    # Unspecified / reserved.
    "https://0.0.0.0/",
])
def test_private_targets_are_blocked_regardless_of_scheme(url):
    verdict = classify_url(url)
    assert not verdict.allowed, f"{url} should be blocked, got: {verdict.reason}"


@pytest.mark.parametrize("host,expected", [
    # inet_aton spellings of 127.0.0.1. ipaddress.ip_address(str) rejects all of
    # these, and glibc's resolver accepts them — so whether they were caught used
    # to depend on the OS. Decoded explicitly now.
    ("2130706433", "127.0.0.1"),
    ("0x7f000001", "127.0.0.1"),
    ("127.1", "127.0.0.1"),
    ("127.0.1", "127.0.0.1"),
    ("0177.0.0.1", "127.0.0.1"),
    ("0", "0.0.0.0"),
    # And a private one for good measure: 10.0.0.5.
    ("167772165", "10.0.0.5"),
])
def test_legacy_numeric_ip_spellings_are_blocked(host, expected):
    verdict = classify_url(f"https://{host}/")
    assert not verdict.allowed, f"{host} ({expected}) should be blocked"
    assert expected in verdict.reason, verdict.reason


@pytest.mark.parametrize("host", [
    # Must NOT be misread as numeric: these are ordinary hostnames.
    "example.com",
    "partner-1.bank.example.com",
    "1partner.example.com",
    "12345.example.com",
])
def test_hostnames_are_not_misparsed_as_numeric(host, monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda h, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    assert classify_url(f"https://{host}/").allowed, f"{host} must resolve normally"


@pytest.mark.parametrize("url", [
    "https://partner.bank.example.com/a2a",
    "http://partner.bank.example.com/a2a",
    "https://8.8.8.8/",
])
def test_public_targets_are_allowed(url, monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda host, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    verdict = classify_url(url)
    assert verdict.allowed, f"{url} should be allowed, got: {verdict.reason}"


# ── DNS-based evasion ────────────────────────────────────────────────────────

def test_hostname_resolving_to_link_local_is_blocked(monkeypatch):
    """The `nip.io` style trick: a public name pointing at 169.254.169.254."""
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda host, *a, **k: [(2, 1, 6, "", ("169.254.169.254", 0))],
    )
    verdict = classify_url("https://metadata.nip.io/latest/meta-data/")
    assert not verdict.allowed
    assert "link-local" in verdict.reason


def test_all_resolved_addresses_are_checked(monkeypatch):
    """A public A record must not mask a private one."""
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda host, *a, **k: [
            (2, 1, 6, "", ("93.184.216.34", 0)),   # public, listed first
            (2, 1, 6, "", ("10.0.0.5", 0)),        # private
        ],
    )
    verdict = classify_url("https://split-horizon.example.com/")
    assert not verdict.allowed, "a private address anywhere in the answer must block"


def test_unresolvable_host_is_refused(monkeypatch):
    """Fail CLOSED on DNS failure.

    A name that does not resolve at check time could resolve to a private
    address moments later (DNS rebinding), and the guard cannot vet an address
    it never saw — so an unresolvable host is refused by default.
    """
    def boom(*a, **k):
        raise OSError("name resolution failed")
    monkeypatch.setattr("socket.getaddrinfo", boom)
    verdict = classify_url("https://does-not-exist.invalid/")
    assert not verdict.allowed
    assert "did not resolve" in verdict.reason


def test_unresolvable_host_allowed_when_opted_out(monkeypatch):
    """`block_on_resolution_failure=False` restores the pre-hardening behaviour.

    The escape hatch for a deployment whose resolver is flaky enough that the
    fail-closed default causes more outages than the rebinding risk justifies.
    """
    def boom(*a, **k):
        raise OSError("name resolution failed")
    monkeypatch.setattr("socket.getaddrinfo", boom)
    verdict = classify_url("https://does-not-exist.invalid/",
                           block_on_resolution_failure=False)
    assert verdict.allowed
    assert "did not resolve" in verdict.reason


# ── non-http schemes ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "gopher://127.0.0.1:70/",
    "ftp://internal/",
])
def test_non_http_schemes_are_blocked(url):
    assert not classify_url(url).allowed


def test_url_without_host_is_blocked():
    assert not classify_url("https://").allowed


def test_empty_url_is_allowed():
    """Nothing to fetch — the caller treats empty as 'not configured'."""
    assert classify_url("").allowed
    assert classify_url("   ").allowed


# ── allowlist: what makes enforcement survivable ─────────────────────────────

def test_allowlist_permits_an_approved_internal_host():
    allow = parse_allowlist("10.20.30.40, partner.internal")
    assert classify_url("https://10.20.30.40/a2a", allowlist=allow).allowed
    assert classify_url("https://partner.internal/a2a", allowlist=allow).allowed


def test_allowlist_is_case_insensitive_and_trims():
    allow = parse_allowlist("  Partner.Internal  ,, ")
    assert allow == {"partner.internal"}
    assert classify_url("https://PARTNER.INTERNAL/x", allowlist=allow).allowed


def test_allowlist_does_not_leak_to_other_hosts():
    allow = parse_allowlist("10.20.30.40")
    assert not classify_url("https://10.20.30.41/a2a", allowlist=allow).allowed


def test_parse_allowlist_handles_empty():
    assert parse_allowlist(None) == frozenset()
    assert parse_allowlist("") == frozenset()


# ── modes: observe must not break anything ───────────────────────────────────

BLOCKED_URL = "https://169.254.169.254/latest/meta-data/"


def test_observe_mode_does_not_raise():
    """The no-regression guarantee: deploying in observe mode changes nothing."""
    verdict = check_outbound_url(BLOCKED_URL, mode="observe")
    assert not verdict.allowed          # it reports the problem ...
    # ... but it did not raise, so the caller proceeds exactly as before.


def test_observe_mode_logs_what_would_be_blocked(caplog):
    with caplog.at_level("WARNING", logger="app.core.ssrf_guard"):
        check_outbound_url(BLOCKED_URL, mode="observe")
    assert "WOULD BE REFUSED" in caplog.text
    assert "SSRF_ALLOWED_INTERNAL_HOSTS" in caplog.text


def test_enforce_mode_raises():
    with pytest.raises(SsrfBlocked) as ei:
        check_outbound_url(BLOCKED_URL, mode="enforce")
    assert "link-local" in ei.value.reason


def test_enforce_mode_allows_a_public_target(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda host, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    check_outbound_url("https://partner.example.com/a2a", mode="enforce")   # no raise


def test_enforce_mode_respects_the_allowlist():
    allow = parse_allowlist("169.254.169.254")
    check_outbound_url(BLOCKED_URL, mode="enforce", allowlist=allow)        # no raise


def test_off_mode_skips_everything():
    verdict = check_outbound_url(BLOCKED_URL, mode="off")
    assert verdict.allowed
    assert "disabled" in verdict.reason


@pytest.mark.parametrize("mode", ["OBSERVE", " observe "])
def test_observe_is_recognised_case_and_space_insensitively(mode):
    verdict = check_outbound_url(BLOCKED_URL, mode=mode)
    assert not verdict.allowed      # reported
    # and crucially, no exception was raised


@pytest.mark.parametrize("mode", ["", None])
def test_empty_mode_uses_the_enforce_default(mode):
    """An unset setting must get the secure default, not a permissive one.

    This inverts an earlier assertion deliberately: while the guard was being
    rolled out, an unknown mode fell back to `observe` so a typo could not cause
    an outage. Now that enforcement is the default, the same typo must fail
    SAFE instead — refusing is recoverable, silently permitting SSRF is not.
    """
    with pytest.raises(SsrfBlocked):
        check_outbound_url(BLOCKED_URL, mode=mode)


def test_enforce_is_only_triggered_by_the_exact_word():
    with pytest.raises(SsrfBlocked):
        check_outbound_url(BLOCKED_URL, mode="ENFORCE")     # case-insensitive
    with pytest.raises(SsrfBlocked):
        check_outbound_url(BLOCKED_URL, mode=" enforce ")   # whitespace-tolerant


# ── credentials-in-URL must not confuse host parsing ─────────────────────────

def test_userinfo_in_url_does_not_hide_the_real_host():
    """`https://public.example.com@127.0.0.1/` really targets 127.0.0.1."""
    verdict = classify_url("https://public.example.com@127.0.0.1/")
    assert not verdict.allowed
    assert "loopback" in verdict.reason


def test_trailing_dot_fqdn_is_normalised():
    allow = parse_allowlist("partner.internal")
    assert classify_url("https://partner.internal./x", allowlist=allow).allowed


# ── enforce is now the DEFAULT ────────────────────────────────────────────────

def test_default_mode_enforces():
    """The closure change: calling with no mode must REFUSE, not merely log."""
    with pytest.raises(SsrfBlocked):
        check_outbound_url(BLOCKED_URL)


@pytest.mark.parametrize("mode", ["nonsense", "enforcing", "true", "yes", "1"])
def test_unknown_mode_fails_safe_by_enforcing(mode):
    """A typo in SSRF_GUARD_MODE must not silently permit SSRF."""
    with pytest.raises(SsrfBlocked):
        check_outbound_url(BLOCKED_URL, mode=mode)


@pytest.mark.parametrize("mode", ["observe", "OBSERVE", " observe "])
def test_observe_still_available_for_staged_rollout(mode):
    verdict = check_outbound_url(BLOCKED_URL, mode=mode)
    assert not verdict.allowed          # reported, not raised


# ── two tiers: private is opt-in-able, loopback/link-local never is ───────────

@pytest.mark.parametrize("url", [
    "https://10.0.0.5/internal",
    "https://172.16.0.1/",
    "https://192.168.1.1/router",
])
def test_private_ranges_can_be_allowed_wholesale(url):
    """A deployment with many internal partners can permit RFC-1918."""
    assert classify_url(url, allow_private=True).allowed
    check_outbound_url(url, mode="enforce", allow_private=True)      # no raise


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/admin",
    "https://169.254.169.254/latest/meta-data/",
    "https://[::1]/",
    "https://0.0.0.0/",
    "https://224.0.0.1/",
])
def test_never_legitimate_ranges_stay_blocked_even_with_allow_private(url):
    """allow_private must NOT re-open loopback or the metadata range."""
    assert not classify_url(url, allow_private=True).allowed
    with pytest.raises(SsrfBlocked):
        check_outbound_url(url, mode="enforce", allow_private=True)


# ── DNS rebinding: the pin ───────────────────────────────────────────────────

def test_pin_url_substitutes_the_resolved_address(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo",
                        lambda h, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    pinned = pin_url("http://partner.example.com/.well-known/agent-card.json")
    assert pinned is not None
    assert pinned.url == "http://93.184.216.34/.well-known/agent-card.json"
    assert pinned.host_header == "partner.example.com"
    assert pinned.headers == {"Host": "partner.example.com"}


def test_pin_url_preserves_the_port(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo",
                        lambda h, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    pinned = pin_url("http://partner.example.com:8443/x")
    assert pinned.url == "http://93.184.216.34:8443/x"
    assert pinned.host_header == "partner.example.com:8443"


def test_pin_url_brackets_ipv6(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo",
                        lambda h, *a, **k: [(23, 1, 6, "", ("2606:2800:220:1::1", 0, 0, 0))])
    pinned = pin_url("http://partner.example.com/x")
    assert pinned.url.startswith("http://[2606:2800:220:1::1]/")


def test_pin_url_blocks_a_rebinding_answer(monkeypatch):
    """The attack this closes: a public-looking name answering with loopback."""
    monkeypatch.setattr("socket.getaddrinfo",
                        lambda h, *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))])
    with pytest.raises(SsrfBlocked) as ei:
        pin_url("http://rebind.example.com/")
    assert "loopback" in ei.value.reason


def test_pin_url_checks_every_answer_not_just_the_pinned_one(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda h, *a, **k: [
        (2, 1, 6, "", ("93.184.216.34", 0)),
        (2, 1, 6, "", ("127.0.0.1", 0)),
    ])
    with pytest.raises(SsrfBlocked):
        pin_url("http://split.example.com/")


def test_pin_url_returns_none_for_an_ip_literal():
    """Nothing to rebind — the caller uses the original URL."""
    assert pin_url("http://93.184.216.34/x") is None


def test_pin_url_returns_none_for_allowlisted_host():
    allow = parse_allowlist("partner.internal")
    assert pin_url("http://partner.internal/x", allowlist=allow) is None


def test_pin_url_returns_none_when_off():
    assert pin_url("http://anything/x", mode="off") is None


def test_pin_url_observe_mode_does_not_raise(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo",
                        lambda h, *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))])
    assert pin_url("http://rebind.example.com/", mode="observe") is None


# ── log sanitisation (CWE-532) ───────────────────────────────────────────────
#
# These pin the bug that shipped in the first hardening pass: the helper built
# its redacted URL with `ParseResult._replace(username=None, password=None)`,
# but `username`/`password` are read-only PROPERTIES derived from `netloc`, not
# fields of the named tuple. Every call therefore raised TypeError, which a bare
# `except Exception` swallowed — so all eight call sites logged the constant
# string "<unparseable url>" and the guard's diagnostics were useless. Nothing
# tested it, which is why it went unnoticed.

def test_sanitize_strips_query_but_keeps_the_locatable_part():
    out = _sanitize_url_for_log("https://api.example.com/v1/x?api_key=SECRET123")
    assert "SECRET123" not in out
    assert "api.example.com" in out          # still identifies the target
    assert "/v1/x" in out
    assert out != "<unparseable url>"        # the regression this pins


def test_sanitize_strips_embedded_credentials():
    out = _sanitize_url_for_log("https://user:pa55w0rd@api.example.com/path")
    assert "pa55w0rd" not in out
    assert "user" not in out
    assert "api.example.com" in out


def test_sanitize_strips_fragment_and_reports_what_was_dropped():
    out = _sanitize_url_for_log("https://h.example.com/p?t=SEKRIT#anchorval")
    # Assert against the URL part only: the "[query/fragment redacted]" suffix
    # legitimately contains the words "query"/"fragment", so a naive substring
    # check on those would false-positive.
    url_part = out.split(" [", 1)[0]
    assert "SEKRIT" not in url_part
    assert "anchorval" not in url_part
    assert url_part == "https://h.example.com/p"
    assert "redacted" in out                 # tells the reader it was trimmed


def test_sanitize_passes_through_a_clean_url_unchanged():
    url = "https://plain.example.com/path"
    assert _sanitize_url_for_log(url) == url


def test_sanitize_preserves_port_and_brackets_ipv6():
    assert ":8443" in _sanitize_url_for_log("https://h.example.com:8443/p")
    assert "[::1]" in _sanitize_url_for_log("http://[::1]:8080/p")


# ── credentials hidden in `path` because the URL has no `//` ─────────────────
# Found on a second, adversarial pass over the Checkmarx "Filtering Sensitive
# Logs" findings, by asking what happens when the URL is MALFORMED rather than
# assuming the sanitizer's rebuild always sees an authority.
#
# `urlparse` only populates `netloc` when the URL contains `//`. Without it the
# entire authority lands in `path`:
#
#     urlparse("https:user:pw@host/x") -> netloc='', path='user:pw@host/x'
#
# `_sanitize_url_for_log` rebuilt the URL from `netloc` and, when `netloc` was
# empty, returned `path` VERBATIM — so the function whose one job is removing
# credentials handed the password straight to the log line. It was also silent
# about it: `parsed.username`/`parsed.password` derive from `netloc` too, so both
# were None and no "[credentials redacted]" marker was appended.
#
# Reachable from untrusted input: `_validate_endpoint_url` in api/partners.py
# rejects on `parsed.scheme != "https"`, and urlparse reports scheme 'https' for
# `https:user:pw@host`, so the value clears validation and is then logged by the
# guard on the refusal path.
@pytest.mark.parametrize("url,secret", [
    ("https:svc:SchemeRelPw1@partner.example.com/a2a",  "SchemeRelPw1"),
    ("https:/svc:SchemeRelPw2@partner.example.com/a2a", "SchemeRelPw2"),
    ("http:admin:SchemeRelPw3@internal.example/api",    "SchemeRelPw3"),
    ("user:SchemeRelPw4@evil.example.com/path",         "SchemeRelPw4"),
])
def test_sanitize_strips_credentials_when_url_has_no_double_slash(url, secret):
    out = _sanitize_url_for_log(url)
    assert secret not in out, f"credential leaked verbatim: {out!r}"
    # The reader must also be TOLD something was withheld, otherwise a trimmed
    # URL is indistinguishable from one that genuinely had no credentials.
    assert "credentials redacted" in out


def test_sanitize_keeps_an_at_sign_that_is_part_of_a_real_path():
    """`@` after a real host is a legal path character (`/users/@me`). Stripping
    it would corrupt ordinary URLs, so the credential strip must only apply when
    there is no authority at all."""
    url = "https://api.example.com/users/@me"
    assert _sanitize_url_for_log(url) == url
