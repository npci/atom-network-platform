# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""The engine prompts must name the ACTIVE pack's initiator tokens.

`txn_initiated_by` is a wire value: the certification agent matches it character
for character. Both LLM prompts that tell a model what to put there therefore
have to be rendered with the active pack's `cert_roles:`, not frozen in the
markdown.

This exists because `planner.md` was templated and `writer.md` was not, and
nothing failed. Two properties conspired to hide it:

* the suite runs `DOMAIN_PACK=packs/network`, the one pack whose tokens the
  markdown happened to hardcode, so the literals were always right in CI; and
* `WorkbookPlan._normalize_txn_initiated_by` rewrites an unrecognised token to
  the PARTNER role instead of raising, so on any other pack every
  authority-initiated case was silently relabelled partner-initiated.

The pack below deliberately uses tokens that share no substring with any
ecosystem's, so a prompt that reverts to a literal fails on the assertion rather
than passing by coincidence.
"""
from __future__ import annotations

import pathlib

import pytest

PROMPTS = pathlib.Path(__file__).resolve().parents[2] / "app/excel_testcase_engine/prompts"

# Tokens no pack uses, so their presence proves substitution actually happened.
_PACK = (
    "key: initiator_probe\n"
    "certification_harness: sim_pack\n"
    "cert_roles: {authority: ZORB, partner: QUUX, partner_wire: Quux}\n"
    "cert_test_defaults: {amount: '1.00'}\n"
    "wire_envelope: {root_element: R, namespace: 'http://example.test/', org_id: O}\n"
)


@pytest.fixture
def probe_pack(tmp_path, monkeypatch):
    """Activate a pack whose initiator tokens belong to no real ecosystem."""
    from app.core.domain import registry

    pack = tmp_path / "initiator_probe.yaml"
    pack.write_text(_PACK)
    monkeypatch.setenv("DOMAIN_PACK", str(pack))
    registry._load.cache_clear()
    try:
        yield
    finally:
        registry._load.cache_clear()


@pytest.mark.parametrize(
    "system_fn",
    [
        pytest.param("planner", id="planner"),
        pytest.param("writer", id="writer"),
    ],
)
def test_prompt_carries_the_active_packs_initiator_tokens(probe_pack, system_fn):
    """Both prompts must state THIS pack's tokens, with nothing left to fill."""
    if system_fn == "planner":
        from app.excel_testcase_engine.agents.planner import _planner_system as render
    else:
        from app.excel_testcase_engine.agents.writer import _writer_system as render

    text = render()

    assert "ZORB" in text, "authority token missing — prompt was not rendered"
    assert "Quux" in text, "partner wire token missing — prompt was not rendered"
    # `render_prompt` raises on an unfilled name, but assert anyway: a literal
    # `{{...}}` reaching a model reads as a bizarre instruction, not an error.
    assert "{{" not in text, "a placeholder survived into the prompt"


def test_no_engine_prompt_hardcodes_a_role_token():
    """The class-level guard: no prompt may name an ecosystem's role token.

    A token frozen in the markdown is correct for exactly one pack and silently
    wrong for every other, so this bans the whole shape rather than the two
    files that happened to carry it.
    """
    import re

    banned = re.compile(r"\b(NPCI|UPI|BANK|Bank)\b")
    offenders = {
        path.name: sorted({m.group(0) for m in banned.finditer(path.read_text(encoding="utf-8"))})
        for path in sorted(PROMPTS.glob("*.md"))
        if banned.search(path.read_text(encoding="utf-8"))
    }
    assert not offenders, (
        f"prompts hardcode role tokens {offenders}; render them from the active "
        "pack via render_prompt(..., AUTHORITY_INITIATOR=..., PARTNER_INITIATOR=...)"
    )
