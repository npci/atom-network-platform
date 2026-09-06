# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The first load-bearing seam: agents get domain vocabulary from a pack.

Before PR #9, four agents did `from app.core.domain_rules import NETWORK_HARD_RULES`.
Now they ask the registry. The behaviour must be unchanged — these are prompts
that reach a model, so "equivalent" is not good enough, only "identical" is.
"""
import importlib

import pytest

from app.core.domain.config_pack import ConfigPackError, load as load_config_pack
from app.core.domain.contract import DomainPack
from app.core.domain.registry import (
    UnknownPackError, active_pack_key, get_active_pack, prompt_block,
)
from app.packs.network.pack import NetworkPack
from app.packs.network.rules import NETWORK_ERROR_CODE_EXAMPLES, NETWORK_HARD_RULES


def test_default_pack_is_network(monkeypatch):
    """DOMAIN_PACK is a file path now (the registered-key/`_PACKS` resolution
    is dormant — see registry.py's module docstring), so the raw selector is
    no longer the bare word "network"; what stays true is that the DEFAULT
    resolves to a pack whose `.key` is "network".

    The DEFAULT is what's under test, so the ambient DOMAIN_PACK is removed —
    without that this test asserted whatever the shell happened to export
    (the F-1 shape: it failed on any NLLN-configured host)."""
    from app.core.domain import registry

    monkeypatch.delenv("DOMAIN_PACK", raising=False)
    registry._load.cache_clear()
    try:
        assert active_pack_key().endswith("network.yaml")
        assert get_active_pack().key == "network"
    finally:
        registry._load.cache_clear()


def test_network_pack_satisfies_the_contract():
    assert isinstance(NetworkPack(), DomainPack)


def test_pack_supplies_the_exact_rules_the_agents_used_to_import(monkeypatch):
    """Byte-identity, not equivalence. These strings are prompt input; a
    changed byte is a changed prompt and, for cached segments, a cache miss.

    Pinned to the default network pack: the byte-identity claim is about its constants,
    and asserting them against whatever pack the shell exports made this fail
    on every non-network host (the F-1 shape)."""
    from app.core.domain import registry

    monkeypatch.delenv("DOMAIN_PACK", raising=False)
    registry._load.cache_clear()
    try:
        assert prompt_block("hard_rules") == NETWORK_HARD_RULES
        assert prompt_block("error_codes") == NETWORK_ERROR_CODE_EXAMPLES
    finally:
        registry._load.cache_clear()


@pytest.mark.parametrize(
    "module_path, const_name",
    [
        ("app.agents.canvas", "SYSTEM_PROMPT"),
        ("app.agents.xsd", "ASSESSMENT_SYSTEM_PROMPT"),
        ("app.agents.xsd", "XSD_GENERATION_SYSTEM_PROMPT"),
    ],
)
def test_agent_prompts_still_embed_the_pack_rules(module_path, const_name):
    """These prompts are module-level f-strings evaluated at import time, which
    is why the registry must resolve without a DB or a request context.

    Asserted against the ACTIVE pack's rules, not the network's constant: the claim is
    "the prompt embeds the domain's hard rules", and under an NLLN process the
    module correctly embedded NLLN's — asserting the network's text there failed the
    GENERIC behaviour for being generic. This is now a stronger test: it runs
    meaningfully under every DOMAIN_PACK the suite is executed with."""
    mod = importlib.import_module(module_path)
    active_rules = prompt_block("hard_rules")
    assert active_rules, "active pack declares no hard_rules block"
    assert active_rules in getattr(mod, const_name)


def test_unknown_pack_raises_clearly_rather_than_falling_back(monkeypatch):
    """Silently defaulting would mean a deployment intending one domain quietly
    generating another's vocabulary — wrong prose, not an error, which is the
    worst way to discover it. DOMAIN_PACK is a file path now, so "unknown"
    means "no file there" rather than "not in a registry dict" — the error
    must still name the value that was tried and say why it failed."""
    from app.core.domain import registry

    monkeypatch.setenv("DOMAIN_PACK", "not-a-pack")
    registry._load.cache_clear()
    with pytest.raises(UnknownPackError) as exc:
        registry.get_active_pack()
    assert "not-a-pack" in str(exc.value)
    assert "no file exists" in str(exc.value)   # says WHY, not just THAT
    registry._load.cache_clear()


def test_prompt_block_returns_default_for_a_block_the_pack_omits():
    """A domain with no publishing authority supplies no authority block. Core
    must not KeyError on that — it is a valid domain, not a broken pack."""
    assert prompt_block("no_such_block") == ""
    assert prompt_block("no_such_block", "fallback") == "fallback"


def test_pack_declares_capabilities_it_lacks_by_omission(monkeypatch):
    """Omission is how a pack states absence — it must never stub.

    `channel()` was wired when the A2A adapter landed, so the network pack now reports one.
    This test guards the declaration MECHANISM (define-the-method vs omit-it),
    not a fixed harness answer — so the deployment's `CERT_HARNESS` is
    neutralised the same way `test_cert_modes.py` / `test_cert_pack_run.py`
    do; without that, a host `.env` carrying `CERT_HARNESS=sim_pack` changed
    this test's outcome (the F-1 shape, third instance).
    """
    from app.core.config import settings
    from app.core.domain.contract import certification_of, channel_of

    monkeypatch.setattr(settings, "cert_harness", "", raising=False)

    pack = NetworkPack()
    # Both capabilities are wired now. What this still guards is that they are
    # declared by DEFINING the method — a pack that lacks one omits it, and the
    # accessors report None rather than the pack returning a stub.
    channel = channel_of(pack)
    assert channel is not None and channel.key == "a2a"
    harness = certification_of(pack)
    assert harness is not None and harness.key in {"cert_agent", "precert"}

    class _Bare:
        key = "bare"; version = "0"
        def change_types(self): return []
        def artifacts(self): return []
        def prompt_blocks(self): return {}

    assert certification_of(_Bare()) is None, "omission must still mean absence"
    assert channel_of(_Bare()) is None


def test_pack_artifacts_carry_real_blueprints():
    specs = {s.key: s for s in NetworkPack().artifacts()}
    assert {"canvas", "brd", "tech_spec", "xsd"} <= set(specs)
    assert specs["brd"].blueprint is not None
    assert specs["brd"].blueprint["sections"]


def test_config_pack_repo_roles_round_trip(tmp_path):
    path = tmp_path / "domain.yaml"
    path.write_text(
        """key: topology
repo_roles:
  - key: core
    label: Core / shared library
    required: true
    builds_first: true
  - key: app
    required: true
""",
        encoding="utf-8",
    )

    roles = load_config_pack(str(path)).repo_roles()
    assert [role.key for role in roles] == ["core", "app"]
    assert roles[0].label == "Core / shared library"
    assert roles[0].required is True and roles[0].builds_first is True
    assert roles[1].required is True and roles[1].builds_first is False


def test_config_pack_rejects_malformed_repo_role(tmp_path):
    path = tmp_path / "bad-domain.yaml"
    path.write_text("key: topology\nrepo_roles:\n  - label: Missing key\n", encoding="utf-8")

    pack = load_config_pack(str(path))
    with pytest.raises(ConfigPackError, match="repo_roles"):
        pack.repo_roles()


def test_core_does_not_import_the_pack_directly():
    """The seam only holds if core asks the registry. A direct import from
    app.core.* into app.packs.* would quietly re-couple them."""
    import pathlib

    core = pathlib.Path(__file__).resolve().parents[2] / "app" / "core"
    offenders = [
        p.relative_to(core.parent)
        for p in core.rglob("*.py")
        if "app.packs" in p.read_text(encoding="utf-8")
        and p.name != "registry.py"      # the registry resolves by string path
    ]
    assert not offenders, f"core imports a pack directly: {offenders}"
