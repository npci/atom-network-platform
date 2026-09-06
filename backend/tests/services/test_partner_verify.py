# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Unit tests for partner_verify — outbound-TLS verify resolution.

Precedence: per-partner ssl_verify → global partner_tls_verify (on/off);
then per-partner ca_cert_pem → global partner_ca_bundle → default trust.
Applied to both the Test-connectivity probe and the real A2A card fetch.
"""
from __future__ import annotations

import shutil
import ssl
import subprocess
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.a2a_client import partner_verify


def _p(ssl_verify=None, ca_cert_pem=None):
    return SimpleNamespace(id="p1", ssl_verify=ssl_verify, ca_cert_pem=ca_cert_pem)


@pytest.fixture
def verify_on(monkeypatch):
    monkeypatch.setattr(settings, "partner_tls_verify", True)
    monkeypatch.setattr(settings, "partner_ca_bundle", "")


def test_default_verifies(verify_on):
    assert partner_verify(_p()) is True


def test_per_partner_skip_overrides_global_on(verify_on):
    assert partner_verify(_p(ssl_verify=False)) is False


def test_global_off_inherited_and_overridable(monkeypatch):
    monkeypatch.setattr(settings, "partner_tls_verify", False)
    monkeypatch.setattr(settings, "partner_ca_bundle", "")
    assert partner_verify(_p()) is False                 # null → inherit global off
    assert partner_verify(_p(ssl_verify=True)) is True   # per-partner overrides


def test_global_ca_bundle_used_when_no_partner_cert(monkeypatch):
    monkeypatch.setattr(settings, "partner_tls_verify", True)
    monkeypatch.setattr(settings, "partner_ca_bundle", "/etc/ssl/npci-ca.pem")
    assert partner_verify(_p()) == "/etc/ssl/npci-ca.pem"


def test_partner_cert_beats_global_bundle(monkeypatch, tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("openssl not available")
    monkeypatch.setattr(settings, "partner_tls_verify", True)
    monkeypatch.setattr(settings, "partner_ca_bundle", "/etc/ssl/npci-ca.pem")
    pem = tmp_path / "t.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout",
                    "/dev/null", "-out", str(pem), "-days", "1", "-nodes",
                    "-subj", "/CN=test"], check=True, capture_output=True)
    v = partner_verify(_p(ca_cert_pem=pem.read_text()))
    assert isinstance(v, ssl.SSLContext)   # trusts the uploaded cert, not the bundle


def test_invalid_cert_falls_back_not_raise(verify_on):
    # A bad paste must not hard-fail every call — degrade to default trust.
    assert partner_verify(_p(ca_cert_pem="-----BEGIN CERTIFICATE-----\nnope\n-----END CERTIFICATE-----")) is True
