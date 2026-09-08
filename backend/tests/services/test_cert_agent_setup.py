# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Config-submission -> ParticipantConfig mapping, and the simulator block.

The DB-touching half of `setup.py` (configure_participant / assign_scope / catalogue
lookups) is exercised by the live cert run, not here — these cover the pure
mapping decisions, which are the ones that silently produce a wrong environment
rather than an error.
"""
from __future__ import annotations

from app.services.cert_agent.setup import participant_config_from_submission, simulator_block

# What the partner actually sends today (handlers/cert_lifecycle.py `_BANK_CONFIG`).
# The KEYS are the partner's wire spelling and stay verbatim; the mapped config's
# field names are the platform's own and are domain-neutral.
FLAT = {
    "bank_name": "My Bank", "bank_code": "MYB", "bank_org_id": "MYORG1",
    "bank_ifsc": "MYPS0000001", "psp_name": "MyPSP", "psp_org_id": "OLV101",
    "psp_code": "MYB01", "handler": "mypsp", "mpinlength": "6",
    "bank_server_ip": "127.0.0.1", "bank_server_port": "8443",
}

# The shape the spec's Appendix B defines (the partner is being moved to this).
NESTED = {
    "bank_identity": {"bank_name": "Bank X", "org_id": "ORGX001", "nbin": "BKID0XXXXXX",
                      "ifsc": "BKID0001234", "participant_code": "BKIDPSP001",
                      "handle": "@bankx", "acquirer_id": "ACQ-BKID-001"},
    "network": {"host": "10.42.10.45", "port": 8443, "base_url": "https://network-cert.bankx.in"},
    "security": {"tls_tier": "mtls"},
    "roles": ["remitter"], "requested_subset": "Subset-D",
}


def test_flat_submission_maps_every_field_precert_needs():
    c = participant_config_from_submission(FLAT)
    assert c.participant_org_id == "OLV101"
    assert c.partner_org_id == "MYORG1"
    assert c.partner_code == "MYB"
    # Routing is the field that decides whether a case can complete at all: precert
    # sends the request here, and a wrong value times out waiting on the
    # simulator's transaction log.
    assert (c.partner_server_ip, c.partner_server_port) == ("127.0.0.1", "8443")


def test_nested_spec_submission_is_understood():
    c = participant_config_from_submission(NESTED)
    # spec has no psp_org_id; participant_code stands in
    assert c.participant_org_id == "BKIDPSP001"
    assert c.partner_org_id == "ORGX001"
    assert c.partner_routing_code == "BKID0001234"
    assert c.handler == "bankx"              # "@bankx" -> handler, the @ is not part of it
    assert (c.partner_server_ip, c.partner_server_port) == ("10.42.10.45", "8443")  # int port -> str


def test_protobuf_float_port_does_not_become_a_bad_routing_string():
    """The A2A wire is protobuf Struct: one numeric type, so 8443 arrives as 8443.0.

    Naive str() gives "8443.0", which precert would store as the partner's
    endpoint port and then fail to route to — a silent, hard-to-trace break.
    Observed on the wire, not hypothetical.
    """
    nested = {**NESTED, "network": {**NESTED["network"], "port": 8443.0}}
    assert participant_config_from_submission(nested).partner_server_port == "8443"
    # A non-numeric port is passed through rather than silently zeroed.
    weird = {**NESTED, "network": {**NESTED["network"], "port": "8443/tcp"}}
    assert participant_config_from_submission(weird).partner_server_port == "8443/tcp"


def test_nested_submission_cannot_supply_bank_code():
    """A documented gap, pinned so it is not mistaken for a mapping bug.

    precert keys institutions on a 3-letter `bank_code` (its own column name);
    the spec's `bank_identity` has no equivalent field. Until that is reconciled,
    a purely spec-shaped payload onboards with an empty partner code unless a
    default is supplied.
    """
    assert participant_config_from_submission(NESTED).partner_code == ""
    assert (participant_config_from_submission(NESTED, defaults={"bank_code": "BKX"})
            .partner_code == "BKX")


def test_certificates_are_never_carried():
    """Spec: certificate bodies are never inline — only cert_ref + fingerprint."""
    assert participant_config_from_submission(FLAT).hsm_file_b64 == ""
    assert participant_config_from_submission(NESTED).hsm_file_b64 == ""


def test_defaults_only_fill_gaps():
    c = participant_config_from_submission(FLAT, defaults={"psp_org_id": "OTHER"})
    assert c.participant_org_id == "OLV101", \
        "the partner's own value must win over the fallback"


def test_no_psp_identifier_is_not_onboardable():
    assert participant_config_from_submission({}) is None
    assert participant_config_from_submission({"bank_name": "X"}) is None
    # …but a fallback makes it onboardable, which is how a partner that merely
    # acks cert_config_request still certifies.
    assert participant_config_from_submission({}, defaults={"psp_org_id": "OLV101"}) is not None


def test_simulator_block_publishes_an_alias_never_a_url():
    """ITA-6 / item 3.5: a raw URL on the wire is an address for the partner
    to dial into the Authority's test estate — the tunnel's §2 refuses it. The alias is
    resolved by each side's own allowlist."""
    s = simulator_block(alias="cert_simulator", cflow_id="CFLOW-1")
    assert s["endpoint"] == "a2a://cert_simulator"
    # protocol_version now derives from the ACTIVE pack (`<key>-<version>`)
    # instead of the hardcoded "UPI-2.x" that put a payments label on every
    # domain's wire — so this asserts against the active pack, not a fixed
    # string, and stays green under whatever DOMAIN_PACK the suite runs.
    from app.core.domain.registry import get_active_pack

    _p = get_active_pack()
    assert s["protocol_version"] == f"{_p.key}-{_p.version}"
    # An explicit override is still honoured.
    assert simulator_block(alias="cert_simulator",
                           protocol_version="X-9")["protocol_version"] == "X-9"
    assert s["credentials_ref"] == "cflow://CFLOW-1/sim-credentials"
    # No cflow -> no invented vault path.
    assert simulator_block(alias="cert_simulator")["credentials_ref"] is None


def test_simulator_block_appends_the_pack_selector_percent_encoded():
    """Both halves of item 3.5 in one edit: SIM's ?pack= rides the same
    endpoint, percent-encoded so `CHG-4711@3` survives every parser."""
    s = simulator_block(alias="cert_simulator", pack_ref="CHG-4711@3")
    assert s["endpoint"] == "a2a://cert_simulator?pack=CHG-4711%403"


def test_hsm_cert_seeding_removed_stays_empty_on_onboarding(monkeypatch):
    """`hsm_file_b64` is written empty on every onboarding, and stays that way.

    Previously `provision_from_config` copied a shared RSA public key
    (`simulator_hsm_cert()`) into `tbl_cert_file.hsm_file`, because the
    deleted Java `CredEncryptorService.hsmPubKey` called it UNGUARDED on
    every request path and an empty value was fatal at run time. That
    function, its call site here, and the RSA key it copied were all
    removed when the shared-key seeding approach was withdrawn: nothing
    remaining in this repository reads `hsm_file`, so there is nothing left
    to seed it for, and no `hsm_cert_b64` parameter to pass one in through.
    """
    import app.services.cert_agent.setup as S

    captured = {}
    monkeypatch.setattr(S, "subset_exists", lambda db_cfg, s: True)
    monkeypatch.setattr(S, "catalogue_cases", lambda db_cfg, s: [("RE_94", "REMITTER")])

    class _Prov:
        def __init__(self, db):  # noqa: D107
            pass

        def configure_participant(self, cfg):
            captured["hsm"] = cfg.hsm_file_b64
            return ["+participant"]

        def assign_scope(self, cfg, **kw):
            return ["+scope"]

    monkeypatch.setattr(S, "NfiniteProvisioner", _Prov)

    S.provision_from_config(FLAT, db_cfg={}, cert_name="Cert1", default_subset="Subset-A2A-BIDI")
    assert captured["hsm"] == "", "hsm_file_b64 must stay empty — nothing reads it any more"
    assert not hasattr(S, "simulator_hsm_cert"), "simulator_hsm_cert should be fully removed"
