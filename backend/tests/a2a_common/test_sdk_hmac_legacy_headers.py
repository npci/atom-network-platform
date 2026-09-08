# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""The inbound HMAC dual-accept must survive the middleware, not just exist.

`hmac_signer.verify()` falls back from `X-Auth-*` to the superseded `X-NPCI-*`
spelling so a counterparty that has not deployed the rename still authenticates.
That fallback was correct and thoroughly tested — and completely dead, because
`sdk_hmac_middleware` (the ONLY verify call site on this side) pre-extracted a
three-key dict using the canonical names before calling it:

    envelope_headers = {HEADER_TIMESTAMP: headers.get("x-auth-timestamp"), ...}

For a request carrying only `X-NPCI-*` that dict held canonical KEYS with None
VALUES, and the legacy names were absent entirely, so `_h()` had nothing to fall
back to. Every legacy-signed request was rejected `missing_envelope_headers`.

Consequence, observed live: the partner platform signs `X-NPCI-*` and could not
return a single certification result — `POST /a2a-rpc/rpc` 401 — while the unit
tests for `verify()` stayed green, because they call `verify()` directly and
never go through the middleware's extract.

These tests therefore drive `verify()` through a header map shaped the way the
middleware builds it (lowercased keys, all headers present), which is the part
that was untested.
"""
from __future__ import annotations

import pytest

from app.a2a_common.hmac_signer import (
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    LEGACY_HEADER_NONCE,
    LEGACY_HEADER_SIGNATURE,
    LEGACY_HEADER_TIMESTAMP,
    sign,
    verify,
)

SECRET = "s" * 64
BODY = b'{"jsonrpc":"2.0","method":"message/send","id":1}'


def _asgi_style(headers: dict[str, str]) -> dict[str, str]:
    """Reproduce the middleware's lowercased header map."""
    return {k.lower(): v for k, v in headers.items()}


def _legacy_only(envelope: dict[str, str]) -> dict[str, str]:
    """The envelope as a counterparty that predates the rename sends it."""
    return {
        LEGACY_HEADER_TIMESTAMP: envelope[HEADER_TIMESTAMP],
        LEGACY_HEADER_NONCE:     envelope[HEADER_NONCE],
        LEGACY_HEADER_SIGNATURE: envelope[HEADER_SIGNATURE],
    }


def test_canonical_headers_verify():
    ok, err = verify(_asgi_style(sign(BODY, SECRET)), BODY, SECRET)
    assert ok, err


def test_legacy_only_headers_verify_through_a_middleware_shaped_map():
    """The regression. Fails pre-fix with `missing_envelope_headers`."""
    ok, err = verify(_asgi_style(_legacy_only(sign(BODY, SECRET))), BODY, SECRET)
    assert ok, f"legacy envelope rejected ({err}) — the partner platform "\
               "signs these and cannot return certification results"


def test_dual_emit_headers_verify():
    """Both spellings present, as this side's own signer emits today."""
    env = sign(BODY, SECRET)
    ok, err = verify(_asgi_style({**env, **_legacy_only(env)}), BODY, SECRET)
    assert ok, err


@pytest.mark.parametrize(
    "drop", [LEGACY_HEADER_TIMESTAMP, LEGACY_HEADER_NONCE, LEGACY_HEADER_SIGNATURE])
def test_incomplete_legacy_envelope_is_still_rejected(drop):
    """Dual-accept must not become "accept anything"."""
    legacy = _legacy_only(sign(BODY, SECRET))
    legacy.pop(drop)
    ok, err = verify(_asgi_style(legacy), BODY, SECRET)
    assert not ok and err == "missing_envelope_headers"


def test_tampered_body_still_fails_under_legacy_headers():
    """The fallback changes WHICH header is read, never whether the MAC holds."""
    ok, err = verify(_asgi_style(_legacy_only(sign(BODY, SECRET))), BODY + b"x", SECRET)
    assert not ok and err == "signature_mismatch"


def test_wrong_secret_still_fails_under_legacy_headers():
    ok, err = verify(_asgi_style(_legacy_only(sign(BODY, SECRET))), BODY, "x" * 64)
    assert not ok and err == "signature_mismatch"


def test_middleware_passes_the_full_header_map_not_an_extract():
    """Guards the specific regression at its source.

    A future refactor that re-introduces a three-key extract would make every
    test above pass (they call `verify` directly) while breaking production
    again, so assert on the middleware's own source.
    """
    import inspect

    from app.a2a_common import sdk_hmac_middleware as mw

    src = inspect.getsource(mw)
    assert "envelope_headers = headers" in src, (
        "sdk_hmac_middleware must hand verify() the whole header map; a filtered "
        "extract silently disables the legacy-spelling inbound fallback")
