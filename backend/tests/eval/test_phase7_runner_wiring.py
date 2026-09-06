# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 7 — runner wiring tests.

Two layers of coverage:
  1. Direct: run_advisory produces a verdict for each new Phase A checkpoint
     given representative artifacts (no LLM, no DB write — _persist_verdict
     is monkeypatched).
  2. Helper: fire_advisory_eval dispatches in both sync and async contexts
     without raising.

These verify that slice-1 contracts + slice-2 checks compose end-to-end and
that the slice-3 wiring helper is safe from either kind of FastAPI handler.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services.evaluation import runner
from app.services.evaluation.checkpoints import CheckpointId, VerdictValue


class _DummyDbSession:
    """Runner accepts any SQLAlchemy-like session object."""


@pytest.fixture
def captured_save(monkeypatch):
    captured: dict = {}

    def _fake_save(db, change_request_id, verdict):
        captured["db"] = db
        captured["change_request_id"] = change_request_id
        captured["verdict"] = verdict
        return SimpleNamespace(id="eval-row-phase7")

    monkeypatch.setattr(runner, "_persist_verdict", _fake_save)
    return captured


# ── Inputs ─────────────────────────────────────────────────────────────────

def _good_prompt() -> dict:
    return {"type": "enhanced_prompt", "content": "Validate the payer VPA before initiating the network debit; reject if checksum fails."}

def _bad_prompt() -> dict:
    return {"type": "enhanced_prompt", "content": "too short"}

def _good_research() -> dict:
    return {
        "type": "research_summary",
        "content": "the Authority procedures referenced in https://www.npci.org.in/specifications guide validation.",
    }

def _research_no_sources() -> dict:
    return {"type": "research_summary", "content": "Some prose with no citations and no urls."}

def _good_canvas() -> dict:
    return {
        "type": "product_canvas",
        "content": "## Problem\n...\n## Scope\nIn-scope: ...\n## Stakeholders\n- PSP\n- Bank",
    }

def _good_clarification() -> dict:
    return {
        "type": "clarification_thread",
        "questions": [
            {"id": "q1", "status": "answered"},
            {"id": "q2", "status": "answered"},
        ],
    }

def _pending_clarification() -> dict:
    return {
        "type": "clarification_thread",
        "questions": [{"id": "q1", "status": "pending"}, {"id": "q2", "status": "answered"}],
    }

def _good_brd() -> dict:
    return {
        "type": "brd",
        "content": (
            "## Background\n"
            "## Functional Requirements\n"
            "- FR-01: Validate payer VPA.\n"
            "## Compliance\n"
            "## Error Codes\n"
        ),
    }


# ── 1. Each Phase 7 checkpoint produces the expected verdict ───────────────

class TestRunAdvisoryForPhase7Checkpoints:
    @pytest.mark.asyncio
    async def test_initial_to_prompt_enhanced_passes_on_good_prompt(self, captured_save):
        await runner.run_advisory(
            db=_DummyDbSession(),
            change_request_id="cr-p7-1",
            checkpoint_id=CheckpointId.INITIAL_TO_PROMPT_ENHANCED,
            source_artifacts={"initial_prompt": {"content": "validate VPA"}},
            target_artifacts={"enhanced_prompt": _good_prompt()},
        )
        v = captured_save["verdict"]
        assert v.checkpoint_id == CheckpointId.INITIAL_TO_PROMPT_ENHANCED
        assert v.verdict == VerdictValue.PASS

    @pytest.mark.asyncio
    async def test_initial_to_prompt_enhanced_fails_on_short_prompt(self, captured_save):
        await runner.run_advisory(
            db=_DummyDbSession(),
            change_request_id="cr-p7-2",
            checkpoint_id=CheckpointId.INITIAL_TO_PROMPT_ENHANCED,
            source_artifacts={"initial_prompt": {"content": "x"}},
            target_artifacts={"enhanced_prompt": _bad_prompt()},
        )
        v = captured_save["verdict"]
        assert v.verdict in (VerdictValue.WARN, VerdictValue.FAIL)
        assert any("minimum" in r.lower() for r in v.reasons)

    @pytest.mark.asyncio
    async def test_prompt_to_research_passes_when_sources_present(self, captured_save):
        await runner.run_advisory(
            db=_DummyDbSession(),
            change_request_id="cr-p7-3",
            checkpoint_id=CheckpointId.PROMPT_TO_RESEARCH,
            source_artifacts={"enhanced_prompt": _good_prompt()},
            target_artifacts={"research_summary": _good_research()},
        )
        assert captured_save["verdict"].verdict == VerdictValue.PASS

    @pytest.mark.asyncio
    async def test_prompt_to_research_warns_or_fails_without_sources(self, captured_save):
        await runner.run_advisory(
            db=_DummyDbSession(),
            change_request_id="cr-p7-4",
            checkpoint_id=CheckpointId.PROMPT_TO_RESEARCH,
            source_artifacts={"enhanced_prompt": _good_prompt()},
            target_artifacts={"research_summary": _research_no_sources()},
        )
        v = captured_save["verdict"]
        assert v.verdict in (VerdictValue.WARN, VerdictValue.FAIL)
        assert any("source" in r.lower() for r in v.reasons)

    @pytest.mark.asyncio
    async def test_research_to_canvas_passes_on_required_sections(self, captured_save):
        await runner.run_advisory(
            db=_DummyDbSession(),
            change_request_id="cr-p7-5",
            checkpoint_id=CheckpointId.RESEARCH_TO_CANVAS,
            source_artifacts={"research_summary": _good_research()},
            target_artifacts={"product_canvas": _good_canvas()},
        )
        assert captured_save["verdict"].verdict == VerdictValue.PASS

    @pytest.mark.asyncio
    async def test_canvas_to_clarification_passes_on_terminal_thread(self, captured_save):
        await runner.run_advisory(
            db=_DummyDbSession(),
            change_request_id="cr-p7-6",
            checkpoint_id=CheckpointId.CANVAS_TO_CLARIFICATION,
            source_artifacts={"product_canvas": _good_canvas()},
            target_artifacts={"clarification_thread": _good_clarification()},
        )
        assert captured_save["verdict"].verdict == VerdictValue.PASS

    @pytest.mark.asyncio
    async def test_canvas_to_clarification_flags_pending_questions(self, captured_save):
        await runner.run_advisory(
            db=_DummyDbSession(),
            change_request_id="cr-p7-7",
            checkpoint_id=CheckpointId.CANVAS_TO_CLARIFICATION,
            source_artifacts={"product_canvas": _good_canvas()},
            target_artifacts={"clarification_thread": _pending_clarification()},
        )
        v = captured_save["verdict"]
        assert v.verdict in (VerdictValue.WARN, VerdictValue.FAIL)
        assert any("unresolved" in r.lower() or "pending" in r.lower() for r in v.reasons)

    @pytest.mark.asyncio
    async def test_clarification_to_brd_passes_on_good_brd(self, captured_save):
        await runner.run_advisory(
            db=_DummyDbSession(),
            change_request_id="cr-p7-8",
            checkpoint_id=CheckpointId.CLARIFICATION_TO_BRD,
            source_artifacts={
                "product_canvas":        _good_canvas(),
                "clarification_thread":  _good_clarification(),
            },
            target_artifacts={"brd_document": _good_brd()},
        )
        assert captured_save["verdict"].verdict == VerdictValue.PASS


# ── 2. fire_advisory_eval dispatches in both contexts ───────────────────────

class TestFireAdvisoryEvalDispatch:
    def test_sync_context_dispatches_in_thread(self, monkeypatch):
        """In a sync context, fire_advisory_eval must spawn a daemon thread
        that runs the advisory coroutine via asyncio.run."""
        calls: list[dict] = []

        async def _fake_isolated(**kwargs):
            calls.append(kwargs)

        monkeypatch.setattr(runner, "_run_with_isolated_session", _fake_isolated)

        runner.fire_advisory_eval(
            change_request_id="cr-sync",
            checkpoint_id=CheckpointId.CANVAS_TO_CLARIFICATION,
            source_artifacts={"product_canvas": {"content": "x"}},
            target_artifacts={"clarification_thread": {"questions": []}},
        )

        # Give the daemon thread a moment to run asyncio.run(...).
        deadline = 5.0
        import time
        start = time.time()
        while not calls and (time.time() - start) < deadline:
            time.sleep(0.05)

        assert calls, "fire_advisory_eval did not dispatch from sync context"
        assert calls[0]["checkpoint_id"] == CheckpointId.CANVAS_TO_CLARIFICATION
        assert calls[0]["change_request_id"] == "cr-sync"

    @pytest.mark.asyncio
    async def test_async_context_schedules_task(self, monkeypatch):
        """In an async context, fire_advisory_eval must schedule on the
        running loop without spawning a thread."""
        calls: list[dict] = []

        async def _fake_isolated(**kwargs):
            calls.append(kwargs)

        monkeypatch.setattr(runner, "_run_with_isolated_session", _fake_isolated)

        runner.fire_advisory_eval(
            change_request_id="cr-async",
            checkpoint_id=CheckpointId.PROMPT_TO_RESEARCH,
            source_artifacts={"enhanced_prompt": {"content": "x"}},
            target_artifacts={"research_summary": {"content": "y"}},
        )

        # Yield so the scheduled task runs.
        for _ in range(10):
            if calls:
                break
            await asyncio.sleep(0.01)

        assert calls, "fire_advisory_eval did not schedule a task in async context"
        assert calls[0]["checkpoint_id"] == CheckpointId.PROMPT_TO_RESEARCH
