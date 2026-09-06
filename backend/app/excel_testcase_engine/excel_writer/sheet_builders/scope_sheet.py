# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Build eRUPI Gateway-style scope sheet."""

from __future__ import annotations

from openpyxl.worksheet.worksheet import Worksheet

from app.excel_testcase_engine.excel_writer.styles import ALIGN_BODY_CENTER, BORDER, FONT_BODY, FONT_HEADER, HEADER_FILL, TAB_COLORS
from app.excel_testcase_engine.schemas.workbook_plan import WorkbookPlan


def build(ws: Worksheet, plan: WorkbookPlan) -> None:
    ws.append(["Sr No", "Scope", "Entity Involved", "Scope-Sheet", "Test Case Count"])
    for idx, sheet in enumerate([s for s in plan.sheets if s.test_cases], start=1):
        ws.append([idx, sheet.name, ", ".join(sheet.test_cases[0].entities if sheet.test_cases else []), sheet.name, len(sheet.test_cases)])
    ws.append([])
    ws.append(["Response Code Table"])
    ws.append(["Code", "Description"])
    ws.append(["00", "Success"])
    # Example rows are pack content (`scope_sheet_code_examples`) — a domain
    # that declares none shows only the universal success row.
    from app.excel_testcase_engine import domain_vocab
    for code, desc in domain_vocab.scope_sheet_code_examples():
        ws.append([code, desc])
    ws.append([])
    ws.append(["Version", "Revision Details"])
    ws.append(["V1.0", "Initial generated draft"])
    for row in ws.iter_rows():
        for cell in row:
            cell.font = FONT_HEADER if cell.row in {1, ws.max_row - 1} or cell.value in {"Response Code Table"} else FONT_BODY
            cell.border = BORDER
            cell.alignment = ALIGN_BODY_CENTER
            if cell.row == 1 or cell.value in {"Response Code Table", "Code", "Description", "Version", "Revision Details"}:
                cell.fill = HEADER_FILL
    for column in "ABCDE":
        ws.column_dimensions[column].width = 24
    ws.sheet_properties.tabColor = TAB_COLORS["meta"]
