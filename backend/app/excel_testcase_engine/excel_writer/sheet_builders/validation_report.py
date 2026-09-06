# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Append a validation report sheet for fall-through delivery."""

from __future__ import annotations

from openpyxl.worksheet.worksheet import Worksheet

from app.excel_testcase_engine.excel_writer.styles import ALIGN_BODY_LEFT, BORDER, FONT_BODY, FONT_HEADER, HEADER_FILL, TAB_COLORS
from app.excel_testcase_engine.schemas.validation_report import Defect


def build(ws: Worksheet, defects: list[Defect]) -> None:
    ws.append(["Severity", "Sheet", "Row", "Test ID", "Type", "Message", "Fix Hint"])
    for defect in defects:
        ws.append([defect.severity, defect.sheet, defect.row, defect.test_id, defect.type, defect.message, defect.fix_hint])
    for row in ws.iter_rows():
        for cell in row:
            cell.font = FONT_HEADER if cell.row == 1 else FONT_BODY
            cell.border = BORDER
            cell.alignment = ALIGN_BODY_LEFT
            if cell.row == 1:
                cell.fill = HEADER_FILL
    widths = [12, 22, 8, 15, 20, 60, 60]
    for idx, width in enumerate(widths, start=1):
        ws.column_dimensions[chr(ord("A") + idx - 1)].width = width
    ws.sheet_properties.tabColor = TAB_COLORS["extras"]
