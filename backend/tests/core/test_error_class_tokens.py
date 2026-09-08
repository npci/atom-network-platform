# SPDX-License-Identifier: MIT
"""The error-code CLASSIFICATION must come from the active pack, not be hardcoded.

Regression guard for a defect found 2026-09-07 during a full ADCN E2E: the
`td_bd` FIELD name is shared across packs (a pinned key), but its VALUES are
domain-specific — the network pack declares TD/BD, adcn and nlln declare SE/PD.
`_validate_tech_spec` hardcoded `\\b(TD|BD)\\b`, which was correct for exactly
one of the three shipped packs. On the other two it failed in BOTH directions:
it never enforced the tokens the domain requires, and it demanded tokens the
domain forbids.

The neighbouring error-code SHAPE check was already pack-driven with an
explicit comment that falling back to another domain's alphabet is the failure
it exists to prevent. These tests pin that the classification check now obeys
the same contract.
"""
import os

import pytest
import yaml

from app.core.domain.config_pack import ConfigPack
from app.core.domain.contract import error_class_tokens_of

# __file__ is backend/tests/core/<this>.py — three levels up is backend/.
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NETWORK = os.path.join(BACKEND, "app", "packs", "network", "network.yaml")
NLLN = os.path.join(BACKEND, "app", "packs", "nlln", "nlln.yaml")


def _pack(path: str) -> ConfigPack:
    return ConfigPack(yaml.safe_load(open(path)), source_path=path)


def test_network_pack_declares_td_bd():
    """The payments domain's tokens. Pinned: this is the one case the old
    hardcoded check got right, so a regression here would be invisible."""
    assert error_class_tokens_of(_pack(NETWORK)) == frozenset({"TD", "BD"})


def test_nlln_pack_declares_se_pd_not_td_bd():
    """A non-payments domain must NOT inherit the payments alphabet."""
    tokens = error_class_tokens_of(_pack(NLLN))
    assert tokens == frozenset({"SE", "PD"})
    assert "TD" not in tokens and "BD" not in tokens


def test_tokens_are_disjoint_across_domains():
    """If these ever overlap, a document valid in one domain silently passes
    the other's check — the exact confusion this accessor prevents."""
    assert not (error_class_tokens_of(_pack(NETWORK))
                & error_class_tokens_of(_pack(NLLN)))


def test_pack_without_declaration_yields_silence_not_a_default():
    """Empty means SKIP the check. Returning a default here would reintroduce
    the bug in a new form — asserting an alphabet the domain never declared."""
    pack = ConfigPack({"key": "bare", "version": "1.0"}, source_path="<test>")
    assert error_class_tokens_of(pack) == frozenset()


def test_long_names_in_the_rule_prose_are_not_mistaken_for_tokens():
    """The declaration reads `td_bd = "SE" (System Error) or "PD" (Policy
    Decline)`. Only the quoted short tokens are tokens; the parenthesised
    prose must not leak in."""
    pack = ConfigPack(
        {"key": "x", "version": "1.0", "prompt_blocks": {
            "proposals_error_class_rule":
                '- Every error code MUST have td_bd = "SE" (System Error) '
                'or "PD" (Policy Decline).'}},
        source_path="<test>",
    )
    assert error_class_tokens_of(pack) == frozenset({"SE", "PD"})


@pytest.mark.parametrize("pack_path,good,bad", [
    (NETWORK, "| U09 | TD | PSP | bad |", "| U09 | SE | PSP | bad |"),
    (NLLN, "| E003 | SE | Library | bad |", "| E003 | TD | Library | bad |"),
])
def test_validator_accepts_own_tokens_and_rejects_the_other_domains(
        monkeypatch, pack_path, good, bad):
    """End-to-end through the validator: a document carrying ANOTHER domain's
    classification must still warn. Before the fix, an nlln spec using TD would
    have passed and one using its own SE would have warned — backwards."""
    monkeypatch.setenv("DOMAIN_PACK", pack_path)
    from app.core.domain import registry
    registry.get_active_pack.cache_clear() if hasattr(
        registry.get_active_pack, "cache_clear") else None
    from app.agents.document_validator import _validate_tech_spec

    def warned(text):
        return any(i["rule"] == "missing_td_bd"
                   for i in _validate_tech_spec("error codes " + text))

    assert not warned(good), "own domain's tokens must not warn"
    assert warned(bad), "another domain's tokens must not satisfy the check"
