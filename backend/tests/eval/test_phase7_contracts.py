# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 7 — contract registration and catalog sanity tests.

Asserts that the five new Phase A checkpoints are registered, expose the
correct transition boundaries, and reference only hard-fail codes that are
present in the catalog. Runs fast: no DB, no LLM, no HTTP.
"""
import pytest

from app.services.evaluation.checkpoints import CheckpointId, PolicyMode
from app.services.evaluation.contracts import all_checkpoint_ids, get_contract
from app.services.evaluation.hard_fail_catalog import HARD_FAIL_CATALOG, get_hard_fail


PHASE7_CHECKPOINTS = {
    CheckpointId.INITIAL_TO_PROMPT_ENHANCED: ("prompt_enhancement", "research"),
    CheckpointId.PROMPT_TO_RESEARCH:         ("research",            "canvas"),
    CheckpointId.RESEARCH_TO_CANVAS:         ("canvas",              "clarification"),
    CheckpointId.CANVAS_TO_CLARIFICATION:    ("clarification",       "brd"),
    CheckpointId.CLARIFICATION_TO_BRD:       ("brd",                 "tech_spec"),
}

PHASE7_NEW_HARD_FAIL_CODES = [
    "PROMPT_TOO_SHORT",
    "NO_SOURCES_FOUND",
    "UNANSWERED_CRITICAL_QUESTION",
]


class TestPhase7Enum:
    def test_all_phase7_checkpoint_ids_exist(self):
        for cp in PHASE7_CHECKPOINTS:
            assert isinstance(cp, CheckpointId)

    def test_all_phase7_checkpoints_are_registered(self):
        registered = set(all_checkpoint_ids())
        for cp in PHASE7_CHECKPOINTS:
            assert cp in registered, f"{cp} missing from contract registry"


class TestPhase7Contracts:
    @pytest.mark.parametrize("checkpoint_id,boundary", list(PHASE7_CHECKPOINTS.items()))
    def test_contract_resolves_with_expected_boundary(self, checkpoint_id, boundary):
        contract = get_contract(checkpoint_id)
        from_stage, to_stage = boundary
        assert contract.from_stage == from_stage
        assert contract.to_stage == to_stage

    @pytest.mark.parametrize("checkpoint_id", list(PHASE7_CHECKPOINTS))
    def test_contract_starts_in_advisory(self, checkpoint_id):
        """Every Phase 7 checkpoint must ship in advisory mode; graduation is operational."""
        assert get_contract(checkpoint_id).policy_mode == PolicyMode.ADVISORY

    @pytest.mark.parametrize("checkpoint_id", list(PHASE7_CHECKPOINTS))
    def test_contract_has_at_least_one_rubric_dimension(self, checkpoint_id):
        assert len(get_contract(checkpoint_id).rubric_dimensions) >= 1

    @pytest.mark.parametrize("checkpoint_id", list(PHASE7_CHECKPOINTS))
    def test_contract_artifacts_non_empty(self, checkpoint_id):
        contract = get_contract(checkpoint_id)
        assert contract.required_source_artifacts, "source artifacts must not be empty"
        assert contract.required_target_artifacts, "target artifacts must not be empty"

    @pytest.mark.parametrize("checkpoint_id", list(PHASE7_CHECKPOINTS))
    def test_contract_hard_fail_codes_all_in_catalog(self, checkpoint_id):
        for code in get_contract(checkpoint_id).hard_fail_codes:
            assert code in HARD_FAIL_CATALOG, f"{code} referenced but not in catalog"

    @pytest.mark.parametrize("checkpoint_id", list(PHASE7_CHECKPOINTS))
    def test_contract_override_roles_non_empty(self, checkpoint_id):
        assert get_contract(checkpoint_id).override_allowed_roles

    def test_rubric_weights_sum_to_about_one(self):
        for checkpoint_id in PHASE7_CHECKPOINTS:
            total = sum(d.weight for d in get_contract(checkpoint_id).rubric_dimensions)
            assert 0.99 <= total <= 1.01, f"{checkpoint_id} weights sum to {total:.3f}"


class TestPhase7HardFailCatalog:
    @pytest.mark.parametrize("code", PHASE7_NEW_HARD_FAIL_CODES)
    def test_new_code_exists(self, code):
        entry = get_hard_fail(code)
        assert entry.code == code
        assert entry.title
        assert entry.meaning
        assert entry.remediation
