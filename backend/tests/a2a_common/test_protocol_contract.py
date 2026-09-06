# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 0 contract guard for the A2A protocol v1.

Asserts `app.a2a_common.protocol` matches `A2A_PROTOCOL_DESIGN.pdf` (v1.0): the
frozen 28 messages (+ echo) and their directions are exactly the PDF appendix,
all 22 error codes exist, and the envelope honours deltas 1–4. If someone adds,
renames, or drops a message, this fails — drift from the contract is caught in CI.

The expected lists below are transcribed from the PDF's "All 28 messages at a
glance" appendix and the §10 error-code table. Treat THEM as the source when the
PDF is revised; update both together.
"""
from __future__ import annotations

import pytest

# Package __init__ imports the a2a SDK; skip cleanly where the wheel is absent
# (matches tests/a2a_common/test_imports.py).
pytest.importorskip("a2a")

from app.a2a_common.protocol import (  # noqa: E402
    MESSAGES,
    PROTOCOL_VERSION,
    A2ATaskType,
    Direction,
    Envelope,
    ErrorCode,
    PayloadBase,
    FROZEN_TASK_TYPES,
    make_envelope,
    read_envelope,
)

# --- PDF appendix: the 28 frozen messages + echo, in order, with direction ---
_N2B = Direction.AUTHORITY_TO_PARTNER
_B2N = Direction.PARTNER_TO_AUTHORITY
_EITHER = Direction.EITHER

PDF_APPENDIX: list[tuple[str, Direction]] = [
    ("change_communication", _N2B),
    ("proposal_acknowledged", _B2N),
    ("change_acknowledgement", _B2N),
    ("query", _B2N),
    ("clarification_response", _N2B),
    ("counter_proposal", _B2N),
    ("counter_decision", _N2B),
    ("milestone_update", _B2N),
    ("milestone_status_request", _EITHER),
    ("milestone_status_report", _EITHER),
    ("cert_readiness_declaration", _B2N),
    ("blocker", _B2N),
    ("blocker_status_update", _N2B),
    ("blocker_resolution", _N2B),
    ("cert_config_request", _N2B),
    ("cert_config_submission", _B2N),
    ("cert_setup_notification", _N2B),
    ("cert_test_preparation", _B2N),
    ("cert_case_result", _EITHER),
    ("cert_verdict_notification", _N2B),
    ("cert_verdict_dispute", _B2N),
    ("cert_waiver_request", _B2N),
    ("cert_waiver_decision", _N2B),
    ("cert_fix_notification", _B2N),
    ("cert_signoff_notification", _N2B),
    ("cert_status_request", _EITHER),
    ("cert_status_report", _EITHER),
    ("cert_run_abort", _EITHER),
    ("echo", _B2N),
]

# PDF §10 — the 22 structured error codes.
PDF_ERROR_CODES = {
    "signature_mismatch", "timestamp_skew", "replay_detected",
    "missing_envelope_headers", "invalid_token", "session_revoked",
    "session_expired", "partner_inactive", "mtls_required",
    "mtls_fingerprint_mismatch", "ip_not_allowed", "partner_mismatch",
    "unknown_id", "invalid_state_transition", "unknown_agent",
    "agent_not_authorized_for_task", "unknown_task_type",
    "payload_validation_error", "bank_identity_mismatch",
    "cert_fingerprint_mismatch", "bank_unreachable", "executor_error",
}


def test_protocol_version():
    assert PROTOCOL_VERSION == "1.0"


def test_frozen_task_types_match_pdf_exactly():
    """The non-ext task types are precisely the PDF appendix — no more, no less."""
    expected = [name for name, _ in PDF_APPENDIX]
    actual = [tt.value for tt in FROZEN_TASK_TYPES]
    assert actual == expected, (
        "Frozen task types drifted from the PDF appendix.\n"
        f"  missing from code: {set(expected) - set(actual)}\n"
        f"  extra in code:     {set(actual) - set(expected)}"
    )
    assert len(expected) == 29  # 28 messages + echo


def test_directions_match_pdf():
    for name, direction in PDF_APPENDIX:
        tt = A2ATaskType(name)
        assert MESSAGES[tt].direction == direction, f"{name} direction mismatch"


def test_ext_types_excluded_from_frozen():
    ext = [tt for tt in A2ATaskType if tt.is_ext()]
    assert {tt.value for tt in ext} == {
        "cert_witness_request",
        "cert_witness_scheduled",
        "revision_in_progress",
        "round_opened",
        "round_closed",
        # ITA I-0: the integration-testing tunnel pair.
        "http_exchange_request",
        "http_exchange_response",
        # ITA I-6: the start signal for the partner-initiated suite half.
        "cert_execution_start",
    }
    for tt in ext:
        assert tt not in FROZEN_TASK_TYPES


def test_tunnel_exchange_types_are_pii_bearing():
    """The tunnel forwards an encapsulated body verbatim, plus Authorization
    and Cookie (ITA §5.3, deliberately). Classifying it as PII-free would let
    a third party's live traffic reach an external LLM unfiltered — the exact
    failure PII_BEARING_TASK_TYPES exists to prevent."""
    from app.a2a_common.protocol import PII_BEARING_TASK_TYPES

    for tt in (A2ATaskType.HTTP_EXCHANGE_REQUEST, A2ATaskType.HTTP_EXCHANGE_RESPONSE):
        assert MESSAGES[tt].carries_pii is True
        assert tt in PII_BEARING_TASK_TYPES
        assert MESSAGES[tt].direction == Direction.EITHER, \
            "one pair must serve both the forward and the reverse flow"


def test_round_lifecycle_ext_are_authority_to_partner_change_id():
    """round_opened / round_closed must correlate on change_id and flow NPCI→bank —
    a swap here would silently route them wrong. Guards the wire spec."""
    for tt in (A2ATaskType.ROUND_OPENED, A2ATaskType.ROUND_CLOSED):
        spec = MESSAGES[tt]
        assert spec.direction == _N2B, f"{tt.value} direction drifted"
        assert spec.ext is True


def test_every_task_type_has_a_spec():
    """No enum member is missing direction/correlation metadata."""
    for tt in A2ATaskType:
        assert tt in MESSAGES, f"{tt.value} has no MessageSpec"


def test_all_22_error_codes_present_with_layers():
    actual = {ec.value for ec in ErrorCode}
    assert actual == PDF_ERROR_CODES, (
        f"missing: {PDF_ERROR_CODES - actual}; extra: {actual - PDF_ERROR_CODES}"
    )
    # every code maps to a non-empty pipeline layer
    for ec in ErrorCode:
        assert ec.layer


def test_envelope_roundtrips_with_from_alias_and_defaults():
    env = Envelope.model_validate(
        {
            "message_id": "m-1",
            "task_type": "change_communication",
            "from": "npci-platform",
            "change_id": "chg-1",
        }
    )
    assert env.from_ == "npci-platform"
    assert env.protocol_version == "1.0"      # delta #1 default
    assert env.task_type is A2ATaskType.CHANGE_COMMUNICATION
    dumped = env.model_dump(by_alias=True)
    assert dumped["from"] == "npci-platform"  # serialises back to "from"
    assert dumped["message_id"] == "m-1"      # delta #2 present


def test_envelope_requires_message_id():
    with pytest.raises(Exception):
        Envelope.model_validate(
            {"task_type": "echo", "from": "bank-x"}  # no message_id
        )


def test_envelope_forbids_unknown_fields():
    with pytest.raises(Exception):
        Envelope.model_validate(
            {
                "message_id": "m-2",
                "task_type": "echo",
                "from": "bank-x",
                "surprise_field": True,  # extra="forbid" on the envelope
            }
        )


def test_middleware_error_codes_map_to_canonical():
    """The finer middleware codes catalogue cleanly onto canonical ErrorCodes."""
    from app.a2a_common.protocol import MIDDLEWARE_ERROR_CODES
    assert MIDDLEWARE_ERROR_CODES  # non-empty
    for raw, canonical in MIDDLEWARE_ERROR_CODES.items():
        assert isinstance(raw, str)
        assert isinstance(canonical, ErrorCode)


def test_payload_base_allows_extensions():
    """Payload bodies stay permissive (the 'preserve extensions' decision)."""
    p = PayloadBase.model_validate({"anything": 1, "nested": {"a": "b"}})
    assert p.model_dump()["anything"] == 1


# --- Phase 1: envelope plumbing helpers ---


def test_make_envelope_sets_required_fields_and_omits_none():
    env = make_envelope(
        A2ATaskType.CHANGE_COMMUNICATION,
        message_id="m-1",
        from_="npci-platform",
        payload={"a": 1},
        change_id="chg-1",
        correlation_id="corr-1",
        agent_id="npci.platform.v1",
        timestamp="2026-06-05T00:00:00+00:00",
    )
    assert env["protocol_version"] == "1.0"        # delta #1
    assert env["message_id"] == "m-1"              # delta #2
    assert env["task_type"] == "change_communication"
    assert env["from"] == "npci-platform"
    assert env["payload"] == {"a": 1}
    assert env["change_id"] == "chg-1"
    assert env["correlation_id"] == "corr-1"
    # None optionals are omitted, not sent as null
    assert "cflow_id" not in env
    assert "agent_run_id" not in env


def test_make_envelope_accepts_plain_string_task_type():
    """Migration window: pre-rename task_types (not yet in the enum) still build."""
    env = make_envelope("status_update", message_id="m-2", from_="partner")
    assert env["task_type"] == "status_update"
    assert env["payload"] == {}


def test_make_then_read_envelope_roundtrips():
    env = make_envelope(
        A2ATaskType.QUERY,
        message_id="m-3",
        from_="partner",
        payload={"q": "?"},
        change_id="chg-9",
        correlation_id="corr-9",
    )
    parsed = read_envelope(env)
    assert parsed.task_type == "query"
    assert parsed.message_id == "m-3"
    assert parsed.correlation_id == "corr-9"
    assert parsed.change_id == "chg-9"
    assert parsed.payload == {"q": "?"}
    assert parsed.from_ == "partner"


def test_read_envelope_tolerates_legacy_message():
    """A pre-v1 message (no message_id/correlation_id) parses with None fields."""
    legacy = {"task_type": "echo", "change_id": None, "payload": {}, "from": "partner"}
    parsed = read_envelope(legacy)
    assert parsed.task_type == "echo"
    assert parsed.message_id is None
    assert parsed.correlation_id is None
    assert parsed.protocol_version is None
