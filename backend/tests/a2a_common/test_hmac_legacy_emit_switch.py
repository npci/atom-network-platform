# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""`A2A_EMIT_LEGACY_HEADERS` controls the OUTBOUND half of the rename only.

The last step of the header rollout — "stop sending the legacy spellings" —
depends on an
operational fact no repository can know: whether every counterparty this
deployment sends to already reads `X-Auth-*`. So it is a switch, not a commit.
A fleet whose partners are all updated turns it off; one still facing an older
partner leaves it on and is unaffected by the upgrade.

Two properties matter and are pinned here:

* the switch NEVER affects inbound acceptance. Continuing to accept a
  superseded spelling costs nothing and is exactly what makes the rollout
  order-independent; making that switchable would hand someone a way to break
  the boundary from one side.
* the signature is computed over (timestamp, nonce, body) and never over header
  NAMES, so emitting fewer headers cannot change whether a signature verifies.
"""
from __future__ import annotations

import importlib

import pytest

from app.a2a_common.hmac_signer import (
    HEADER_NONCE, HEADER_SIGNATURE, HEADER_TIMESTAMP,
    LEGACY_HEADER_NONCE, LEGACY_HEADER_SIGNATURE, LEGACY_HEADER_TIMESTAMP,
)

BODY = b'{"jsonrpc":"2.0","method":"message/send","id":1}'
SECRET = "s" * 64
# Asserted through the module constants, not the literals: the legacy prefix is
# retargetable in one place (`hmac_signer.LEGACY_HEADER_PREFIX`) for adopters
# who vendor this package, and these tests should follow it rather than pin it.
_LEGACY = (LEGACY_HEADER_TIMESTAMP, LEGACY_HEADER_NONCE, LEGACY_HEADER_SIGNATURE)
_CURRENT = (HEADER_TIMESTAMP, HEADER_NONCE, HEADER_SIGNATURE)


def _signer(monkeypatch, emit_legacy: str):
    """Re-import the module so the env var is read at import time again."""
    monkeypatch.setenv("A2A_EMIT_LEGACY_HEADERS", emit_legacy)
    import app.a2a_common.hmac_signer as mod

    return importlib.reload(mod)


@pytest.fixture(autouse=True)
def _restore():
    """Leave the module as the rest of the suite expects it."""
    yield
    import app.a2a_common.hmac_signer as mod

    importlib.reload(mod)


def test_default_emits_both_spellings():
    """Unset means dual-emit — an upgrade must change nothing by itself."""
    import app.a2a_common.hmac_signer as mod

    env = mod.sign(BODY, SECRET)
    for name in _CURRENT + _LEGACY:
        assert name in env, f"{name} missing from the default envelope"


@pytest.mark.parametrize("off", ["0", "false", "False", "no", "NO"])
def test_switch_off_drops_only_the_legacy_half(monkeypatch, off):
    mod = _signer(monkeypatch, off)
    env = mod.sign(BODY, SECRET)
    for name in _CURRENT:
        assert name in env
    for name in _LEGACY:
        assert name not in env, f"{name} still emitted with the switch off"


@pytest.mark.parametrize("on", ["1", "true", "yes", "anything-else"])
def test_switch_on_keeps_dual_emit(monkeypatch, on):
    mod = _signer(monkeypatch, on)
    assert set(_LEGACY).issubset(mod.sign(BODY, SECRET))


def test_signature_is_identical_either_way(monkeypatch):
    """Header names are not signed, so the switch cannot change the MAC."""
    import app.a2a_common.hmac_signer as base

    both = base.sign(BODY, SECRET, ts=1_700_000_000, nonce="a" * 32)
    mod = _signer(monkeypatch, "0")
    only_new = mod.sign(BODY, SECRET, ts=1_700_000_000, nonce="a" * 32)
    assert both[HEADER_SIGNATURE] == only_new[HEADER_SIGNATURE]


def test_inbound_still_accepts_legacy_with_the_switch_off(monkeypatch):
    """The switch is OUTBOUND-only. A counterparty that has not deployed the
    rename must keep verifying against us however we choose to send."""
    mod = _signer(monkeypatch, "0")
    env = mod.sign(BODY, SECRET)
    legacy_only = {
        LEGACY_HEADER_TIMESTAMP: env[HEADER_TIMESTAMP],
        LEGACY_HEADER_NONCE:     env[HEADER_NONCE],
        LEGACY_HEADER_SIGNATURE: env[HEADER_SIGNATURE],
    }
    ok, err = mod.verify(legacy_only, BODY, SECRET)
    assert ok, f"legacy inbound rejected ({err}) — the switch must not gate acceptance"
