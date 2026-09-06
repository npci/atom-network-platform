# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Column layout registry for certification test-case sheets."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.domain.registry import prompt_block

# The C2 "which side is this sheet written from" column. Was the literal "PSP as",
# which names a payments role on every domain's workbook. Resolved once at import
# (layouts are module-level constants) and used as BOTH the header text and its
# `widths` key — they must stay the same string or the width silently goes unset.
_ROLE_AS_HEADER = prompt_block("role_as_label", "Role as")


@dataclass(frozen=True)
class Layout:
    """One test-case sheet layout."""

    key: str
    headers: list[str]
    start_row: int
    start_col: int
    freeze_panes: str
    widths: dict[str, float]


LAYOUT_A1 = Layout(
    key="A1",
    headers=["TEST ID", "DETAILS", "DESCRIPTION", "TEST STEPS", "Status"],
    start_row=1,
    start_col=1,
    freeze_panes="A2",
    widths={"TEST ID": 12, "DETAILS": 30, "DESCRIPTION": 45, "TEST STEPS": 70, "Status": 13},
)

LAYOUT_B1 = Layout(
    key="B1",
    headers=["TEST ID", "DETAILS", "DESCRIPTION", "Status", "TEST STEPS"],
    start_row=1,
    start_col=1,
    freeze_panes="A2",
    widths={"TEST ID": 12, "DETAILS": 30, "DESCRIPTION": 45, "Status": 13, "TEST STEPS": 70},
)

LAYOUT_C1 = Layout(
    key="C1",
    headers=["S.NO", "TEST ID", "DETAILS", "DESCRIPTION", "Scope", "TXN INITIATED BY", "STATUS", "TEST STEPS"],
    start_row=2,
    start_col=2,
    freeze_panes="B3",
    widths={"S.NO": 7, "TEST ID": 12, "DETAILS": 30, "DESCRIPTION": 45, "Scope": 14, "TXN INITIATED BY": 16, "STATUS": 13, "TEST STEPS": 80},
)

LAYOUT_C2 = Layout(
    key="C2",
    headers=["S.NO", "TEST ID", "DETAILS", _ROLE_AS_HEADER, "TEST DESCRIPTION", "Scope", "TXN INITIATED BY", "STATUS", "TEST STEPS"],
    start_row=2,
    start_col=2,
    freeze_panes="B3",
    widths={"S.NO": 7, "TEST ID": 12, "DETAILS": 30, _ROLE_AS_HEADER: 10, "TEST DESCRIPTION": 45, "Scope": 14, "TXN INITIATED BY": 16, "STATUS": 13, "TEST STEPS": 80},
)

LAYOUT_C3 = Layout(
    key="C3",
    headers=["S.NO", "Test Case ID", "API Details", "Description", "Test Steps", "Status", "To Be Done by Entity", "Round 1 Txn Ids", "Round 2 Txn Ids"],
    start_row=1,
    start_col=1,
    freeze_panes="A2",
    widths={"S.NO": 7, "Test Case ID": 14, "API Details": 30, "Description": 45, "Test Steps": 80, "Status": 13, "To Be Done by Entity": 18, "Round 1 Txn Ids": 30, "Round 2 Txn Ids": 30},
)

LAYOUT_REGISTRY = {
    layout.key: layout
    for layout in [LAYOUT_A1, LAYOUT_B1, LAYOUT_C1, LAYOUT_C2, LAYOUT_C3]
}
