# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""ITA I-0 — the tunnel contract and allowlist. Pure; no containers, no wire.

This is the verification bar `INTEGRATION_TESTING_AGENT_PLAN.md` §9 states for
I-0: round-trip a request with duplicate headers, a binary body, an empty body
and no body; hop-by-hop headers dropped; unknown alias rejected; path-prefix
enforcement; a malformed allowlist fails startup; and **the query string
survives byte-identically** — duplicate keys, an encoded `%40`, and an
unrecognised parameter — because contract selection rides on it (§12.5).

These functions are the parts worth testing hard: they decide whether the
tunnel is TRANSPARENT (contract) and whether it is SAFE (allowlist).
"""
from __future__ import annotations

import base64
import json

import pytest

from app.a2a_common.integration_allowlist import (
    AllowlistError, build_target_url, load_allowlist, resolve_alias,
)
from app.a2a_common.integration_contract import (
    ErrorCode, HttpRequestSpec, HttpResponseSpec, TunnelError, body_digest,
    classify_headers, decode_request, decode_response, encode_error,
    encode_request, encode_response,
)

EX = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
ALIAS = "external_api"

POLICY = {
    ALIAS: {"scheme": "http", "host": "api.internal", "port": 8080,
            "path_prefixes": ["/v1/"], "strip_headers": []},
    "callback": {"scheme": "https", "host": "sim.internal",
                 "path_prefixes": ["/cb/", "/hooks/"], "strip_headers": ["cookie"]},
}


def _round_trip(request: HttpRequestSpec, **kw):
    return decode_request(encode_request(exchange_id=EX, alias=ALIAS,
                                         request=request, **kw))


# ── round-trip fidelity ──────────────────────────────────────────────────────

def test_binary_body_survives_byte_identically():
    """A test tunnel that corrupts a binary payload is worse than no tunnel."""
    body = bytes(range(256)) * 4
    out = _round_trip(HttpRequestSpec(method="POST", path="/v1/pay", body=body))
    assert out.request.body == body


def test_empty_body_and_absent_body_both_round_trip():
    for spec in (HttpRequestSpec(method="POST", path="/v1/pay", body=b""),
                 HttpRequestSpec(method="GET", path="/v1/ping")):
        assert _round_trip(spec).request.body == b""


def test_duplicate_headers_are_preserved_in_order():
    """HTTP permits repeats; a dict would silently drop all but one."""
    headers = (("Set-Cookie", "a=1"), ("Set-Cookie", "b=2"), ("Via", "1.1 x"))
    out = _round_trip(HttpRequestSpec(method="GET", path="/v1/x", headers=headers))
    assert out.request.headers == headers


def test_header_map_is_refused_not_silently_flattened():
    payload = encode_request(exchange_id=EX, alias=ALIAS,
                             request=HttpRequestSpec("GET", "/v1/x"))
    payload["request"]["headers"] = {"set-cookie": "a=1"}     # a map loses repeats
    with pytest.raises(TunnelError) as exc:
        decode_request(payload)
    assert exc.value.code == ErrorCode.MALFORMED_EXCHANGE


def test_method_and_path_round_trip():
    out = _round_trip(HttpRequestSpec(method="delete", path="/v1/thing/7"))
    assert (out.request.method, out.request.path) == ("DELETE", "/v1/thing/7")


def test_cert_context_rides_along_and_is_optional():
    ctx = {"cflow_id": "CFLOW-ab12", "cert_attempt": 2, "test_case_id": "TC-7",
           "initiator": "bank", "npci_mode": "simulator", "partner_mode": "application"}
    assert _round_trip(HttpRequestSpec("GET", "/v1/x"), cert_context=ctx).cert_context == ctx
    assert _round_trip(HttpRequestSpec("GET", "/v1/x")).cert_context is None


def test_deadline_rides_along_and_is_optional():
    assert _round_trip(HttpRequestSpec("GET", "/v1/x"), deadline_ms=20000).deadline_ms == 20000
    assert _round_trip(HttpRequestSpec("GET", "/v1/x")).deadline_ms is None


# ── THE query-string guarantee (§12.5) ───────────────────────────────────────

@pytest.mark.parametrize("query", [
    "pack=CHG-4711%403",                    # the %40 every pack_ref contains
    "a=1&a=2",                              # duplicate keys
    "z=last&a=first",                       # order that a sort would change
    "pack=CHG-1%403&unknown_param=keepme",  # a parameter the tunnel never heard of
    "flag",                                 # valueless key
    "b=%20%2F%3F&c=+plus",                  # encodings a re-encoder would rewrite
    "",                                     # absent
])
def test_query_string_survives_byte_identically(query):
    """Contract selection rides on `?pack=`. A tunnel that normalises the query
    presents as 'certified against baseline' — a FALSE PASS, not an error."""
    out = _round_trip(HttpRequestSpec("GET", "/v1/x", query=query))
    assert out.request.query == query


def test_target_url_appends_the_query_verbatim():
    target = resolve_alias(load_allowlist(POLICY), ALIAS, "/v1/pay")
    url = build_target_url(target, "/v1/pay", "pack=CHG-4711%403&a=1&a=2")
    assert url == "http://api.internal:8080/v1/pay?pack=CHG-4711%403&a=1&a=2"


def test_target_url_omits_the_separator_when_there_is_no_query():
    target = resolve_alias(load_allowlist(POLICY), ALIAS, "/v1/pay")
    assert build_target_url(target, "/v1/pay", "") == "http://api.internal:8080/v1/pay"


# ── header classification (§5.3) ─────────────────────────────────────────────

def test_hop_by_hop_headers_are_dropped():
    headers = [("Connection", "keep-alive"), ("Keep-Alive", "timeout=5"),
               ("TE", "trailers"), ("Trailer", "Expires"),
               ("Transfer-Encoding", "chunked"), ("Upgrade", "h2c"),
               ("Proxy-Authorization", "Basic x"), ("X-Keep", "yes")]
    forwarded, dropped = classify_headers(headers)
    assert forwarded == (("X-Keep", "yes"),)
    assert len(dropped) == 7


def test_host_and_content_length_are_dropped_for_recomputation():
    forwarded, dropped = classify_headers(
        [("Host", "old.example"), ("Content-Length", "12"), ("Accept", "*/*")])
    assert forwarded == (("Accept", "*/*"),)
    assert {n.lower() for n, _ in dropped} == {"host", "content-length"}


def test_authorization_and_cookie_are_forwarded_by_default():
    """Deliberate (§5.3): a tunnel that strips credentials cannot test an
    authenticated API. Transparency wins; strip_headers is the escape hatch."""
    forwarded, dropped = classify_headers(
        [("Authorization", "Bearer t"), ("Cookie", "s=1")])
    assert len(forwarded) == 2 and dropped == ()


def test_strip_headers_removes_named_headers_case_insensitively():
    forwarded, dropped = classify_headers(
        [("Authorization", "Bearer t"), ("Cookie", "s=1")], strip=["COOKIE"])
    assert forwarded == (("Authorization", "Bearer t"),)
    assert dropped == (("Cookie", "s=1"),)


def test_classification_is_case_insensitive_and_keeps_repeats():
    forwarded, _ = classify_headers(
        [("cOnNeCtIoN", "x"), ("Set-Cookie", "a"), ("set-cookie", "b")])
    assert forwarded == (("Set-Cookie", "a"), ("set-cookie", "b"))


# ── digest + size + hop guards ───────────────────────────────────────────────

def test_digest_mismatch_is_detected_before_replay():
    payload = encode_request(exchange_id=EX, alias=ALIAS,
                             request=HttpRequestSpec("POST", "/v1/pay", body=b"real"))
    payload["request"]["body_b64"] = base64.b64encode(b"tampered").decode()
    with pytest.raises(TunnelError) as exc:
        decode_request(payload)
    assert exc.value.code == ErrorCode.DIGEST_MISMATCH


def test_digest_matches_the_documented_algorithm():
    payload = encode_request(exchange_id=EX, alias=ALIAS,
                             request=HttpRequestSpec("POST", "/v1/pay", body=b"abc"))
    assert payload["request"]["body_sha256"] == body_digest(b"abc")


def test_invalid_base64_is_malformed_not_a_crash():
    payload = encode_request(exchange_id=EX, alias=ALIAS,
                             request=HttpRequestSpec("POST", "/v1/x", body=b"x"))
    payload["request"]["body_b64"] = "!!!not base64!!!"
    with pytest.raises(TunnelError) as exc:
        decode_request(payload)
    assert exc.value.code == ErrorCode.MALFORMED_EXCHANGE


def test_oversize_body_is_rejected_on_encode_and_decode():
    big = HttpRequestSpec("POST", "/v1/x", body=b"x" * 100)
    with pytest.raises(TunnelError) as exc:
        encode_request(exchange_id=EX, alias=ALIAS, request=big, max_body_bytes=10)
    assert exc.value.code == ErrorCode.PAYLOAD_TOO_LARGE
    payload = encode_request(exchange_id=EX, alias=ALIAS, request=big)
    with pytest.raises(TunnelError) as exc:
        decode_request(payload, max_body_bytes=10)
    assert exc.value.code == ErrorCode.PAYLOAD_TOO_LARGE


def test_hop_limit_blocks_a_tunnel_into_a_tunnel():
    payload = encode_request(exchange_id=EX, alias=ALIAS,
                             request=HttpRequestSpec("GET", "/v1/x"), hop=2)
    with pytest.raises(TunnelError) as exc:
        decode_request(payload, max_hops=1)
    assert exc.value.code == ErrorCode.HOP_LIMIT_EXCEEDED
    assert decode_request(payload, max_hops=2).hop == 2


def test_a_hop_below_one_is_malformed_not_generous():
    """F-10: the bound was upper-only, so hop=0 and hop=-1 were accepted.

    Found by the partner platform against the vendored copy. `hop` is 1-based,
    so a value below 1 lets a caller award itself extra forwards: a chain
    opening at hop=-1 runs -1 -> 0 -> 1 -> 2 before tripping max_hops=1.
    """
    for bad in (0, -1):
        payload = encode_request(exchange_id=EX, alias=ALIAS,
                                 request=HttpRequestSpec("GET", "/v1/x"))
        payload["hop"] = bad
        with pytest.raises(TunnelError) as exc:
            decode_request(payload, max_hops=1)
        assert exc.value.code == ErrorCode.MALFORMED_EXCHANGE
    # The legitimate lower bound still passes.
    ok = encode_request(exchange_id=EX, alias=ALIAS,
                        request=HttpRequestSpec("GET", "/v1/x"), hop=1)
    assert decode_request(ok, max_hops=1).hop == 1


def test_a_url_on_the_wire_is_refused_loudly():
    """The whole design says the caller sends an alias. A URL means the far
    side is speaking an unsafe contract — silently using the alias would hide
    that."""
    payload = encode_request(exchange_id=EX, alias=ALIAS,
                             request=HttpRequestSpec("GET", "/v1/x"))
    payload["target"]["url"] = "http://169.254.169.254/"
    with pytest.raises(TunnelError) as exc:
        decode_request(payload)
    assert exc.value.code == ErrorCode.MALFORMED_EXCHANGE


def test_encode_request_never_puts_a_url_on_the_wire():
    payload = encode_request(exchange_id=EX, alias=ALIAS,
                             request=HttpRequestSpec("GET", "/v1/x"))
    assert payload["target"] == {"alias": ALIAS}
    assert "url" not in json.dumps(payload)


# ── responses ────────────────────────────────────────────────────────────────

def test_response_round_trip_with_binary_body_and_repeats():
    body = bytes(range(200))
    payload = encode_response(exchange_id=EX, elapsed_ms=143,
                              response=HttpResponseSpec(
                                  200, (("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")), body))
    out = decode_response(payload)
    assert out.response.status == 200
    assert out.response.body == body
    assert out.response.headers == (("Set-Cookie", "a=1"), ("Set-Cookie", "b=2"))
    assert out.elapsed_ms == 143
    assert not out.failed


def test_error_response_is_structured_and_assertable():
    out = decode_response(encode_error(exchange_id=EX, code=ErrorCode.TARGET_TIMEOUT,
                                       detail="did not respond in 20000ms"))
    assert out.failed and out.error["code"] == ErrorCode.TARGET_TIMEOUT
    assert out.response is None


def test_response_and_error_are_mutually_exclusive():
    payload = encode_response(exchange_id=EX, response=HttpResponseSpec(200))
    payload["error"] = {"code": ErrorCode.TARGET_TIMEOUT, "detail": ""}
    with pytest.raises(TunnelError):
        decode_response(payload)


def test_unknown_error_code_is_refused_at_encode():
    with pytest.raises(ValueError):
        encode_error(exchange_id=EX, code="made_up_code")


def test_response_digest_mismatch_is_detected():
    payload = encode_response(exchange_id=EX, response=HttpResponseSpec(200, (), b"real"))
    payload["response"]["body_b64"] = base64.b64encode(b"tampered").decode()
    with pytest.raises(TunnelError) as exc:
        decode_response(payload)
    assert exc.value.code == ErrorCode.DIGEST_MISMATCH


# ── allowlist: resolution and enforcement ────────────────────────────────────

def test_unknown_alias_is_a_hard_rejection_with_no_fallback():
    with pytest.raises(TunnelError) as exc:
        resolve_alias(load_allowlist(POLICY), "nope", "/v1/x")
    assert exc.value.code == ErrorCode.UNKNOWN_ALIAS


def test_path_prefix_is_enforced():
    allow = load_allowlist(POLICY)
    assert resolve_alias(allow, ALIAS, "/v1/pay").host == "api.internal"
    with pytest.raises(TunnelError) as exc:
        resolve_alias(allow, ALIAS, "/admin/shutdown")
    assert exc.value.code == ErrorCode.PATH_NOT_ALLOWED


def test_traversal_cannot_escape_an_allowed_prefix():
    with pytest.raises(TunnelError) as exc:
        resolve_alias(load_allowlist(POLICY), ALIAS, "/v1/../admin")
    assert exc.value.code == ErrorCode.PATH_NOT_ALLOWED


def test_a_prefix_does_not_admit_its_character_siblings():
    """F-6: a bare startswith let "/api/health" admit "/api/healthcheck".

    Found by the partner platform against the vendored copy of this module
    during the two-sided run. The operator writing "/api/health" is selecting
    one path, not every sibling that happens to share those leading bytes, and
    the failure was silent — the request simply went through.
    """
    allow = load_allowlist({"h": {"scheme": "http", "host": "h",
                                  "path_prefixes": ["/api/health"]}})
    for allowed in ("/api/health", "/api/health/sub"):
        assert resolve_alias(allow, "h", allowed).alias == "h"
    for denied in ("/api/healthcheck", "/api/health-admin", "/api/healthzzz"):
        with pytest.raises(TunnelError) as exc:
            resolve_alias(allow, "h", denied)
        assert exc.value.code == ErrorCode.PATH_NOT_ALLOWED


def test_a_trailing_slash_on_a_prefix_changes_nothing():
    """Both spellings must mean the same set, or the fix just moves the trap."""
    for prefix in ("/api/sim", "/api/sim/"):
        allow = load_allowlist({"s": {"scheme": "http", "host": "h",
                                      "path_prefixes": [prefix]}})
        for allowed in ("/api/sim", "/api/sim/execute"):
            assert resolve_alias(allow, "s", allowed).alias == "s"
        with pytest.raises(TunnelError):
            resolve_alias(allow, "s", "/api/simulator-admin")


def test_percent_encoded_traversal_is_rejected():
    """F-7: "%2e%2e" is inert to Starlette but a traversal to nginx/Apache/Go.

    The guard is documented as the control, so it must not depend on which
    server happens to sit at the other end of the tunnel.
    """
    allow = load_allowlist(POLICY)
    for path in ("/v1/%2e%2e/admin", "/v1/%2E%2E/admin"):
        with pytest.raises(TunnelError) as exc:
            resolve_alias(allow, ALIAS, path)
        assert exc.value.code == ErrorCode.PATH_NOT_ALLOWED


def test_control_characters_are_rejected_by_the_allowlist_not_by_httpx():
    """CRLF was stopped only at call time by httpx raising InvalidURL.

    An incidental backstop in the HTTP client is not a control: swap the client
    and the guarantee disappears with nothing failing loudly.
    """
    allow = load_allowlist(POLICY)
    with pytest.raises(TunnelError) as exc:
        resolve_alias(allow, ALIAS, "/v1/x\r\nX-Injected: 1")
    assert exc.value.code == ErrorCode.PATH_NOT_ALLOWED


def test_any_of_several_prefixes_matches():
    allow = load_allowlist(POLICY)
    for path in ("/cb/x", "/hooks/y"):
        assert resolve_alias(allow, "callback", path).alias == "callback"


def test_scheme_default_ports():
    allow = load_allowlist({"a": {"scheme": "https", "host": "h", "path_prefixes": ["/"]},
                            "b": {"scheme": "http", "host": "h", "path_prefixes": ["/"]}})
    assert (allow["a"].port, allow["b"].port) == (443, 80)


def test_per_alias_strip_headers_reach_classification():
    target = resolve_alias(load_allowlist(POLICY), "callback", "/cb/x")
    forwarded, dropped = classify_headers(
        [("Cookie", "s=1"), ("Authorization", "Bearer t")], strip=target.strip_headers)
    assert forwarded == (("Authorization", "Bearer t"),)
    assert dropped == (("Cookie", "s=1"),)


# ── allowlist: a malformed policy fails at STARTUP ───────────────────────────

@pytest.mark.parametrize("bad,reason", [
    ("{not json", "unparsable JSON"),
    ('["a"]', "not an object"),
    ('{"a": {"scheme": "ftp", "host": "h", "path_prefixes": ["/"]}}', "scheme"),
    ('{"a": {"scheme": "http", "path_prefixes": ["/"]}}', "missing host"),
    ('{"a": {"scheme": "http", "host": "http://h/x", "path_prefixes": ["/"]}}', "host with scheme"),
    ('{"a": {"scheme": "http", "host": "h", "port": 0, "path_prefixes": ["/"]}}', "port range"),
    ('{"a": {"scheme": "http", "host": "h", "port": "eighty", "path_prefixes": ["/"]}}', "port type"),
    ('{"a": {"scheme": "http", "host": "h"}}', "path_prefixes absent"),
    ('{"a": {"scheme": "http", "host": "h", "path_prefixes": []}}', "path_prefixes empty"),
    ('{"a": {"scheme": "http", "host": "h", "path_prefixes": "/v1/"}}', "prefixes not a list"),
    ('{"a": {"scheme": "http", "host": "h", "path_prefixes": ["v1"]}}', "prefix without /"),
    ('{"a": {"scheme": "http", "host": "h", "path_prefixes": ["/"], "strip_headers": "cookie"}}',
     "strip_headers not a list"),
])
def test_malformed_allowlist_is_rejected(bad, reason):
    with pytest.raises(AllowlistError):
        load_allowlist(bad)


def test_link_local_metadata_address_is_refused_even_if_listed():
    """169.254.169.254 is the address the SSRF threat model names. A typo or a
    copied config must not be able to open it."""
    with pytest.raises(AllowlistError) as exc:
        load_allowlist('{"meta": {"scheme": "http", "host": "169.254.169.254", '
                       '"path_prefixes": ["/"]}}')
    assert "link-local" in str(exc.value)


def test_absent_policy_is_valid_and_reaches_nothing():
    """'Enabled but not configured' must be expressible: no aliases, so every
    resolution is a hard rejection."""
    for empty in (None, "", "  ", {}):
        allow = load_allowlist(empty)
        assert allow == {}
        with pytest.raises(TunnelError) as exc:
            resolve_alias(allow, ALIAS, "/v1/x")
        assert exc.value.code == ErrorCode.UNKNOWN_ALIAS


def test_settings_refuse_to_start_on_a_malformed_allowlist():
    """The startup gate itself: a policy the tunnel cannot parse stops the app
    rather than starting it permissive."""
    from app.core.config import Settings

    with pytest.raises(Exception) as exc:
        Settings(secret_key="x" * 40, integration_testing_enabled=True,
                 integration_testing_allowlist="{not json")
    assert "INTEGRATION_TESTING_ALLOWLIST" in str(exc.value)


def test_settings_start_cleanly_when_the_tunnel_is_disabled():
    """A malformed policy nothing will read must not block an operator who has
    not turned the tunnel on."""
    from app.core.config import Settings

    Settings(secret_key="x" * 40, integration_testing_enabled=False,
             integration_testing_allowlist="{not json")


def test_tunnel_is_off_by_default(monkeypatch):
    """The DECLARED default must be off, whatever this host happens to set.

    Constructing Settings() plainly read the developer's backend/.env, so on any
    host running the tunnel this asserted the operator's choice rather than the
    field default and failed. Note _env_file=None would NOT be enough: in
    pydantic-settings an ambient OS env var outranks an explicitly-passed env
    file, so the variable has to actually be cleared.
    """
    from app.core.config import Settings

    monkeypatch.delenv("INTEGRATION_TESTING_ENABLED", raising=False)
    assert Settings(secret_key="x" * 40,
                    _env_file=None).integration_testing_enabled is False
