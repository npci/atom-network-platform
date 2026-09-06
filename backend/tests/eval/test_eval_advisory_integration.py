# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 1 advisory runner integration tests (no external services)."""
from __future__ import annotations

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
        return SimpleNamespace(id="eval-row-1")

    monkeypatch.setattr(runner, "_persist_verdict", _fake_save)
    return captured


def _valid_brd() -> dict:
    return {
        "type": "brd",
        "content": (
            "## Background\n"
            "## Functional Requirements\n"
            "- FR-01: Validate payer VPA.\n"
            "## Compliance\n"
        ),
    }


def _valid_tech_spec() -> dict:
    return {
        "type": "tech_spec",
        "content": (
            "## Overview\n"
            "## Functional Requirements\n"
            "FR-01 is implemented as documented in the API section below.\n"
            "## API\n"
            "## Error Code Table\n"
            "| code | meaning |\n"
            "| U30  | VPA not registered |\n"
        ),
    }


class TestAdvisoryRunner:
    @pytest.mark.asyncio
    async def test_run_advisory_stores_pass_verdict(self, captured_save):
        result = await runner.run_advisory(
            db=_DummyDbSession(),
            change_request_id="cr-pass",
            checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
            source_artifacts={"brd_document": _valid_brd()},
            target_artifacts={"tech_spec_document": _valid_tech_spec()},
            source_artifact_ids=["brd-1"],
            target_artifact_ids=["ts-1"],
        )

        assert result is not None
        assert captured_save["change_request_id"] == "cr-pass"
        assert captured_save["verdict"].verdict == VerdictValue.PASS
        assert captured_save["verdict"].passed is True

    @pytest.mark.asyncio
    async def test_missing_required_artifact_records_fail(self, captured_save):
        await runner.run_advisory(
            db=_DummyDbSession(),
            change_request_id="cr-missing",
            checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
            source_artifacts={},
            target_artifacts={"tech_spec_document": _valid_tech_spec()},
        )

        verdict = captured_save["verdict"]
        assert verdict.verdict == VerdictValue.FAIL
        assert verdict.passed is False
        assert "MISSING_REQUIRED_ARTIFACT" in verdict.hard_fail_codes

    @pytest.mark.asyncio
    async def test_check_errors_are_warn_and_non_blocking(self, monkeypatch, captured_save):
        monkeypatch.setattr(
            runner,
            "run_checks",
            lambda check_names, artifacts: ["CHECK_ERROR: 'x' raised RuntimeError: boom"],
        )

        await runner.run_advisory(
            db=_DummyDbSession(),
            change_request_id="cr-warn",
            checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
            source_artifacts={"brd_document": _valid_brd()},
            target_artifacts={"tech_spec_document": _valid_tech_spec()},
        )

        verdict = captured_save["verdict"]
        assert verdict.verdict == VerdictValue.WARN
        assert verdict.passed is True
        assert "CHECK_EXECUTION_ERROR" in verdict.warn_codes
