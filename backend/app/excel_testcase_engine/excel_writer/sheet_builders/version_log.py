# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Build compact Version Log sheets."""

from __future__ import annotations

from openpyxl.worksheet.worksheet import Worksheet

from app.excel_testcase_engine.excel_writer.styles import ALIGN_BODY_CENTER, ALIGN_HEADER, BORDER, FONT_BODY, FONT_HEADER, HEADER_FILL, TAB_COLORS
from app.excel_testcase_engine.schemas.workbook_plan import WorkbookPlan


def build(ws: Worksheet, plan: WorkbookPlan) -> None:
    headers = ["S.NO", "Date", "Details", "Version", "Created By", "Approved By", "Remarks"]
    ws.append(headers)
    from app.excel_testcase_engine import domain_vocab
    ws.append([1, "", plan.global_conventions.get("feature_name", domain_vocab.feature_name_default()), "Initial Draft", "", "", ""])
    ws.append([])
    ws.append(["", "Scope", "Total Test Case"])
    testcase_sheets = [sheet for sheet in plan.sheets if sheet.test_cases]
    for sheet in testcase_sheets:
        ws.append(["", sheet.name, len(sheet.test_cases)])
    total_row = ws.max_row + 1
    ws.append(["", "Total", f"=SUM(C5:C{total_row - 1})"])
    for row in ws.iter_rows():
        for cell in row:
            cell.font = FONT_HEADER if cell.row in {1, 4} else FONT_BODY
            cell.border = BORDER
            cell.alignment = ALIGN_HEADER if cell.row in {1, 4} else ALIGN_BODY_CENTER
            if cell.row in {1, 4}:
                cell.fill = HEADER_FILL
    for column in "ABCDEFG":
        ws.column_dimensions[column].width = 18
    ws.sheet_properties.tabColor = TAB_COLORS["meta"]
