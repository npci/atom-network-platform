# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""The partner platform still speaks the pre-rename wire vocabulary.

Migration 0139 renamed tables, columns and stored row values. It could not
rename what the PARTNER sends, and that platform deploys separately: the agreed
rollout is (1) partner accepts both spellings, (2) authority stops emitting the
old ones, (3) partner switches what it sends. Steps 2 and 3 have not happened.

The HTTP headers were given dual-accept treatment for this reason. Two payload
values were not, and unlike a header mismatch — which fails loudly with
`missing_envelope_headers` — both of these fail SILENTLY:

* `direction` falls through a catch-all to AUTHORITY_TO_PARTNER, so an
  unrecognised legacy value is not rejected but INVERTED; and
* `requested_action_from_*` simply reads as absent.

Each test below fails against the pre-fix code with a wrong value rather than an
error, which is what made them worth pinning.
"""
from __future__ import annotations

import pytest

from app.models.phase_c import CertDirection, requested_action_from


# The spellings the unchanged partner platform emits today.
LEGACY_DIRECTIONS = [
    ("partner_to_npci", CertDirection.PARTNER_TO_AUTHORITY),
    ("npci_to_partner", CertDirection.AUTHORITY_TO_PARTNER),
]
CURRENT_DIRECTIONS = [
    ("partner_to_authority", CertDirection.PARTNER_TO_AUTHORITY),
    ("authority_to_partner", CertDirection.AUTHORITY_TO_PARTNER),
]


@pytest.mark.parametrize("normaliser_path", [
    "app.a2a_common.authority_handlers",
    "app.api.a2a_cert_handlers",
])
@pytest.mark.parametrize("wire,expected", LEGACY_DIRECTIONS + CURRENT_DIRECTIONS)
def test_direction_normalisers_accept_both_vocabularies(normaliser_path, wire, expected):
    """Both copies of `_normalize_direction` must agree, on both spellings.

    There are two independent implementations of this function. A fix applied to
    one and not the other is invisible: whichever handler the message happens to
    reach decides whether the result is recorded correctly.
    """
    import importlib

    module = importlib.import_module(normaliser_path)
    assert module._normalize_direction(wire) is expected, (
        f"{normaliser_path} mapped {wire!r} to the wrong side — a partner-initiated "
        "result attributed to the authority is silent data corruption, not an error")


def test_direction_case_is_ignored():
    """The normalisers lower-case first; pin that for the legacy spellings too."""
    from app.a2a_common.authority_handlers import _normalize_direction

    assert _normalize_direction("PARTNER_TO_NPCI") is CertDirection.PARTNER_TO_AUTHORITY


def test_unknown_direction_still_falls_back_to_authority_to_partner():
    """The catch-all is preserved deliberately — this is not a behaviour change.

    Pinned so that a future attempt to make the fallback strict is a conscious
    decision: an inbound message carrying junk should still record a result
    rather than 500, and the legacy aliases above are what stop the fallback
    firing for values we actually understand.
    """
    from app.a2a_common.authority_handlers import _normalize_direction

    assert _normalize_direction("something_else") is CertDirection.AUTHORITY_TO_PARTNER
    assert _normalize_direction("") is CertDirection.AUTHORITY_TO_PARTNER
    assert _normalize_direction(None) is CertDirection.AUTHORITY_TO_PARTNER


@pytest.mark.parametrize("key", [
    "requested_action_from_authority",
    "requested_action_from_npci",
])
def test_requested_action_read_under_either_key(key):
    """0139 renamed the COLUMN; nothing rewrites JSON payload keys.

    So `blockers.payload` and `a2a_messages.payload` keep the old spelling on
    historical rows permanently — the partner deploying is not enough to retire
    this read.
    """
    assert requested_action_from({key: "raise a waiver"}) == "raise a waiver"


def test_requested_action_prefers_the_current_key_and_tolerates_absence():
    assert requested_action_from({
        "requested_action_from_authority": "new",
        "requested_action_from_npci": "old",
    }) == "new"
    assert requested_action_from({}) is None
    assert requested_action_from({"requested_action_from_authority": ""}) is None
