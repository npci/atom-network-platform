# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Unit tests for the party inference agent.

Focus is on the branches the fail-open contract creates — not on
whatever a real LLM would return. We patch `call_llm_structured` to
supply the shape we want to test.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from app.agents import party_inference as PI
from app.core.domain import registry as _registry

# These tests assert UPI's four-party vocabulary, and the module resolves its
# canonical set from the ACTIVE pack at import time — so pin the UPI pack for
# this file's duration and reload the module both ways (see
# test_question_generator_v1 for the full reasoning; same F-1 shape).
_UPI_PACK = str(Path(PI.__file__).resolve().parents[1]
                / "packs" / "network" / "network.yaml")


@pytest.fixture(scope="module", autouse=True)
def _pin_upi_pack_for_this_module():
    prior = os.environ.get("DOMAIN_PACK")
    os.environ["DOMAIN_PACK"] = _UPI_PACK
    _registry._load.cache_clear()
    importlib.reload(PI)
    yield
    if prior is None:
        os.environ.pop("DOMAIN_PACK", None)
    else:
        os.environ["DOMAIN_PACK"] = prior
    _registry._load.cache_clear()
    importlib.reload(PI)


@pytest.mark.asyncio
async def test_returns_llm_response_when_structured_call_succeeds(monkeypatch):
    async def _fake(system, user, *, schema, tool_name, agent_name, max_tokens=None):
        return {
            "parties_in_scope": ["PAYER_PSP", "PAYEE_PSP"],
            "rationale": "BRD names Payer PSP and Payee PSP only.",
            "confidence": "high",
        }

    monkeypatch.setattr(PI, "call_llm_structured", _fake)
    result = await PI.infer_parties(
        enhanced_prompt="Add preAuthLimit tag to ReqTransfer for pre-authorised debits.",
        research_report="…",
        canvas_content="…",
        brd_content="…",
    )
    assert result.parties_in_scope == ["PAYER_PSP", "PAYEE_PSP"]
    assert result.rationale.startswith("BRD names")
    assert result.confidence == "high"
    assert result.source == "llm"


@pytest.mark.asyncio
async def test_llm_error_falls_back_to_all_four(monkeypatch):
    async def _fake(*a, **kw):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(PI, "call_llm_structured", _fake)
    result = await PI.infer_parties(enhanced_prompt="some change")
    assert set(result.parties_in_scope) == {
        "PAYER_PSP", "PAYEE_PSP", "REMITTER_BANK", "BENEFICIARY_BANK",
    }
    assert result.source == "fallback_all_four"
    assert result.confidence == "low"
    assert "provider unavailable".lower() in result.rationale.lower() or "RuntimeError" in result.rationale


@pytest.mark.asyncio
async def test_empty_llm_result_falls_back_to_all_four(monkeypatch):
    async def _fake(*a, **kw):
        return {"parties_in_scope": [], "rationale": "unclear", "confidence": "low"}

    monkeypatch.setattr(PI, "call_llm_structured", _fake)
    result = await PI.infer_parties(enhanced_prompt="a change")
    # Empty LLM response → treat as "no signal" and show all four.
    assert set(result.parties_in_scope) == {
        "PAYER_PSP", "PAYEE_PSP", "REMITTER_BANK", "BENEFICIARY_BANK",
    }
    assert result.source == "fallback_all_four"


@pytest.mark.asyncio
async def test_empty_prompt_returns_no_signal_default(monkeypatch):
    called = {"n": 0}

    async def _fake(*a, **kw):
        called["n"] += 1
        return {"parties_in_scope": ["PAYER_PSP"], "rationale": "x", "confidence": "med"}

    monkeypatch.setattr(PI, "call_llm_structured", _fake)
    result = await PI.infer_parties(enhanced_prompt="")
    # No prompt → skip the LLM entirely and return the safe default.
    assert called["n"] == 0
    assert set(result.parties_in_scope) == {
        "PAYER_PSP", "PAYEE_PSP", "REMITTER_BANK", "BENEFICIARY_BANK",
    }
    assert result.source == "no_signal"
