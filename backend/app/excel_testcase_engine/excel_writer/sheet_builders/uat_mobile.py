# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Build UAT MOBILE APP sheet."""

from __future__ import annotations

from openpyxl.worksheet.worksheet import Worksheet

from app.excel_testcase_engine.excel_writer.styles import ALIGN_BODY_LEFT, BORDER, FONT_BODY, FONT_HEADER, HEADER_FILL, TAB_COLORS


def build(ws: Worksheet) -> None:
    ws.append(["TEST ID", "FUNCTIONALITY", "STEPS TO EXECUTE"])
    ws.append(["", "E2E CASES", ""])
    for row in ws.iter_rows():
        for cell in row:
            cell.font = FONT_HEADER if cell.row <= 2 else FONT_BODY
            cell.border = BORDER
            cell.alignment = ALIGN_BODY_LEFT
            if cell.row == 1:
                cell.fill = HEADER_FILL
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 35
    ws.column_dimensions["C"].width = 80
    ws.sheet_properties.tabColor = TAB_COLORS["meta"]
