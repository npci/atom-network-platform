# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Build SUBSET definition sheet."""

from __future__ import annotations

from openpyxl.worksheet.worksheet import Worksheet

from app.excel_testcase_engine.excel_writer.styles import ALIGN_BODY_CENTER, BORDER, FONT_BODY, FONT_HEADER, HEADER_FILL, TAB_COLORS
from app.excel_testcase_engine.schemas.workbook_plan import WorkbookPlan


def build(ws: Worksheet, plan: WorkbookPlan) -> None:
    ws.append(["Subset", "Description", "Test Case Count"])
    for idx, sheet in enumerate([s for s in plan.sheets if s.test_cases], start=1):
        col = chr(ord("A") + idx - 1)
        ws.append([f"Subset {idx}", sheet.name, f"=COUNTA('MODES OF CERTIFICATION'!{col}5:{col}{4 + len(sheet.test_cases)})"])
    for row in ws.iter_rows():
        for cell in row:
            cell.font = FONT_HEADER if cell.row == 1 else FONT_BODY
            cell.border = BORDER
            cell.alignment = ALIGN_BODY_CENTER
            if cell.row == 1:
                cell.fill = HEADER_FILL
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 40
    ws.column_dimensions["C"].width = 18
    ws.sheet_properties.tabColor = TAB_COLORS["meta"]
