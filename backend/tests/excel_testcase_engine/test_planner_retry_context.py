# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Planner retry loops must not discard the brief they are correcting.

Regression for the 2026-09-05 cert_test_cases failure: the skeleton loop
replaced `user_msg` with a bare correction, dropping the EnrichedBrief and the
"Required fields:" block. Attempts 1-2 then ran blind and invented
`workbook_title` / `sheet_name`, so the run died on a ValidationError that the
retry itself had caused.
"""

from __future__ import annotations

import pytest

from app.excel_testcase_engine.agents import planner
from app.excel_testcase_engine.observability import ConfigurationError
from app.excel_testcase_engine.schemas.enriched_brief import EnrichedBrief


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class _ScriptedClient:
    """Returns each scripted payload in turn, recording the prompts it saw."""

    def __init__(self, payloads: list[str]) -> None:
        self._payloads = list(payloads)
        self.user_messages: list[str] = []

    async def complete(self, *, system, messages, **kwargs):
        self.user_messages.append(messages[-1].content)
        return _FakeResponse(self._payloads.pop(0))


def _brief() -> EnrichedBrief:
    return EnrichedBrief(
        original_brief="SENTINEL_BRIEF_TEXT — the payer/payee transfer flow",
        archetype="B",
        feature_name="Transfer",
        roles=["Payer PSP", "Payee PSP"],
        apis=["ReqTransfer", "RespTransfer"],
    )


_GOOD_SKELETON = """
{"filename": "Pack.xlsx", "archetype": "B", "sheets": [
  {"name": "Payer PSP", "layout": "B1", "test_cases": []},
  {"name": "Payee PSP", "layout": "B1", "test_cases": []}
]}
"""

# What the model actually returned on attempt 2 in production.
_WRONG_KEYS = """
{"workbook_title": "Pack", "archetype": "B", "sheets": [
  {"sheet_name": "Payer PSP", "layout": "B1", "test_cases": []}
]}
"""


@pytest.mark.asyncio
async def test_retry_after_invalid_json_still_carries_the_brief(monkeypatch):
    """The corrective prompt must append to the original, not replace it."""
    client = _ScriptedClient([_WRONG_KEYS, _GOOD_SKELETON])
    monkeypatch.setattr(planner, "get_client", lambda _n: client)

    plan = await planner._plan_skeleton(_brief())

    assert plan.filename == "Pack.xlsx"
    assert len(client.user_messages) == 2

    first, retry = client.user_messages
    assert "SENTINEL_BRIEF_TEXT" in first

    # The regression: the retry used to be ~200 chars of bare correction.
    assert "SENTINEL_BRIEF_TEXT" in retry, "retry dropped the EnrichedBrief"
    assert "filename" in retry, "retry dropped the required-fields schema block"
    assert len(retry) > len(first), "retry should extend the brief, not replace it"


@pytest.mark.asyncio
async def test_dropped_role_retry_keeps_the_brief_and_is_logged(monkeypatch):
    """Dropping a role the brief named is the only reason to retry on count."""
    one_sheet = """
    {"filename": "Pack.xlsx", "archetype": "B", "sheets": [
      {"name": "Payer PSP", "layout": "B1", "test_cases": []}
    ]}
    """
    client = _ScriptedClient([one_sheet, _GOOD_SKELETON])
    monkeypatch.setattr(planner, "get_client", lambda _n: client)

    events: list[tuple] = []
    monkeypatch.setattr(
        planner.LOGGER, "warning", lambda ev, **kw: events.append((ev, kw)),
    )

    plan = await planner._plan_skeleton(_brief())   # brief names 2 roles

    assert len(plan.sheets) == 2
    retry = client.user_messages[1]
    assert "SENTINEL_BRIEF_TEXT" in retry, "retry dropped the EnrichedBrief"
    assert "Payee PSP" in retry, "the retry must name the dropped role"

    dropped = [kw for ev, kw in events if ev == "planner.skeleton.dropped_roles"]
    assert dropped, "the dropped-role rejection must be logged, not silent"
    assert dropped[0]["missing"] == ["Payee PSP"]


@pytest.mark.asyncio
async def test_archetype_c_accepts_a_two_role_pack(monkeypatch):
    """The production case: archetype C with 2 roles must simply work.

    C used to demand 3+ role sheets, which no 2-role BRD/TSD could satisfy and
    which the pack vocabulary could not pad to either. The archetype now only
    selects annexure depth, so this is a normal, valid pack.
    """
    two_sheets_c = """
    {"filename": "Pack.xlsx", "archetype": "C", "sheets": [
      {"name": "Operator", "layout": "C1", "test_cases": []},
      {"name": "Maintenance Organisation", "layout": "C2", "test_cases": []}
    ]}
    """
    client = _ScriptedClient([two_sheets_c])
    monkeypatch.setattr(planner, "get_client", lambda _n: client)

    brief = _brief()
    brief.archetype = "C"
    brief.roles = ["Operator", "Maintenance Organisation"]

    plan = await planner._plan_skeleton(brief)

    assert plan.archetype == "C"
    assert [s.name for s in plan.sheets] == ["Operator", "Maintenance Organisation"]
    assert len(client.user_messages) == 1, "must succeed on the first attempt"


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2, 5, 7])
async def test_role_sheet_count_follows_the_brief(monkeypatch, count):
    """However many roles the documents name — that many sheets. No cap."""
    roles = [f"Party {i}" for i in range(count)]
    sheets = ", ".join(
        f'{{"name": "{r}", "layout": "C1", "test_cases": []}}' for r in roles
    )
    client = _ScriptedClient(
        ['{"filename": "P.xlsx", "archetype": "C", "sheets": [' + sheets + "]}"]
    )
    monkeypatch.setattr(planner, "get_client", lambda _n: client)

    brief = _brief()
    brief.archetype = "C"
    brief.roles = roles

    plan = await planner._plan_skeleton(brief)

    assert [s.name for s in plan.sheets] == roles
    assert len(client.user_messages) == 1, "no retry — any role count is valid"


@pytest.mark.asyncio
async def test_extra_role_sheets_are_allowed_but_recorded(monkeypatch):
    """More sheets than roles is permitted, but logged (could be invention)."""
    three = """
    {"filename": "Pack.xlsx", "archetype": "C", "sheets": [
      {"name": "Payer PSP", "layout": "C1", "test_cases": []},
      {"name": "Payee PSP", "layout": "C2", "test_cases": []},
      {"name": "Settlement Agent", "layout": "C3", "test_cases": []}
    ]}
    """
    client = _ScriptedClient([three])
    monkeypatch.setattr(planner, "get_client", lambda _n: client)

    events: list[tuple] = []
    monkeypatch.setattr(
        planner.LOGGER, "warning", lambda ev, **kw: events.append((ev, kw)),
    )

    plan = await planner._plan_skeleton(_brief())   # brief names 2 roles

    assert len(plan.sheets) == 3, "extra sheets must not be rejected"
    extra = [kw for ev, kw in events if ev == "planner.skeleton.extra_role_sheets"]
    assert extra and extra[0]["extra"] == ["Settlement Agent"]


@pytest.mark.asyncio
async def test_zero_roles_fails_before_any_llm_call(monkeypatch):
    """Empty is the one remaining error — nothing to build sheets from."""
    def _boom(_name):
        raise AssertionError("planner must not call the LLM with no roles")

    monkeypatch.setattr(planner, "get_client", _boom)

    brief = _brief()
    brief.roles = []

    with pytest.raises(ConfigurationError) as exc:
        await planner.plan(brief)

    assert "does not name any role" in str(exc.value)


def test_configuration_error_reaches_the_operator():
    """ConfigurationError must not be collapsed to the generic string."""
    from app.api.agents import _engine_error_detail

    detail = _engine_error_detail(
        ConfigurationError("Archetype C requires at least 3 role sheets")
    )
    assert detail == "Archetype C requires at least 3 role sheets"

    # Anything else still stays opaque.
    assert _engine_error_detail(
        RuntimeError("/app/secret/path.py exploded")
    ) == "An internal error occurred"
