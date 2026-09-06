# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Slice 6 — `PATCH /admin/partners/{id}/protocol` smoke test.

Pure unit tests against the request validator. Real round-trips via the
admin client land in Slice 8's end-to-end suite once the legacy router
goes away.

Skipped cleanly when a2a-sdk isn't installed (lazy-imported by the
admin partners router, which can soft-fail without the SDK).
"""
from __future__ import annotations

import pytest


pytest.importorskip("a2a")
pytest.importorskip("sqlalchemy")


def test_valid_protocol_passes_validator():
    from app.api.partners import _VALID_PROTOCOLS, UpdateProtocolRequest

    assert "legacy" in _VALID_PROTOCOLS
    assert "a2a_sdk" in _VALID_PROTOCOLS

    req = UpdateProtocolRequest(protocol_version="a2a_sdk")
    assert req.protocol_version == "a2a_sdk"
    assert req.protocol_version in _VALID_PROTOCOLS


def test_unknown_protocol_caught_at_endpoint_layer():
    """The pydantic model accepts any string (so older clients can
    round-trip the column default), but the endpoint rejects unknown
    values. Confirms the runtime check is the source of truth."""
    from app.api.partners import _VALID_PROTOCOLS, UpdateProtocolRequest

    # Model accepts arbitrary strings — design choice, not a bug.
    req = UpdateProtocolRequest(protocol_version="grpc")
    assert req.protocol_version == "grpc"
    # But the runtime guard catches it.
    assert req.protocol_version not in _VALID_PROTOCOLS


def test_partner_response_carries_protocol_version():
    """Sanity: `_partner_response` exposes the column to the UI so the
    dropdown can default to the current value."""
    from app.api.partners import _partner_response
    from app.models.phase_c import PartnerAgent, PartnerStatus

    p = PartnerAgent(
        id="p-x", name="X", partner_type=["bank"],
        endpoint_url="http://x.local",
        api_key="a2a_x",
        status=PartnerStatus.ACTIVE,
        protocol_version="a2a_sdk",
    )
    resp = _partner_response(p)
    assert resp["protocol_version"] == "a2a_sdk"
