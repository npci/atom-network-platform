# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 2 gate-policy tests (soft/hard gate + retry cap)."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.services.evaluation.checkpoints import CheckpointId, PolicyMode
from app.services.evaluation.judge import extract_hard_fail_codes
from app.services.evaluation.policy import decide_gate, get_policy_mode


@dataclass
class _FakeVerdict:
    id: str
    verdict: str
    hard_fail_codes: list[str] = field(default_factory=list)
    source_artifact_ids: list[str] = field(default_factory=list)
    target_artifact_ids: list[str] = field(default_factory=list)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _DbWithPolicy:
    def __init__(self, value):
        self.value = value

    def execute(self, *_args, **_kwargs):
        return _ScalarResult(self.value)


class TestPhase2PolicyModeAndGates:
    def test_pass_continues_in_hard_gate_mode(self):
        decision = decide_gate(
            checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
            policy_mode=PolicyMode.HARD_GATE,
            verdict=_FakeVerdict(id="v1", verdict="PASS"),
        )
        assert decision.blocked is False

    def test_warn_continues_in_advisory_mode(self):
        decision = decide_gate(
            checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
            policy_mode=PolicyMode.ADVISORY,
            verdict=_FakeVerdict(id="v1", verdict="WARN"),
        )
        assert decision.blocked is False

    def test_warn_requires_ack_in_soft_gate_mode(self):
        verdict = _FakeVerdict(id="v-warn", verdict="WARN")
        blocked = decide_gate(
            checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
            policy_mode=PolicyMode.SOFT_GATE,
            verdict=verdict,
            acknowledged_verdict_id=None,
        )
        assert blocked.blocked is True
        assert blocked.requires_ack is True
        assert blocked.required_ack_verdict_id == "v-warn"

        allowed = decide_gate(
            checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
            policy_mode=PolicyMode.SOFT_GATE,
            verdict=verdict,
            acknowledged_verdict_id="v-warn",
        )
        assert allowed.blocked is False

    def test_warn_blocks_in_hard_gate_mode(self):
        decision = decide_gate(
            checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
            policy_mode=PolicyMode.HARD_GATE,
            verdict=_FakeVerdict(id="v-warn", verdict="WARN"),
            override_allowed=True,
        )
        assert decision.blocked is True
        assert decision.override_allowed is True

    def test_fail_blocks_in_hard_gate_mode(self):
        decision = decide_gate(
            checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
            policy_mode=PolicyMode.HARD_GATE,
            verdict=_FakeVerdict(id="v-fail", verdict="FAIL", hard_fail_codes=["MISSING_MANDATORY_SECTION"]),
            retry_allowed=False,
            retries_used=0,
            override_allowed=False,
        )
        assert decision.blocked is True
        assert decision.retry_available is False

    def test_retry_cap_is_one_attempt(self):
        verdict = _FakeVerdict(id="v-fail", verdict="FAIL")
        first_fail = decide_gate(
            checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
            policy_mode=PolicyMode.HARD_GATE,
            verdict=verdict,
            retry_allowed=True,
            retries_used=0,
            override_allowed=False,
        )
        assert first_fail.retry_available is True

        second_fail = decide_gate(
            checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
            policy_mode=PolicyMode.HARD_GATE,
            verdict=verdict,
            retry_allowed=True,
            retries_used=1,
            override_allowed=False,
        )
        assert second_fail.retry_available is False

    def test_policy_mode_reads_checkpoint_override(self):
        db = _DbWithPolicy("hard_gate")
        mode = get_policy_mode(db, CheckpointId.BRD_TO_TECH_SPEC)
        assert mode == PolicyMode.HARD_GATE

    def test_hard_fail_code_maps_prompt_too_short(self):
        codes = extract_hard_fail_codes(
            ["Enhanced prompt is only 4 characters; minimum is 40 for the research stage to operate."],
            ["MISSING_REQUIRED_ARTIFACT", "EMPTY_OR_PLACEHOLDER_CONTENT", "PROMPT_TOO_SHORT"],
        )
        assert codes == ["PROMPT_TOO_SHORT"]

    def test_hard_fail_code_maps_placeholder_content(self):
        codes = extract_hard_fail_codes(
            ["Placeholder pattern detected: '\\bTODO\\b'. Remove or replace before advancing."],
            ["MISSING_REQUIRED_ARTIFACT", "EMPTY_OR_PLACEHOLDER_CONTENT", "PROMPT_TOO_SHORT"],
        )
        assert codes == ["EMPTY_OR_PLACEHOLDER_CONTENT"]
