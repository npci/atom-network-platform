# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Stage-order flow: v2 (BRD→XSD→TSD) is the default; legacy explicit v1 is preserved.

Locks in 'XSD before Tech Spec' as the default so a future change can't silently
flip it back. Pure functions — no DB.
"""
from app.api.agents import _status_flow, _next_status, _post_brd_status, _STATUS_FLOW_V2, _STATUS_FLOW_V1
from app.models.change_request import ChangeStatus as S


def test_default_and_null_are_v2():
    # the product decision: missing/None/0 → v2 (XSD before Tech Spec)
    assert _status_flow(None) is _STATUS_FLOW_V2
    assert _status_flow(0) is _STATUS_FLOW_V2
    assert _status_flow(2) is _STATUS_FLOW_V2


def test_explicit_v1_preserved_for_legacy_rows():
    assert _status_flow(1) is _STATUS_FLOW_V1


def test_v2_orders_xsd_before_tech_spec():
    flow = _STATUS_FLOW_V2
    assert flow.index(S.XSD) < flow.index(S.TECH_SPEC)


def test_post_brd_goes_to_xsd_by_default():
    assert _post_brd_status(None) == S.XSD
    assert _post_brd_status(2) == S.XSD
    assert _post_brd_status(1) == S.TECH_SPEC          # legacy only


def test_next_status_default_chain_is_brd_xsd_techspec_kit():
    assert _next_status(S.BRD, None) == S.XSD
    assert _next_status(S.XSD, None) == S.TECH_SPEC
    assert _next_status(S.TECH_SPEC, None) == S.PRODUCT_KIT
    assert _next_status(S.PRODUCT_KIT, None) == S.COMPLETED
    assert _next_status(S.COMPLETED, None) is None
    # legacy v1 chain still BRD→TSD→XSD
    assert _next_status(S.BRD, 1) == S.TECH_SPEC
    assert _next_status(S.TECH_SPEC, 1) == S.XSD
