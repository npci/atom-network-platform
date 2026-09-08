# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Shared fixtures for the eval test suite.

Auto-disables the critic LLM call in every eval test so the runner never
makes real network calls. Tests that specifically want to exercise critic
behaviour (e.g. test_phase_a_critic.py) override this fixture or
monkeypatch `app.services.evaluation.critic.critique` to a stub returning
findings of their choice.

Also pins the domain pack — see `_pin_domain_pack` for why.
"""
from __future__ import annotations

import pathlib

import pytest


@pytest.fixture(autouse=True)
def _pin_domain_pack(monkeypatch):
    """Pin the network pack for this folder, because its FIXTURES are network documents.

    Nearly every artifact in these tests is network-flavoured (`U30`, `Z6`,
    "standard NPCI convention"), and several assert an overall PASS verdict on
    them. Those assertions are only true under the default network pack — they used to
    pass by inheriting whatever `DOMAIN_PACK` the shell happened to hold, and
    silently changed meaning when the suite was run under the pack the service
    actually runs.

    They did not FAIL under NLLN either, which was the real problem: the
    error-code check matched the literal `00` inside an ISO-8601 timestamp, so
    a network fixture "passed" a domain that recognises none of its codes. Once
    that check started reading the code shape from the active pack, the
    latent assumption surfaced as a genuine failure.

    Pinning makes the assumption explicit rather than ambient. Cross-domain
    behaviour is asserted deliberately, by tests that set `DOMAIN_PACK`
    themselves (e.g. `test_code_shape_follows_the_active_domain_pack`), and a
    `monkeypatch.setenv` inside a test body still overrides this.

    NAMED EXPLICITLY, not `registry.DEFAULT_PACK`. This used to read the
    default, and the default WAS the network pack, so "pin the network pack"
    and "pin the default" were the same statement — which is why the docstring
    above could say the first while the code did the second. Once the platform
    default became the domain-NEUTRAL `generic` pack, reading DEFAULT_PACK
    silently re-pointed this whole folder at a pack that recognises none of its
    fixtures' error codes. The fixtures did not change; the tests just started
    failing. This folder wants THIS pack, by name, forever.
    """
    from app.core.domain import registry

    network_pack = str(
        pathlib.Path(registry.__file__).resolve().parents[2]
        / "packs" / "network" / "network.yaml"
    )
    monkeypatch.setenv("DOMAIN_PACK", network_pack)
    registry._load.cache_clear()
    yield
    registry._load.cache_clear()


@pytest.fixture(autouse=True)
def _disable_critic_unless_overridden(monkeypatch, request):
    """By default every test in this folder runs with critic disabled.

    A test can opt back in by adding the marker `pytest.mark.critic_enabled`
    (in which case the fixture stays out of the way and the test is expected
    to monkeypatch critic.critique or the underlying call_llm itself).
    """
    if request.node.get_closest_marker("critic_enabled"):
        return

    from app.services.evaluation import critic as critic_mod

    async def _no_op_critic(*args, **kwargs):
        return critic_mod.CriticResult(
            findings=[],
            judge_model=None,
            provider=None,
            enabled=False,
            latency_ms=0,
        )

    monkeypatch.setattr(critic_mod, "critique", _no_op_critic)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "critic_enabled: opt back into the real critic flow for this test "
        "(the autouse conftest fixture disables it by default).",
    )
