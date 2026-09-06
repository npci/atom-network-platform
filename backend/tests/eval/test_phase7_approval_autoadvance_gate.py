# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 7 — auto-advance paths must honour the eval gate.

Three pre-existing code paths used to advance `cr.status` BRD -> TECH_SPEC
without consulting `_enforce_eval_gate_for_transition`:

  - submit_brd dev_skip_approvals shortcut
  - respond_approval when the last approver approves
  - dev_auto_approve_brd dev shortcut

After Phase 7 they all route through the non-raising helper
`_eval_gate_allows_transition`, which lets a failing gate refuse the
status promotion while leaving the BRD approved.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.api import agents
from app.models.change_request import ChangeStatus
from app.services.evaluation.checkpoints import CheckpointId, PolicyMode


@dataclass
class _FakeVerdict:
    id: str
    verdict: str
    hard_fail_codes: list[str] = field(default_factory=list)
    source_artifact_ids: list[str] = field(default_factory=list)
    target_artifact_ids: list[str] = field(default_factory=list)


class _DummyDb:
    """Sentinel — never touched because we stub policy/store reads."""


class TestEvalGateAllowsTransitionHelper:
    def test_returns_true_when_policy_is_advisory(self, monkeypatch):
        monkeypatch.setattr(agents, "get_policy_mode", lambda *a, **kw: PolicyMode.ADVISORY)
        monkeypatch.setattr(
            agents, "get_latest",
            lambda *a, **kw: _FakeVerdict(id="v", verdict="FAIL", hard_fail_codes=["X"]),
        )
        monkeypatch.setattr(agents, "count_runs", lambda *a, **kw: 1)

        allowed, info = agents._eval_gate_allows_transition(
            db=_DummyDb(),
            change_id="cr-1",
            current_status=ChangeStatus.BRD,
            next_status=ChangeStatus.TECH_SPEC,
        )
        assert allowed is True
        assert info is not None
        assert info["blocked"] is False

    def test_returns_false_when_hard_gate_fails(self, monkeypatch):
        monkeypatch.setattr(agents, "get_policy_mode", lambda *a, **kw: PolicyMode.HARD_GATE)
        monkeypatch.setattr(
            agents, "get_latest",
            lambda *a, **kw: _FakeVerdict(
                id="v-fail",
                verdict="FAIL",
                hard_fail_codes=["MISSING_MANDATORY_SECTION"],
            ),
        )
        monkeypatch.setattr(agents, "count_runs", lambda *a, **kw: 1)

        allowed, detail = agents._eval_gate_allows_transition(
            db=_DummyDb(),
            change_id="cr-2",
            current_status=ChangeStatus.BRD,
            next_status=ChangeStatus.TECH_SPEC,
        )
        assert allowed is False
        assert detail is not None
        assert detail["blocked"] is True
        assert detail["policy_mode"] == "hard_gate"
        assert detail["checkpoint_id"] == CheckpointId.CLARIFICATION_TO_BRD.value

    def test_returns_true_when_no_mapping_for_transition(self, monkeypatch):
        # Transitions not in _TRANSITION_GATE_CHECKPOINTS return None from
        # _enforce_eval_gate_for_transition; the helper translates that to
        # (True, None) so unrelated transitions are not blocked.
        allowed, info = agents._eval_gate_allows_transition(
            db=_DummyDb(),
            change_id="cr-3",
            current_status=ChangeStatus.PRODUCT_KIT,
            next_status=ChangeStatus.PROMPT_ENHANCEMENT,
        )
        assert allowed is True
        assert info is None


class TestApprovalAutoAdvanceRespectsGate:
    """Verify the three BRD auto-advance paths skip cr.status promotion
    when the gate would block, without raising errors."""

    @pytest.mark.parametrize("policy_mode,verdict,expected_allowed", [
        (PolicyMode.HARD_GATE, "FAIL", False),
        (PolicyMode.HARD_GATE, "WARN", False),
        (PolicyMode.HARD_GATE, "PASS", True),
        (PolicyMode.ADVISORY, "FAIL", True),
        (PolicyMode.SOFT_GATE, "WARN", False),  # WARN without ack blocks soft_gate
    ])
    def test_helper_matches_gate_policy(self, monkeypatch, policy_mode, verdict, expected_allowed):
        monkeypatch.setattr(agents, "get_policy_mode", lambda *a, **kw: policy_mode)
        monkeypatch.setattr(
            agents, "get_latest",
            lambda *a, **kw: _FakeVerdict(
                id="v-x",
                verdict=verdict,
                hard_fail_codes=["MISSING_REQUIRED_ARTIFACT"] if verdict == "FAIL" else [],
            ),
        )
        monkeypatch.setattr(agents, "count_runs", lambda *a, **kw: 1)

        allowed, _ = agents._eval_gate_allows_transition(
            db=_DummyDb(),
            change_id="cr-multi",
            current_status=ChangeStatus.BRD,
            next_status=ChangeStatus.TECH_SPEC,
        )
        assert allowed is expected_allowed, (
            f"policy={policy_mode.value} verdict={verdict} expected={expected_allowed} got={allowed}"
        )
