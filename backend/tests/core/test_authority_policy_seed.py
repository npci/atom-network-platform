# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""The authority-policy seed resolves per deployment, not per repository.

`backend/data/authority_policy.md` used to be an 818-line brief on one payments
network's operating circulars — real bank names, their market shares, regulator
circular numbers — and it was bind-mounted into every container and seeded into
the database on first boot, whatever domain the deployment was for. The
feasibility resolver then fed it to an LLM as authoritative policy.

That corpus is now the network PACK's, reached through
`registry.pack_data_file`. What the platform ships is an empty template. These
tests pin the three things that made the move safe: the shipped default carries
no ecosystem's branding, a pack can supply its own corpus, and a deployment
whose pack supplies none gets nothing rather than someone else's.
"""
import importlib
from pathlib import Path

import pytest

from app.core.domain.registry import pack_data_file

REPO_BACKEND = Path(__file__).resolve().parents[2]
NETWORK_YAML = REPO_BACKEND / "app" / "packs" / "network" / "network.yaml"
NLLN_YAML = REPO_BACKEND / "app" / "packs" / "nlln" / "nlln.yaml"
SHIPPED_DEFAULT = REPO_BACKEND / "data" / "authority_policy.md"

# Names and terms that have no business in a domain-general platform's default
# policy document. Not exhaustive — a tripwire, not a scanner.
BRANDED_TERMS = (
    "HDFC", "ICICI", "SBI", "Axis Bank", "Kotak", "IndusInd", "PhonePe",
    "GPay", "Google Pay", "Paytm", "RuPay", "BHIM", "NPCI", "TPAP",
    "RBI", "UPI",
)


def _seed_path(monkeypatch, *, pack: Path, operator: str):
    """Resolve the seed with DOMAIN_PACK and AUTHORITY_POLICY_PATH pinned.

    `app.core.config.settings` is instantiated at import, so overriding the
    setting means patching the live object, not the environment.
    """
    monkeypatch.setenv("DOMAIN_PACK", str(pack))
    import app.core.authority_policy_seed as mod
    importlib.reload(mod)
    monkeypatch.setattr(mod.settings, "authority_policy_path", operator)
    return mod.authority_policy_seed_path()


def test_shipped_default_names_no_ecosystem():
    """The file every deployment mounts must be domain-neutral.

    This is the actual defect the move fixed: not that the corpus existed, but
    that it was the DEFAULT.
    """
    text = SHIPPED_DEFAULT.read_text(encoding="utf-8")
    found = sorted({t for t in BRANDED_TERMS if t.lower() in text.lower()})
    assert not found, (
        f"{SHIPPED_DEFAULT} is the platform's shipped default policy and names "
        f"{found}. Domain policy belongs in a pack's data/ directory."
    )


def test_network_pack_supplies_its_own_corpus(monkeypatch):
    """The payments corpus survived the move and is still reachable — for the
    network pack only."""
    resolved = _seed_path(monkeypatch, pack=NETWORK_YAML, operator="/nonexistent/policy.md")
    assert resolved is not None, "the network pack should ship a policy seed"
    assert resolved.is_file()
    assert "packs/network/data" in resolved.as_posix()


def test_pack_without_a_corpus_gets_nothing(monkeypatch):
    """The load-bearing negative. A domain that supplies no policy must seed an
    empty row — inheriting the network pack's would put a payments rulebook in
    front of a library network's resolver."""
    assert _seed_path(monkeypatch, pack=NLLN_YAML, operator="/nonexistent/policy.md") is None


def test_operator_file_outranks_the_pack(monkeypatch, tmp_path):
    """The bind mount is the documented override and must win, so an operator
    can supply real policy without rebuilding an image."""
    operator = tmp_path / "authority_policy.md"
    operator.write_text("# Our policy\n", encoding="utf-8")
    resolved = _seed_path(monkeypatch, pack=NETWORK_YAML, operator=str(operator))
    assert resolved == operator


@pytest.mark.parametrize("pack", [NETWORK_YAML, NLLN_YAML])
def test_pack_data_file_is_scoped_to_the_active_pack(monkeypatch, pack):
    """`pack_data_file` resolves beside the active pack's YAML. A pack must not
    be able to reach another's data by asking for a filename."""
    monkeypatch.setenv("DOMAIN_PACK", str(pack))
    found = pack_data_file("authority_policy.md")
    if found is not None:
        assert found.parent.parent == pack.parent
    assert pack_data_file("no_such_file.md") is None
