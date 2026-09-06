# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 7 — gate enforcement mapping tests.

Slice 4 plugs five new (from_status, to_status) -> checkpoint_id rows into
`_TRANSITION_GATE_CHECKPOINTS`. These tests assert the mapping is correct
and that decide_gate produces the right block/allow outcomes for each new
checkpoint across advisory / soft_gate / hard_gate modes.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.api.agents import _TRANSITION_GATE_CHECKPOINTS
from app.models.change_request import ChangeStatus
from app.services.evaluation.checkpoints import CheckpointId, PolicyMode
from app.services.evaluation.policy import decide_gate


PHASE7_TRANSITIONS = {
    (ChangeStatus.PROMPT_ENHANCEMENT, ChangeStatus.RESEARCH):      CheckpointId.INITIAL_TO_PROMPT_ENHANCED,
    (ChangeStatus.RESEARCH,           ChangeStatus.CANVAS):        CheckpointId.PROMPT_TO_RESEARCH,
    (ChangeStatus.CANVAS,             ChangeStatus.CLARIFICATION): CheckpointId.RESEARCH_TO_CANVAS,
    (ChangeStatus.CLARIFICATION,      ChangeStatus.BRD):           CheckpointId.CANVAS_TO_CLARIFICATION,
    (ChangeStatus.BRD,                ChangeStatus.TECH_SPEC):     CheckpointId.CLARIFICATION_TO_BRD,
}


@dataclass
class _FakeVerdict:
    id: str
    verdict: str
    hard_fail_codes: list[str] = field(default_factory=list)
    source_artifact_ids: list[str] = field(default_factory=list)
    target_artifact_ids: list[str] = field(default_factory=list)


class TestPhase7TransitionMapping:
    @pytest.mark.parametrize("transition,expected_checkpoint", list(PHASE7_TRANSITIONS.items()))
    def test_transition_maps_to_expected_checkpoint(self, transition, expected_checkpoint):
        assert _TRANSITION_GATE_CHECKPOINTS.get(transition) == expected_checkpoint

    def test_phase2_first_wave_still_mapped(self):
        """Slice 4 must not regress the original three gated transitions."""
        assert _TRANSITION_GATE_CHECKPOINTS[(ChangeStatus.TECH_SPEC, ChangeStatus.XSD)] == CheckpointId.BRD_TO_TECH_SPEC
        assert _TRANSITION_GATE_CHECKPOINTS[(ChangeStatus.XSD, ChangeStatus.PRODUCT_KIT)] == CheckpointId.TECH_SPEC_TO_XSD

    def test_unmapped_transitions_return_none(self):
        # Phase B / non-Phase-A transitions are deliberately not gated here.
        assert _TRANSITION_GATE_CHECKPOINTS.get((ChangeStatus.PRODUCT_KIT, ChangeStatus.PROMPT_ENHANCEMENT)) is None


class TestPhase7GateDecisionPerCheckpoint:
    @pytest.mark.parametrize("checkpoint_id", list(PHASE7_TRANSITIONS.values()))
    def test_advisory_never_blocks(self, checkpoint_id):
        decision = decide_gate(
            checkpoint_id=checkpoint_id,
            policy_mode=PolicyMode.ADVISORY,
            verdict=_FakeVerdict(id="v-fail", verdict="FAIL", hard_fail_codes=["MISSING_REQUIRED_ARTIFACT"]),
            retry_allowed=False,
            retries_used=0,
            override_allowed=False,
        )
        assert decision.blocked is False

    @pytest.mark.parametrize("checkpoint_id", list(PHASE7_TRANSITIONS.values()))
    def test_hard_gate_blocks_on_fail(self, checkpoint_id):
        decision = decide_gate(
            checkpoint_id=checkpoint_id,
            policy_mode=PolicyMode.HARD_GATE,
            verdict=_FakeVerdict(id="v-fail", verdict="FAIL", hard_fail_codes=["MISSING_REQUIRED_ARTIFACT"]),
            retry_allowed=False,
            retries_used=0,
            override_allowed=False,
        )
        assert decision.blocked is True

    @pytest.mark.parametrize("checkpoint_id", list(PHASE7_TRANSITIONS.values()))
    def test_hard_gate_passes_on_pass_verdict(self, checkpoint_id):
        decision = decide_gate(
            checkpoint_id=checkpoint_id,
            policy_mode=PolicyMode.HARD_GATE,
            verdict=_FakeVerdict(id="v-pass", verdict="PASS"),
        )
        assert decision.blocked is False

    @pytest.mark.parametrize("checkpoint_id", list(PHASE7_TRANSITIONS.values()))
    def test_hard_gate_blocks_on_warn(self, checkpoint_id):
        decision = decide_gate(
            checkpoint_id=checkpoint_id,
            policy_mode=PolicyMode.HARD_GATE,
            verdict=_FakeVerdict(id="v-warn", verdict="WARN"),
            override_allowed=True,
        )
        assert decision.blocked is True
        assert decision.override_allowed is True

    @pytest.mark.parametrize("checkpoint_id", list(PHASE7_TRANSITIONS.values()))
    def test_soft_gate_requires_ack_on_warn(self, checkpoint_id):
        verdict = _FakeVerdict(id="v-warn", verdict="WARN")
        blocked = decide_gate(
            checkpoint_id=checkpoint_id,
            policy_mode=PolicyMode.SOFT_GATE,
            verdict=verdict,
            acknowledged_verdict_id=None,
        )
        assert blocked.blocked is True
        assert blocked.requires_ack is True

        allowed = decide_gate(
            checkpoint_id=checkpoint_id,
            policy_mode=PolicyMode.SOFT_GATE,
            verdict=verdict,
            acknowledged_verdict_id="v-warn",
        )
        assert allowed.blocked is False

    @pytest.mark.parametrize("checkpoint_id", list(PHASE7_TRANSITIONS.values()))
    def test_hard_gate_allows_override_when_configured(self, checkpoint_id):
        decision = decide_gate(
            checkpoint_id=checkpoint_id,
            policy_mode=PolicyMode.HARD_GATE,
            verdict=_FakeVerdict(id="v-fail", verdict="FAIL", hard_fail_codes=["MISSING_REQUIRED_ARTIFACT"]),
            retry_allowed=False,
            retries_used=0,
            override_allowed=True,
        )
        assert decision.blocked is True
        assert decision.override_allowed is True
