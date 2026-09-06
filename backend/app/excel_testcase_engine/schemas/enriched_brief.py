# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Typed enriched user brief exchanged between enhancer and planner."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.excel_testcase_engine import domain_vocab


class EnrichedBrief(BaseModel):
    """Structured interpretation of the user's one-line request.

    BRD/TSD-only refactor: the API set is populated from the TSD Interface
    Specification (verbatim). `api_classification` is a free-form label — the
    default "tsd_driven" reflects the current mode; older cached responses
    with values like "existing_modified" / "new_api" / "mixed" / "unknown"
    still deserialise cleanly since the field is no longer a Literal.
    """

    original_brief: str
    archetype: Literal["A", "B", "C"] = "A"
    # Defaults from the active domain pack (genericisation sweep): the
    # feature-name fallback, the first cert-party role sheet, and the
    # domain's canonical request/response pair (UPI: ReqTransfer/RespTransfer).
    feature_name: str = Field(default_factory=domain_vocab.feature_name_default)
    roles: list[str] = Field(default_factory=lambda: domain_vocab.role_sheet_names()[:1])
    apis: list[str] = Field(default_factory=domain_vocab.default_apis)
    coverage: list[str] = Field(default_factory=lambda: ["happy_path", "timeout"])
    status_casing: Literal["upper", "title"] = "upper"
    confidence: float = 0.9
    open_questions: list[str] = Field(default_factory=list)
    options: dict = Field(default_factory=dict)

    api_classification: str = "tsd_driven"
    # Kept for back-compat with cached responses; unused in BRD/TSD-only mode.
    existing_apis_touched: list[str] = Field(default_factory=list)
    new_api_names: list[str] = Field(default_factory=list)
    # Reviewer-visible record of what the enhancer inferred rather than
    # confirmed from the brief/BRD/TSD. Always populated (may be empty).
    assumptions: list[str] = Field(default_factory=list)
