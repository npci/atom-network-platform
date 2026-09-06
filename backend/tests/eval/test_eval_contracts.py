# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 0 unit tests — contract schemas, hard-fail catalog, policy modes.

Rules:
- No external services.
- No LLM calls.
- No DB.
- Must run fast with: pytest backend/tests/eval/test_eval_contracts.py
"""
import pytest
from pydantic import ValidationError

from app.services.evaluation.checkpoints import (
    CheckpointId,
    FIRST_WAVE_CHECKPOINTS,
    PolicyMode,
    VerdictValue,
)
from app.services.evaluation.contracts import (
    all_checkpoint_ids,
    all_contracts,
    get_contract,
)
from app.services.evaluation.hard_fail_catalog import (
    HARD_FAIL_CATALOG,
    all_codes,
    get_hard_fail,
)
from app.services.evaluation.schemas import (
    CheckpointContract,
    EvalVerdict,
    RubricDimension,
)


# ── Hard-fail catalog ─────────────────────────────────────────────────────────

class TestHardFailCatalog:
    def test_all_expected_codes_exist(self):
        expected = {
            "MISSING_REQUIRED_ARTIFACT",
            "EMPTY_OR_PLACEHOLDER_CONTENT",
            "MISSING_MANDATORY_SECTION",
            "UNMAPPED_REQUIREMENT",
            "CONTRADICTS_APPROVED_SOURCE",
            "INVALID_UPI_ERROR_PATTERN",
            "XSD_DECISION_MISMATCH",
            "INCOMPLETE_PRODUCT_KIT",
            "UNSAFE_PARTNER_RESPONSE",
        }
        assert expected.issubset(set(all_codes()))

    def test_no_duplicate_codes(self):
        codes = all_codes()
        assert len(codes) == len(set(codes))

    def test_get_known_code(self):
        entry = get_hard_fail("MISSING_REQUIRED_ARTIFACT")
        assert entry.code == "MISSING_REQUIRED_ARTIFACT"
        assert entry.title
        assert entry.meaning
        assert entry.example_evidence
        assert entry.remediation

    def test_get_unknown_code_raises(self):
        with pytest.raises(KeyError, match="Unknown hard-fail code"):
            get_hard_fail("THIS_DOES_NOT_EXIST")

    def test_all_entries_have_complete_fields(self):
        for code, entry in HARD_FAIL_CATALOG.items():
            assert entry.code == code
            assert entry.title, f"{code}: title is empty"
            assert entry.meaning, f"{code}: meaning is empty"
            assert entry.example_evidence, f"{code}: example_evidence is empty"
            assert entry.remediation, f"{code}: remediation is empty"


# ── Policy modes and enums ────────────────────────────────────────────────────

class TestEnums:
    def test_all_policy_modes_exist(self):
        assert PolicyMode.DISABLED == "disabled"
        assert PolicyMode.ADVISORY == "advisory"
        assert PolicyMode.SOFT_GATE == "soft_gate"
        assert PolicyMode.HARD_GATE == "hard_gate"

    def test_unknown_policy_mode_raises(self):
        with pytest.raises(ValidationError):
            CheckpointContract(
                checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
                display_name="Test",
                description="Test",
                from_stage="brd",
                to_stage="tech_spec",
                policy_mode="flying_mode",   # invalid
                rubric_version="v1",
                required_source_artifacts=["brd"],
                required_target_artifacts=["tsd"],
                rubric_dimensions=[
                    RubricDimension(id="dim1", name="D", description="d", weight=1.0, minimum_score=0.5)
                ],
            )

    def test_verdict_values(self):
        assert VerdictValue.PASS == "PASS"
        assert VerdictValue.WARN == "WARN"
        assert VerdictValue.FAIL == "FAIL"

    def test_all_first_wave_checkpoints_are_valid_ids(self):
        valid_ids = set(CheckpointId)
        for cp in FIRST_WAVE_CHECKPOINTS:
            assert cp in valid_ids


# ── Contract registry ─────────────────────────────────────────────────────────

class TestContractRegistry:
    def test_all_first_wave_contracts_registered(self):
        registered = set(all_checkpoint_ids())
        for cp in FIRST_WAVE_CHECKPOINTS:
            assert cp in registered, f"First-wave checkpoint {cp} has no contract"

    def test_get_contract_returns_correct_type(self):
        contract = get_contract(CheckpointId.BRD_TO_TECH_SPEC)
        assert isinstance(contract, CheckpointContract)
        assert contract.checkpoint_id == CheckpointId.BRD_TO_TECH_SPEC

    def test_get_contract_by_string(self):
        contract = get_contract("brd_to_tech_spec")
        assert contract.checkpoint_id == CheckpointId.BRD_TO_TECH_SPEC

    def test_get_unknown_contract_raises(self):
        with pytest.raises(KeyError):
            get_contract("completely_made_up_checkpoint")

    def test_no_duplicate_contracts(self):
        ids = all_checkpoint_ids()
        assert len(ids) == len(set(ids))


# ── Individual contract correctness ──────────────────────────────────────────

class TestContractContents:
    @pytest.mark.parametrize("cp_id", FIRST_WAVE_CHECKPOINTS)
    def test_contract_has_required_source_artifacts(self, cp_id):
        c = get_contract(cp_id)
        assert c.required_source_artifacts, f"{cp_id}: required_source_artifacts is empty"

    @pytest.mark.parametrize("cp_id", FIRST_WAVE_CHECKPOINTS)
    def test_contract_has_required_target_artifacts(self, cp_id):
        c = get_contract(cp_id)
        assert c.required_target_artifacts, f"{cp_id}: required_target_artifacts is empty"

    @pytest.mark.parametrize("cp_id", FIRST_WAVE_CHECKPOINTS)
    def test_contract_hard_fail_codes_in_catalog(self, cp_id):
        c = get_contract(cp_id)
        for code in c.hard_fail_codes:
            assert code in HARD_FAIL_CATALOG, f"{cp_id}: hard_fail code '{code}' not in catalog"

    @pytest.mark.parametrize("cp_id", FIRST_WAVE_CHECKPOINTS)
    def test_contract_initial_policy_is_advisory(self, cp_id):
        c = get_contract(cp_id)
        assert c.policy_mode in (PolicyMode.ADVISORY, PolicyMode.DISABLED), (
            f"{cp_id}: initial policy should be advisory or disabled, not {c.policy_mode}"
        )

    @pytest.mark.parametrize("cp_id", FIRST_WAVE_CHECKPOINTS)
    def test_contract_has_rubric_dimensions(self, cp_id):
        c = get_contract(cp_id)
        assert c.rubric_dimensions, f"{cp_id}: rubric_dimensions is empty"

    @pytest.mark.parametrize("cp_id", FIRST_WAVE_CHECKPOINTS)
    def test_rubric_dimension_weights_non_negative(self, cp_id):
        c = get_contract(cp_id)
        for dim in c.rubric_dimensions:
            assert dim.weight >= 0, f"{cp_id}/{dim.id}: weight is negative"

    @pytest.mark.parametrize("cp_id", FIRST_WAVE_CHECKPOINTS)
    def test_rubric_dimension_scores_in_range(self, cp_id):
        c = get_contract(cp_id)
        for dim in c.rubric_dimensions:
            assert 0.0 <= dim.minimum_score <= 1.0, (
                f"{cp_id}/{dim.id}: minimum_score {dim.minimum_score} out of [0,1]"
            )

    @pytest.mark.parametrize("cp_id", FIRST_WAVE_CHECKPOINTS)
    def test_rubric_dimension_ids_are_unique(self, cp_id):
        c = get_contract(cp_id)
        ids = [d.id for d in c.rubric_dimensions]
        assert len(ids) == len(set(ids)), f"{cp_id}: duplicate rubric dimension ids"


# ── Verdict schema ────────────────────────────────────────────────────────────

class TestVerdictSchema:
    def _minimal_pass(self, **overrides) -> dict:
        base = dict(
            checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
            verdict=VerdictValue.PASS,
            passed=True,
            policy_mode=PolicyMode.ADVISORY,
            confidence=0.9,
            reasons=["All requirements covered."],
            rubric_version="eval-harness.phase0.v1",
            deterministic_version="deterministic.v1",
        )
        base.update(overrides)
        return base

    def test_valid_pass_verdict(self):
        v = EvalVerdict(**self._minimal_pass())
        assert v.verdict == VerdictValue.PASS
        assert v.passed is True

    def test_valid_fail_verdict(self):
        v = EvalVerdict(**self._minimal_pass(
            verdict=VerdictValue.FAIL,
            passed=False,
            reasons=["Missing mandatory section: Error Code Table."],
            hard_fail_codes=["MISSING_MANDATORY_SECTION"],
        ))
        assert v.verdict == VerdictValue.FAIL
        assert v.passed is False

    def test_empty_reasons_rejected(self):
        with pytest.raises(ValidationError, match="reasons"):
            EvalVerdict(**self._minimal_pass(reasons=[]))

    def test_pass_with_passed_false_rejected(self):
        with pytest.raises(ValidationError, match="inconsistent"):
            EvalVerdict(**self._minimal_pass(verdict=VerdictValue.PASS, passed=False))

    def test_fail_with_passed_true_rejected(self):
        with pytest.raises(ValidationError, match="inconsistent"):
            EvalVerdict(**self._minimal_pass(
                verdict=VerdictValue.FAIL,
                passed=True,
                reasons=["Something failed."],
            ))

    def test_verdict_has_reproducibility_fields(self):
        v = EvalVerdict(**self._minimal_pass())
        assert v.rubric_version
        assert v.deterministic_version
        assert v.checkpoint_id
        assert v.created_at is not None


# ── Schema validation edge cases ─────────────────────────────────────────────

class TestSchemaValidation:
    def test_contract_rejects_unknown_hard_fail_code(self):
        with pytest.raises(ValidationError, match="not in hard_fail_catalog"):
            CheckpointContract(
                checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
                display_name="Test",
                description="Test",
                from_stage="brd",
                to_stage="tech_spec",
                policy_mode=PolicyMode.ADVISORY,
                rubric_version="v1",
                required_source_artifacts=["brd"],
                required_target_artifacts=["tsd"],
                rubric_dimensions=[
                    RubricDimension(id="d1", name="D", description="d", weight=1.0, minimum_score=0.5)
                ],
                hard_fail_codes=["CODE_THAT_DOES_NOT_EXIST"],
            )

    def test_contract_rejects_empty_source_artifacts(self):
        with pytest.raises(ValidationError):
            CheckpointContract(
                checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
                display_name="Test",
                description="Test",
                from_stage="brd",
                to_stage="tech_spec",
                policy_mode=PolicyMode.ADVISORY,
                rubric_version="v1",
                required_source_artifacts=[],   # empty — should fail
                required_target_artifacts=["tsd"],
                rubric_dimensions=[
                    RubricDimension(id="d1", name="D", description="d", weight=1.0, minimum_score=0.5)
                ],
            )

    def test_rubric_dimension_rejects_space_in_id(self):
        with pytest.raises(ValidationError, match="spaces"):
            RubricDimension(
                id="bad id with spaces",
                name="Bad",
                description="d",
                weight=1.0,
                minimum_score=0.5,
            )

    def test_rubric_dimension_rejects_score_above_one(self):
        with pytest.raises(ValidationError):
            RubricDimension(id="d1", name="D", description="d", weight=1.0, minimum_score=1.5)

    def test_rubric_dimension_rejects_negative_weight(self):
        with pytest.raises(ValidationError):
            RubricDimension(id="d1", name="D", description="d", weight=-0.1, minimum_score=0.5)
