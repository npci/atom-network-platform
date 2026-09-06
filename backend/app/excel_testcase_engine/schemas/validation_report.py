# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Validation and repair schemas for the quality gate."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Defect(BaseModel):
    """A workbook defect found by mechanical or semantic validation."""

    severity: Literal["critical", "warning"]
    sheet: str
    row: int | None = None
    test_id: str | None = None
    type: str
    message: str
    fix_hint: str = ""


class CellPatch(BaseModel):
    """A concrete openpyxl cell edit returned by the repairer."""

    sheet: str
    row: int
    column: str
    new_value: str


class ValidationReport(BaseModel):
    """Aggregated validation result."""

    status: Literal["pass", "fail"] = "pass"
    defects: list[Defect] = Field(default_factory=list)

    @property
    def has_critical(self) -> bool:
        return any(defect.severity == "critical" for defect in self.defects)
