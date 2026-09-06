# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""PII Tier 2 — per-`A2ATaskType` design-time PII classification.

Tier 1 (`test_pii_redaction_profiles.py`) filters content heuristically. Its
inherent weakness is that it can only catch what its regexes match. Tier 2
addresses that by tagging the CONTRACT: a message type known by design to carry
account/transaction detail gets mandatory filtering even when the heuristic
finds nothing.

The most valuable test here is `test_every_task_type_has_a_pii_rationale`. It is
the mechanism that stops this classification from silently rotting: adding a
message to `A2ATaskType` without deciding whether it carries PII fails CI,
rather than quietly inheriting `carries_pii=False`. Default-by-omission is
exactly how a classification table becomes fiction.
"""
from __future__ import annotations

import importlib.util
import sys
import types

import pytest

pytest.importorskip("pydantic")


def _load_protocol():
    """Import `app.a2a_common.protocol` WITHOUT executing the package's
    `__init__.py`, which imports the optional `a2a` SDK (not installed in every
    environment — `tests/a2a_common/*` use `importorskip("a2a")` for that
    reason). `protocol.py` itself has no SDK dependency; it is pure contract
    metadata, so skipping this whole module when the SDK is absent would lose
    the classification-completeness guard exactly where it matters least.
    """
    name = "app.a2a_common.protocol"
    if name in sys.modules:
        return sys.modules[name]
    if "app.a2a_common" not in sys.modules:
        pkg = types.ModuleType("app.a2a_common")
        pkg.__path__ = ["app/a2a_common"]
        sys.modules["app.a2a_common"] = pkg
    spec = importlib.util.spec_from_file_location(name, "app/a2a_common/protocol.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module          # register before exec: @dataclass needs it
    spec.loader.exec_module(module)
    return module


_proto = _load_protocol()
MESSAGES = _proto.MESSAGES
PII_BEARING_TASK_TYPES = _proto.PII_BEARING_TASK_TYPES
PII_CLASSIFICATION_RATIONALE = _proto.PII_CLASSIFICATION_RATIONALE
A2ATaskType = _proto.A2ATaskType
carries_pii = _proto.carries_pii

from app.core.pii_redaction import redact_a2a_payload_for_llm   # noqa: E402


class TestClassificationIsComplete:
    def test_every_task_type_has_a_pii_rationale(self):
        """THE guard. A new message type must not inherit "no PII" by silence.

        If this fails, add an entry to `PII_CLASSIFICATION_RATIONALE` recording
        what the message is designed to carry — and set `carries_pii=True` on
        its `_spec(...)` if that content can include a consumer's PII.
        """
        missing = [tt.value for tt in A2ATaskType if tt not in PII_CLASSIFICATION_RATIONALE]
        assert not missing, (
            f"task type(s) with no PII classification rationale: {missing}. "
            "Classify them in protocol.PII_CLASSIFICATION_RATIONALE — do not "
            "let a new message type default to carries_pii=False by omission."
        )

    def test_every_task_type_has_a_message_spec(self):
        missing = [tt.value for tt in A2ATaskType if tt not in MESSAGES]
        assert not missing, f"task type(s) with no MessageSpec: {missing}"

    def test_rationale_has_no_entries_for_unknown_task_types(self):
        """Catches a rationale left behind after a task type was renamed."""
        known = set(A2ATaskType)
        stale = [tt for tt in PII_CLASSIFICATION_RATIONALE if tt not in known]
        assert not stale, f"stale rationale entries: {stale}"

    def test_rationales_are_substantive(self):
        """A rationale must actually say something — an empty or placeholder
        string would satisfy the completeness test while conveying nothing."""
        weak = [tt.value for tt, why in PII_CLASSIFICATION_RATIONALE.items()
                if not why or len(why.strip()) < 20]
        assert not weak, f"task type(s) with a non-substantive rationale: {weak}"


class TestSpecAndSetAgree:
    """`MessageSpec.carries_pii` and `PII_BEARING_TASK_TYPES` are two
    representations of one decision; they must never diverge."""

    def test_spec_flag_matches_the_set(self):
        mismatched = [tt.value for tt, spec in MESSAGES.items()
                      if spec.carries_pii != (tt in PII_BEARING_TASK_TYPES)]
        assert not mismatched, (
            f"carries_pii disagrees with PII_BEARING_TASK_TYPES for: {mismatched}")

    def test_carries_pii_helper_matches_the_spec(self):
        for tt, spec in MESSAGES.items():
            assert carries_pii(tt) is spec.carries_pii, tt.value


class TestKnownClassifications:
    """Spot-checks with a stated reason, so a future edit that flips one of
    these has to justify itself against the reasoning, not just a boolean."""

    @pytest.mark.parametrize("task_type", [
        A2ATaskType.CERT_CASE_RESULT,      # real network switch traffic
        A2ATaskType.COUNTER_PROPOSAL,      # partner free-text justification
        A2ATaskType.QUERY,                 # partner free-text question
        A2ATaskType.BLOCKER,               # partner describes a failing txn
        A2ATaskType.CHANGE_COMMUNICATION,  # carries BRD/TSD content
    ])
    def test_pii_bearing_types(self, task_type):
        assert carries_pii(task_type) is True

    @pytest.mark.parametrize("task_type", [
        A2ATaskType.ECHO,                     # connectivity probe
        A2ATaskType.PROPOSAL_ACKNOWLEDGED,    # receipt: ids only
        A2ATaskType.ROUND_OPENED,             # round number + deadline
        A2ATaskType.CERT_STATUS_REQUEST,      # identifiers only
        A2ATaskType.MILESTONE_UPDATE,         # enum + timestamp
    ])
    def test_non_pii_types(self, task_type):
        assert carries_pii(task_type) is False


class TestFailsClosed:
    def test_unknown_wire_string_is_treated_as_pii_bearing(self):
        """An unclassified protocol addition, or a malformed inbound task_type,
        must be filtered rather than trusted."""
        assert carries_pii("some_task_type_nobody_classified") is True

    def test_accepts_raw_wire_strings(self):
        """Inbound callers hold the string form straight off the envelope."""
        assert carries_pii("counter_proposal") is True
        assert carries_pii("echo") is False


class TestPayloadRedaction:
    def test_redacts_nested_pii_in_a_pii_bearing_payload(self):
        payload = {
            "payload": {
                "test_case_id": "TC-1",
                "request": {"payerVpa": "ram.kumar@okhdfcbank",
                            "mobile": "9876543210"},
                "response": {"account": "123456789012"},
            },
        }
        out, count = redact_a2a_payload_for_llm(payload, A2ATaskType.CERT_CASE_RESULT)
        assert count == 3
        flat = str(out)
        for leaked in ("ram.kumar@okhdfcbank", "9876543210", "123456789012"):
            assert leaked not in flat

    def test_preserves_non_string_contract_values(self):
        """Ints/bools are contract values — timeouts, attempt counters, flags.
        Same reasoning as PROFILE_DOC's: a privacy control must not corrupt
        machine-readable content."""
        payload = {"timeout_ms": 30000, "attempt": 2, "ok": True,
                   "amount": 10000, "ratio": 1.5, "nothing": None}
        out, count = redact_a2a_payload_for_llm(payload, A2ATaskType.CERT_CASE_RESULT)
        assert count == 0
        assert out == payload

    def test_does_not_mutate_the_input(self):
        payload = {"payload": {"vpa": "ram.kumar@okhdfcbank"}}
        original = payload["payload"]["vpa"]
        redact_a2a_payload_for_llm(payload, A2ATaskType.CERT_CASE_RESULT)
        assert payload["payload"]["vpa"] == original

    def test_non_pii_task_type_is_passed_through_untouched(self):
        payload = {"ping": "9876543210"}
        out, count = redact_a2a_payload_for_llm(payload, A2ATaskType.ECHO)
        assert count == 0
        assert out == payload

    def test_unknown_task_type_is_redacted(self):
        out, count = redact_a2a_payload_for_llm({"x": "call 9876543210"}, "brand_new")
        assert count == 1
        assert "9876543210" not in str(out)

    def test_none_payload_is_safe(self):
        assert redact_a2a_payload_for_llm(None, A2ATaskType.CERT_CASE_RESULT) == (None, 0)

    def test_walks_lists(self):
        payload = {"cases": [{"vpa": "a.b@okaxis"}, {"vpa": "c.d@okicici"}]}
        out, count = redact_a2a_payload_for_llm(payload, A2ATaskType.CERT_CASE_RESULT)
        assert count == 2
        assert "okaxis" not in str(out) and "okicici" not in str(out)


def _negotiation_classifier_importable() -> bool:
    """`negotiation_classifier` transitively imports `core.database`, which calls
    `create_engine` with Postgres-only pool kwargs at import time — rejected by
    SQLite. Pre-existing environment limitation (see
    tests/agents/test_workspace_secret_scrub.py for the same guard); these tests
    run in CI where DATABASE_URL is Postgres.
    """
    try:
        import app.agents.negotiation_classifier  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _negotiation_classifier_importable(),
                    reason="negotiation_classifier needs a Postgres DATABASE_URL "
                           "(pre-existing create_engine limitation)")
class TestMandatoryOverridesTheHeuristicFlag:
    """The core Tier 2 property: `pii_redaction_freetext_enabled` tunes the
    HEURISTIC filter. It must not disable filtering for a message type
    classified as PII-bearing by design."""

    def test_redaction_still_applies_when_the_general_flag_is_off(self, monkeypatch):
        from app.agents import negotiation_classifier as nc
        from app.core.config import settings

        monkeypatch.setitem(settings.__dict__, "pii_redaction_freetext_enabled", False)

        text = "customer 9876543210 disputes this"
        # No task type -> respects the flag (legacy behaviour preserved).
        assert nc._redact_partner_text(text, field_name="f") == text
        # PII-bearing task type -> filtered anyway.
        guarded = nc._redact_partner_text(
            text, field_name="f", task_type=A2ATaskType.COUNTER_PROPOSAL)
        assert "9876543210" not in guarded

    def test_non_pii_task_type_still_respects_the_flag(self, monkeypatch):
        from app.agents import negotiation_classifier as nc
        from app.core.config import settings

        monkeypatch.setitem(settings.__dict__, "pii_redaction_freetext_enabled", False)
        text = "ref 9876543210"
        assert nc._redact_partner_text(
            text, field_name="f", task_type=A2ATaskType.ECHO) == text


class TestFrozenContractIsUndisturbed:
    def test_adding_carries_pii_did_not_change_direction_or_correlation(self):
        """`carries_pii` is additive metadata. The drift test in
        tests/a2a_common/test_protocol_contract.py guards the frozen PDF
        contract; this asserts the new field did not perturb the specs it
        annotates."""
        for tt in (A2ATaskType.COUNTER_PROPOSAL, A2ATaskType.CERT_CASE_RESULT,
                   A2ATaskType.ECHO):
            spec = MESSAGES[tt]
            assert spec.task_type is tt
            assert spec.direction is not None
            assert spec.correlation_key is not None
