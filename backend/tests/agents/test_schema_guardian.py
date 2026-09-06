# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the deterministic XSD reuse-vs-create guardian (§7.4)."""
from pathlib import Path

import pytest

from app.agents.schema_guardian import analyze_reuse

CANON = "http://example.org/network/schema/"
VARIANT = "http://www.example.net/network/schema/"
NETC = "http://npci.org/netc/schema/"


def _schema(ns: str, body: str) -> str:
    return (f'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="{ns}">'
            f"{body}</xs:schema>")


_PAYTYPE = ('<xs:complexType name="PayType"><xs:sequence>'
            '<xs:element name="amount" type="xs:string"/></xs:sequence></xs:complexType>')
SIBLING = _schema(CANON, '<xs:element name="ReqTransfer" type="PayType"/>' + _PAYTYPE)


def test_redundant_elements_recommend_reuse():
    v = analyze_reuse(SIBLING, [("ReqTransfer.xsd", SIBLING)])
    assert v.decision == "reuse"
    assert not v.escalate
    assert all(f.status == "redundant" for f in v.findings)


def test_novel_element_recommends_new():
    proposed = _schema(CANON, '<xs:element name="RespHbt" type="xs:string"/>')
    v = analyze_reuse(proposed, [("ReqTransfer.xsd", SIBLING)])
    assert v.decision == "new"
    assert not v.escalate


def test_mixed_recommends_extend():
    proposed = _schema(CANON, '<xs:element name="ReqTransfer" type="PayType"/>' + _PAYTYPE
                       + '<xs:element name="RespHbt" type="xs:string"/>')
    v = analyze_reuse(proposed, [("ReqTransfer.xsd", SIBLING)])
    assert v.decision == "extend"
    assert {f.status for f in v.findings} == {"redundant", "novel"}


def test_conflicting_definition_escalates():
    # same element name, DIFFERENT structure → must not silently fork
    proposed = _schema(CANON, '<xs:element name="ReqTransfer" type="DifferentType"/>')
    v = analyze_reuse(proposed, [("ReqTransfer.xsd", SIBLING)])
    assert v.decision == "escalate"
    assert v.escalate
    assert any(f.status == "conflict" for f in v.findings)


@pytest.fixture
def upi_pack(monkeypatch):
    """The spelling-variant behaviour is UPI pack data (`schema_namespaces`),
    so pin the pack rather than assume it."""
    monkeypatch.setenv("DOMAIN_PACK", str(
        Path(__file__).resolve().parents[2] / "app" / "packs" / "network" / "network.yaml"))
    from app.core.domain import registry
    registry._load.cache_clear()
    yield
    registry._load.cache_clear()


def test_namespace_variant_is_flagged(upi_pack):
    proposed = _schema(VARIANT, '<xs:element name="RespHbt" type="xs:string"/>')
    v = analyze_reuse(proposed, [("ReqTransfer.xsd", SIBLING)])
    assert any("namespace spelling variant" in r for r in v.reasons)


def test_cross_namespace_collision_escalates():
    # ReqTransfer defined identically but under a DIFFERENT real namespace (NETC vs the network)
    proposed = _schema(NETC, '<xs:element name="ReqTransfer" type="PayType"/>' + _PAYTYPE)
    v = analyze_reuse(proposed, [("ReqTransfer.xsd", SIBLING)])
    assert v.decision == "escalate"
    assert any("identity boundary" in r for r in v.reasons)


def test_no_siblings_means_new():
    v = analyze_reuse(SIBLING, [])
    assert v.decision == "new"
    assert not v.escalate
