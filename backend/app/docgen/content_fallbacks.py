# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Placeholder table shapes when the LLM omits table_data — driven by plan fields, not named APIs."""
from __future__ import annotations

from typing import Any

# Blueprint sets section_plan["table_fallback_profile"] to one of these (optional).
PROFILE_ERROR_MATRIX = "error_matrix"
PROFILE_TEST_MATRIX = "test_matrix"
PROFILE_FIELD_SPEC = "field_spec"
PROFILE_PROCESS_STEPS = "process_steps"
PROFILE_REQUIREMENT_TABLE = "requirement_table"


def _error_matrix_generic() -> dict[str, Any]:
    return {
        "headers": ["Response Code", "Error Code", "Description", "Operation", "Component", "TD/BD"],
        "rows": [
            ["E1", "E1", "Eligibility or precondition failed", "Pre-check", "Originating service", "BD"],
            ["E2", "E2", "Activation or registration failed", "Setup", "Credential service", "TD"],
            ["E3", "E3", "Authentication or validation failed", "Transaction", "Issuer / verifier", "TD"],
            ["E4", "E4", "Credential mismatch or replay detected", "Transaction", "Issuer / verifier", "TD"],
            ["TO", "TO", "Timeout waiting for downstream response", "Any", "Switch / broker", "TD"],
        ],
    }


def _test_matrix_generic() -> dict[str, Any]:
    return {
        "headers": ["Scenario", "Objective", "Owner"],
        "rows": [
            ["Happy path", "Validate primary success flow end-to-end", "Integration lead"],
            ["Negative path", "Validate failure handling and user-visible outcomes", "QA / Platform"],
            ["Recovery", "Validate retry, timeout, and reconciliation behaviour", "Operations"],
        ],
    }


def _field_spec_generic() -> dict[str, Any]:
    return {
        "headers": ["Field / Element", "Type", "Length", "Description", "Mandatory"],
        "rows": [
            ["schemaVersion", "string", "var", "Replace with normative version field for your API.", "Yes"],
            ["requestId", "string", "var", "Correlation id for tracing; replace with your idempotency key if used.", "Yes"],
            ["payload", "object", "n/a", "Replace with message-specific body per your specification.", "Yes"],
        ],
    }


def _process_steps_generic() -> dict[str, Any]:
    return {
        "headers": ["Step", "Activity", "Responsible"],
        "rows": [
            ["Pre-Check", "Validate prerequisites and participant readiness", "Initiating client / gateway"],
            ["Step 1", "Submit request and route to the next hop per integration rules", "Gateway / orchestrator"],
            ["Step 2", "Process, validate, and forward or respond", "Core platform"],
            ["Post Response", "Return outcome and persist audit trail", "Client / gateway"],
        ],
    }


def _requirement_table_generic() -> dict[str, Any]:
    return {
        "headers": ["ID", "Requirement", "Priority"],
        "rows": [
            ["FR-01", "The system shall satisfy the integration behaviour described in this document.", "High"],
            ["FR-02", "The system shall expose observability suitable for production support (logging, tracing).", "Medium"],
            ["FR-03", "The system shall enforce security and data-handling policies defined by the program.", "High"],
        ],
    }


def _infer_profile_from_heading(heading: str) -> str | None:
    """Lightweight hints from section titles only — no API names."""
    h = (heading or "").lower()
    if "error" in h:
        return PROFILE_ERROR_MATRIX
    if any(x in h for x in ("test", "certification", "audit")):
        return PROFILE_TEST_MATRIX
    if "process" in h and "flow" in h:
        return PROFILE_PROCESS_STEPS
    if "functional requirement" in h or (h.startswith("functional") and "requirement" in h):
        return PROFILE_REQUIREMENT_TABLE
    return None


def fallback_table_data(section_plan: dict[str, Any] | None, heading: str = "") -> dict[str, Any]:
    """
    Return placeholder table_data for repair / writer fallback.
    Prefer section_plan['table_fallback_profile']; otherwise infer from heading keywords; else field_spec.
    """
    sp = section_plan or {}
    profile = sp.get("table_fallback_profile")
    if isinstance(profile, str):
        profile = profile.strip()
    else:
        profile = None

    if not profile:
        profile = _infer_profile_from_heading(heading)
    if not profile:
        profile = PROFILE_FIELD_SPEC

    if profile == PROFILE_ERROR_MATRIX:
        return _error_matrix_generic()
    if profile == PROFILE_TEST_MATRIX:
        return _test_matrix_generic()
    if profile == PROFILE_PROCESS_STEPS:
        return _process_steps_generic()
    if profile == PROFILE_REQUIREMENT_TABLE:
        return _requirement_table_generic()
    # Unknown profile string: still return a neutral spec table
    return _field_spec_generic()
