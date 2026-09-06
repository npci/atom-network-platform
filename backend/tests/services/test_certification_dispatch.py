# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The certification harness seam.

Unlike the channel adapters, neither implementation here was invented to prove
the abstraction — both already existed as functions with an identical signature,
selected by an `if settings.precert_engine_enabled:`. This turned that branch
into a named extension point without changing which harness runs.

The assertions that matter: the selector still picks the same harness it always
did, a domain with no certifier is a no-op rather than an error, and the
adapter cannot recurse into the function it wraps.
"""
import asyncio
import inspect

import pytest

from app.packs.network.certification import (
    CertAgentHarness, PrecertEngineHarness, _to_result, default_harness,
)
from app.core.domain.contract import CertificationHarness, CertResult


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ── Both satisfy the Protocol ────────────────────────────────────────────────

@pytest.mark.parametrize("harness", [CertAgentHarness(), PrecertEngineHarness()],
                         ids=["cert_agent", "precert"])
def test_harness_satisfies_the_protocol(harness):
    assert isinstance(harness, CertificationHarness)
    assert harness.key


def test_selector_still_picks_what_it_always_did(monkeypatch):
    """Behaviour preservation. The flag chose the precert engine before this
    refactor and must still choose it."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "precert_engine_enabled", False, raising=False)
    # Ambient CERT_HARNESS would otherwise decide this for us (see F-1).
    monkeypatch.setattr(settings, "cert_harness", "", raising=False)
    assert default_harness().key == "cert_agent"

    monkeypatch.setattr(settings, "precert_engine_enabled", True, raising=False)
    assert default_harness().key == "precert"


def test_the_upi_pack_now_declares_certification(monkeypatch):
    from app.core.domain.contract import certification_of
    from app.packs.network.pack import NetworkPack

    from app.core.config import settings
    monkeypatch.setattr(settings, "precert_engine_enabled", False, raising=False)
    monkeypatch.setattr(settings, "cert_harness", "", raising=False)

    harness = certification_of(NetworkPack())
    assert harness is not None and harness.key == "cert_agent"


# ── The no-certifier case ────────────────────────────────────────────────────

def test_no_harness_is_a_no_op_not_an_error(monkeypatch):
    """An internal API deprecation has no certification body. A lifecycle that
    treated a missing harness as an error would deadlock on that domain.

    Both declaration paths are stubbed to absence: the Python-pack harness
    object (`certification_of`) AND the config-pack harness NAME
    (`certification_harness_of`). A domain is only certifier-less when it
    declares neither — either one alone now supplies an engine."""
    from app.services import certification_dispatch as cd

    monkeypatch.setattr(cd, "certification_of", lambda _p: None, raising=False)
    monkeypatch.setattr(cd, "certification_harness_of", lambda _p: None,
                        raising=False)
    assert _run(cd.run_certification("chg", "p1", "PSP", {})) is None


def test_config_pack_names_a_platform_harness(tmp_path, monkeypatch):
    """A YAML pack certifies by NAMING a platform-registered harness — the
    genericisation ruling's shape: config supplies the name, the platform
    supplies the behaviour. No Python pack class involved."""
    from app.core.domain import registry
    from app.services import certification_dispatch as cd

    pack = tmp_path / "certifying.yaml"
    pack.write_text("key: certifying\ncertification_harness: sim_pack\n")
    monkeypatch.setenv("DOMAIN_PACK", str(pack))
    registry._load.cache_clear()
    try:
        harness = cd._resolve_harness(None)
        assert harness is not None and harness.key == "sim_pack"
    finally:
        registry._load.cache_clear()


def test_config_pack_without_the_key_still_means_absence(tmp_path, monkeypatch):
    """Omission keeps its meaning across the new path: a YAML pack that names
    no harness has no certification body, and dispatch resolves None."""
    from app.core.domain import registry
    from app.services import certification_dispatch as cd

    pack = tmp_path / "bare.yaml"
    pack.write_text("key: bare\n")
    monkeypatch.setenv("DOMAIN_PACK", str(pack))
    registry._load.cache_clear()
    try:
        assert cd._resolve_harness(None) is None
    finally:
        registry._load.cache_clear()


def test_config_pack_naming_an_unknown_harness_is_refused(tmp_path, monkeypatch):
    """A name the platform does not register RAISES rather than silently
    running a different engine — the dispatch turns that into the
    `unknown_harness` refusal the operator can see."""
    from app.core.domain import registry
    from app.services import certification_dispatch as cd

    pack = tmp_path / "typo.yaml"
    pack.write_text("key: typo\ncertification_harness: simpack\n")
    monkeypatch.setenv("DOMAIN_PACK", str(pack))
    registry._load.cache_clear()
    try:
        with pytest.raises(ValueError):
            cd._resolve_harness(None)
        result = _run(cd.run_certification("chg", "p1", "PSP", {}))
        assert result is not None and result.passed is None
        assert result.details.get("error") == "unknown_harness"
    finally:
        registry._load.cache_clear()


def test_a_raising_harness_does_not_propagate(monkeypatch):
    """Fire-and-forget: these run as background tasks. An exception escaping
    would kill the task with nothing recorded — the orchestrators this wraps
    never raise, and the seam must not introduce that."""
    class _Boom:
        key = "boom"
        async def run(self, **_kw):
            raise RuntimeError("engine down")

    from app.services import certification_dispatch as cd
    monkeypatch.setattr(cd, "certification_of", lambda _p: _Boom(), raising=False)

    result = _run(cd.run_certification("chg", "p1", "PSP", {}))
    assert isinstance(result, CertResult)
    assert result.passed is None
    assert result.details.get("error")


def test_dispatch_passes_every_argument_through(monkeypatch):
    seen = {}

    class _H:
        key = "stub"
        async def run(self, **kw):
            seen.update(kw)
            return CertResult(passed=True, run_id="r1")

    from app.services import certification_dispatch as cd
    monkeypatch.setattr(cd, "certification_of", lambda _p: _H(), raising=False)

    _run(cd.run_certification("chg-1", "p-1", "PSP", {"a": 1}, {"TC1": {"b": 2}}))
    assert seen["change_id"] == "chg-1"
    assert seen["partner_id"] == "p-1"
    assert seen["role"] == "PSP"
    assert seen["test_data"] == {"a": 1}
    assert seen["test_data_per_case"] == {"TC1": {"b": 2}}


# ── Result mapping ───────────────────────────────────────────────────────────

def test_unadjudicated_run_is_none_not_false():
    """`passed=False` means the partner failed certification. A run that has
    been dispatched but not adjudicated must not be recorded as a failure —
    that would flip an assignment to a verdict nobody reached."""
    assert _to_result({"run_id": "r1"}).passed is None
    assert _to_result({"status": "certified"}).passed is True
    assert _to_result({"status": "failed"}).passed is False
    assert _to_result({"passed": False}).passed is False


def test_result_keeps_the_full_summary():
    """The orchestrators return rich summaries the UI and triage rely on;
    narrowing them to a boolean would lose per-test-case detail."""
    summary = {"run_id": "r1", "passed": True, "cases": [{"id": "TC1"}]}
    assert _to_result(summary).details == summary


def test_to_result_survives_an_empty_summary():
    assert _to_result({}).passed is None
    assert _to_result(None).passed is None


# ── The recursion hazard ─────────────────────────────────────────────────────

def test_orchestrator_no_longer_selects_a_harness():
    """The adapter CALLS orchestrate_cert_run, so leaving harness selection
    inside it would recurse forever. The selector moved to
    certification_dispatch; this pins it out of the orchestrator."""
    from app.services import cert_orchestrator

    src = inspect.getsource(cert_orchestrator.orchestrate_cert_run)
    assert "orchestrate_cert_run_precert_engine(" not in src
    assert "certification_of" not in src


def test_executor_dispatches_through_the_seam():
    from app.a2a_common import authority_executor

    src = inspect.getsource(authority_executor)
    assert "run_certification" in src
    assert "import orchestrate_cert_run" not in src
