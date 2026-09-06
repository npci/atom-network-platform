# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Build Archetype C index sheet."""

from __future__ import annotations

from openpyxl.worksheet.worksheet import Worksheet

from app.excel_testcase_engine.excel_writer.styles import ALIGN_BODY_CENTER, ALIGN_HEADER, BORDER, FONT_BODY, FONT_HEADER, FONT_LINK, HEADER_FILL, TAB_COLORS
from app.excel_testcase_engine.schemas.workbook_plan import WorkbookPlan


def build(ws: Worksheet, plan: WorkbookPlan) -> None:
    ws.append(["S.No", "SHEET NAME", "CONTENT"])
    for idx, sheet in enumerate(plan.sheets, start=1):
        ws.append([idx, sheet.name, sheet.metadata.get("content", "Test cases" if sheet.test_cases else "Workbook section")])
        link = ws.cell(row=idx + 1, column=2)
        link.hyperlink = f"#'{sheet.name}'!A1"
        link.font = FONT_LINK
    for row in ws.iter_rows():
        for cell in row:
            cell.border = BORDER
            cell.alignment = ALIGN_HEADER if cell.row == 1 else ALIGN_BODY_CENTER
            if cell.row == 1:
                cell.font = FONT_HEADER
                cell.fill = HEADER_FILL
            elif cell.font != FONT_LINK:
                cell.font = FONT_BODY
    ws.column_dimensions["A"].width = 8
    ws.column_dimensions["B"].width = 32
    ws.column_dimensions["C"].width = 55
    ws.sheet_properties.tabColor = TAB_COLORS["meta"]
