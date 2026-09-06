# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""I-6b: simulator/application mode, per side, per run (§3.6).

The plan's three verify bars, in order:
  1. all four postures resolve to the right targets;
  2. a run against a simulator and one against the application are
     distinguishable FROM THE STORED RESULT ALONE;
  3. application mode cannot be inherited silently from a prior run.
"""
from __future__ import annotations

import pytest

from app.services import cert_modes as m

ALIASES = {"simulator_alias": "cert_simulator",
           "application_alias": "cert_application"}


# ── bar 1: four postures, right targets ──────────────────────────────────────

@pytest.mark.parametrize("npci,partner,npci_alias,partner_alias", [
    (m.SIMULATOR,   m.SIMULATOR,   "cert_simulator",   "cert_simulator"),
    (m.SIMULATOR,   m.APPLICATION, "cert_simulator",   "cert_application"),
    (m.APPLICATION, m.SIMULATOR,   "cert_application", "cert_simulator"),
    (m.APPLICATION, m.APPLICATION, "cert_application", "cert_application"),
])
def test_all_four_postures_resolve_to_the_right_aliases(
        npci, partner, npci_alias, partner_alias):
    modes = m.resolve({"npci_mode": npci, "partner_mode": partner})
    assert m.alias_for("npci", modes.npci, **ALIASES) == npci_alias
    assert m.alias_for("partner", modes.partner, **ALIASES) == partner_alias


def test_the_sides_are_independent():
    modes = m.resolve({"partner_mode": "application"})
    assert (modes.npci, modes.partner) == (m.SIMULATOR, m.APPLICATION)


def test_application_mode_needs_the_trigger_contract_on_either_side():
    """§3.6.1: a deployed application is a subject under test, not a driver —
    there is no control API to call, so it must be TRIGGERED."""
    assert m.requires_trigger(m.APPLICATION) is True
    assert m.requires_trigger(m.SIMULATOR) is False


# ── bar 2: distinguishable from the stored result alone ──────────────────────

def test_each_posture_records_a_distinct_evidence_statement():
    statements = {
        m.RunModes(n, p).evidence()
        for n in m.MODES for p in m.MODES
    }
    assert len(statements) == 4, "each posture must read differently"


def test_a_simulator_pass_says_it_proves_less():
    """§3.6.3: recording them identically lets a certificate claim more than
    was verified."""
    sim = m.RunModes(m.SIMULATOR, m.SIMULATOR).evidence()
    both = m.RunModes(m.APPLICATION, m.APPLICATION).evidence()
    assert "ONLY" in sim and "not evidence" in sim
    assert "full-integration" in both


def test_modes_serialise_onto_the_stored_row():
    assert m.RunModes(m.APPLICATION, m.SIMULATOR).as_dict() == {
        "npci_mode": "application", "partner_mode": "simulator"}


# ── bar 3: no silent inheritance ─────────────────────────────────────────────

def test_application_mode_is_never_inherited_from_the_prior_run():
    """§3.6.4: real deployments have real side effects and the C-6 loop
    re-runs cases round after round."""
    prior = m.RunModes(m.APPLICATION, m.APPLICATION)
    assert m.resolve(None, prior=prior) == m.RunModes(m.SIMULATOR, m.SIMULATOR)
    assert m.resolve({}, prior=prior).any_application is False


def test_application_mode_requires_saying_so_again():
    prior = m.RunModes(m.APPLICATION, m.APPLICATION)
    again = m.resolve({"npci_mode": "application"}, prior=prior)
    assert (again.npci, again.partner) == (m.APPLICATION, m.SIMULATOR)


def test_default_is_simulator_on_both_sides():
    assert m.resolve() == m.RunModes(m.SIMULATOR, m.SIMULATOR)


def test_an_unrecognised_mode_is_refused_not_guessed():
    """A typo'd 'aplication' silently becoming `simulator` would run a weaker
    test than asked for and label it with what was typed."""
    with pytest.raises(ValueError, match="aplication"):
        m.resolve({"npci_mode": "aplication"})


# ── §3.6.4: the loop refuses to auto-dispatch into side effects ──────────────

def test_the_loop_refuses_to_auto_dispatch_in_application_mode():
    ok, why = m.auto_dispatch_allowed(m.RunModes(m.SIMULATOR, m.APPLICATION),
                                      permitted=False)
    assert ok is False
    assert "partner" in why and "side effects" in why


def test_the_refusal_names_every_application_side():
    _, why = m.auto_dispatch_allowed(m.RunModes(m.APPLICATION, m.APPLICATION),
                                     permitted=False)
    assert "npci and partner" in why


def test_simulator_rounds_auto_dispatch_freely():
    assert m.auto_dispatch_allowed(m.RunModes(), permitted=False)[0] is True


def test_configuration_can_explicitly_permit_it():
    ok, why = m.auto_dispatch_allowed(m.RunModes(m.APPLICATION, m.SIMULATOR),
                                      permitted=True)
    assert ok is True and "explicitly permitted" in why


# ── the wiring these rules feed ──────────────────────────────────────────────

def test_the_aliases_and_permission_are_declared_settings():
    from app.core.config import settings

    assert settings.integration_testing_application_alias == "cert_application"
    assert settings.cert_application_mode_auto_dispatch is False, \
        "application-mode auto-dispatch is opt-in (§3.6.4)"


def test_application_mode_without_a_trigger_url_refuses(monkeypatch):
    """§3.6.1: an application-mode side is DRIVEN by the trigger contract.
    With nothing to drive, running the in-process simulator and stamping it
    `application` is the §3.6.3 over-claim reached by accident."""
    import asyncio

    from app.core.config import settings
    from app.services import cert_pack_run

    monkeypatch.setattr(settings, "cert_trigger_url", "")
    out = asyncio.run(cert_pack_run.run_round(
        None, change_id="c", partner_id="p",
        test_data={"modes": {"npci_mode": "application"}}))
    assert out["error"] == "no_authority_trigger_url"


def test_the_signoff_certificate_records_what_was_on_the_other_end():
    from types import SimpleNamespace

    from app.services.cert_orchestrator import _build_signoff_meta

    meta = _build_signoff_meta(
        SimpleNamespace(cert_agent_bank_id="B", name="Bank One"),
        SimpleNamespace(title="T"), flow="UPI", role="PAYER_PSP",
        run_id="r1", passed=3, total=3, signoff_at="2026-08-31T00:00:00",
        modes=m.RunModes(m.SIMULATOR, m.APPLICATION))
    assert meta["partner_mode"] == "application"
    assert "partner's implementation" in meta["evidence"]


# ── S-6: harness selection is per-dispatch (= per change + partner) ──────────

def test_a_dispatch_can_name_its_harness(monkeypatch):
    from app.core.config import settings
    from app.core.domain.contract import certification_of
    from app.packs.network.pack import NetworkPack

    # Neutralise the ambient CERT_HARNESS. Without this, "default unchanged"
    # asserts whatever the developer's .env selected rather than the declared
    # default, so on a host configured for sim_pack it failed.
    monkeypatch.setattr(settings, "cert_harness", "", raising=False)

    pack = NetworkPack()
    assert certification_of(pack, "sim_pack").key == "sim_pack"
    assert certification_of(pack, "cert_agent").key == "cert_agent"
    assert certification_of(pack).key == "cert_agent", "default unchanged"


def test_an_unknown_harness_raises_rather_than_falling_back():
    """Falling back would certify through a different engine than asked."""
    from app.core.domain.contract import certification_of
    from app.packs.network.pack import NetworkPack

    with pytest.raises(ValueError, match="nonsuch"):
        certification_of(NetworkPack(), "nonsuch")


def test_dispatch_reports_an_unknown_harness_instead_of_certifying(monkeypatch):
    """Naming an unknown harness on a domain that HAS certification (just not
    that harness) must report "unknown_harness", not silently degrade to "no
    certifier at all". The registry's default active pack no longer has any
    certification (registered-key resolution is dormant — see
    registry.py's module docstring), so this dispatches against NetworkPack
    directly to keep exercising the scenario the test is actually about."""
    import asyncio

    from app.packs.network.pack import NetworkPack
    import app.services.certification_dispatch as certification_dispatch

    monkeypatch.setattr(certification_dispatch, "get_active_pack", NetworkPack, raising=True)

    result = asyncio.run(certification_dispatch.run_certification(
        "c", "p", "", {}, {}, dispatch_meta={"harness": "nonsuch"}))
    assert result.passed is None
    assert result.details["error"] == "unknown_harness"


def test_a_domain_with_no_certifier_still_reports_absence():
    from app.core.domain.contract import certification_of

    assert certification_of(object()) is None
