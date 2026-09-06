# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Stable checkpoint IDs, verdict values, and policy modes.

These enums are the single source of truth. Never use raw strings
like "brd_to_tech_spec" anywhere else in the codebase — always import
from here so a rename is one-place.
"""
from enum import Enum


class CheckpointId(str, Enum):
    # Phase A — first wave (highest ROI, implement first)
    BRD_TO_TECH_SPEC = "brd_to_tech_spec"
    TECH_SPEC_TO_XSD = "tech_spec_to_xsd"
    PRODUCT_KIT_TO_PHASE_C = "product_kit_to_phase_c_communication"
    PHASE_C_QUERY_TO_PO_RESPONSE = "phase_c_query_to_po_response"

    # Phase A — second wave (add after first wave is calibrated)
    CLARIFICATION_TO_BRD = "clarification_to_brd"
    RESEARCH_TO_CANVAS = "research_to_canvas"

    # Phase A — Phase 7 expansion: full coverage of every Phase A transition.
    # Each gates the transition out of the stage that produces the named artifact:
    #   INITIAL_TO_PROMPT_ENHANCED  → gates prompt_enhancement -> research
    #   PROMPT_TO_RESEARCH          → gates research -> canvas
    #   CANVAS_TO_CLARIFICATION     → gates clarification -> brd
    # The two enum entries above (RESEARCH_TO_CANVAS, CLARIFICATION_TO_BRD) get
    # their first contract in this phase as well; they gate canvas -> clarification
    # and brd -> tech_spec respectively.
    INITIAL_TO_PROMPT_ENHANCED = "initial_to_prompt_enhanced"
    PROMPT_TO_RESEARCH = "prompt_to_research"
    CANVAS_TO_CLARIFICATION = "canvas_to_clarification"

    # Phase B — third wave (needs UAT, not local)
    CODE_TO_REVIEW = "code_to_review"
    REVIEW_TO_BUILD = "review_to_build"

    # Phase C / A2A — third wave
    A2A_OUTBOUND_PREFLIGHT = "a2a_outbound_preflight"
    A2A_INBOUND_CLASSIFICATION = "a2a_inbound_classification"
    READINESS_TO_CERTIFICATION = "readiness_to_certification"


class VerdictValue(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class PolicyMode(str, Enum):
    DISABLED = "disabled"       # defined but not executed
    ADVISORY = "advisory"       # runs, records verdict, never blocks
    SOFT_GATE = "soft_gate"     # WARN requires acknowledgement, FAIL may retry
    HARD_GATE = "hard_gate"     # Any non-PASS verdict blocks until override or retry passes


# First-wave checkpoints that must exist before Phase 1 can start
FIRST_WAVE_CHECKPOINTS: list[CheckpointId] = [
    CheckpointId.BRD_TO_TECH_SPEC,
    CheckpointId.TECH_SPEC_TO_XSD,
    CheckpointId.PRODUCT_KIT_TO_PHASE_C,
    CheckpointId.PHASE_C_QUERY_TO_PO_RESPONSE,
]
