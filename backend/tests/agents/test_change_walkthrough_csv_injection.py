# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""CSV formula injection (CWE-1236) in the tester-scenario export.

`scenarios_to_csv` renders LLM output that is derived from a change's diff and
plan text, so a crafted commit message, code comment or PR description can
steer what lands in a cell. Excel/Sheets/Calc evaluate a cell opening with
`= + - @` (or a leading tab/CR, which they strip first) as a formula, turning
the QA sheet an analyst downloads from
GET /api/agentic/runs/{run_id}/walkthrough.csv into `=HYPERLINK(...)`
exfiltration or a DDE payload.

These tests pin the escaping so the guard cannot be dropped silently.
"""
import csv
import io

import pytest

from app.agents.change_walkthrough import scenarios_to_csv

FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r")


def _rows(walkthrough):
    return list(csv.reader(io.StringIO(scenarios_to_csv(walkthrough))))


@pytest.mark.parametrize("payload", [
    '=HYPERLINK("http://evil/","open")',
    "=cmd|'/c calc'!A1",
    "+1234",
    "-SUM(A1:A2)",
    "@import",
    "\tleading tab",
    "\rleading cr",
])
@pytest.mark.parametrize("field", ["id", "scenario", "input", "expected"])
def test_formula_lead_is_neutralized_in_every_field(payload, field):
    """No exported cell may begin with a character a spreadsheet reads as code."""
    rows = _rows({"tester_scenarios": [{field: payload}]})
    cell = rows[1][["id", "scenario", "input", "expected"].index(field)]
    assert cell.startswith("'"), f"{field}={payload!r} exported unescaped as {cell!r}"
    assert cell[1:] == payload, "escaping must preserve the original text verbatim"


def test_benign_text_is_not_mangled():
    """Quoting/commas stay normal CSV — the guard must not touch safe values."""
    rows = _rows({"tester_scenarios": [
        {"id": 7, "scenario": 'normal, text "quoted"', "input": "ok", "expected": "fine"},
    ]})
    assert rows[1] == ["7", 'normal, text "quoted"', "ok", "fine"]


def test_missing_and_none_fields_are_safe():
    """A scenario dict may be sparse; None must not become the string 'None'."""
    rows = _rows({"tester_scenarios": [{"id": 1, "scenario": None}]})
    assert rows[1] == ["1", "", "", ""]


def test_no_cell_in_a_mixed_export_starts_with_a_formula_lead():
    """Belt-and-braces sweep over a realistic multi-row export."""
    rows = _rows({"tester_scenarios": [
        {"id": "=1", "scenario": "@a", "input": "-b", "expected": "+c"},
        {"id": 2, "scenario": "safe", "input": "safe", "expected": "safe"},
    ]})
    for row in rows[1:]:
        for cell in row:
            assert not cell[:1] in FORMULA_LEADS, f"unescaped cell: {cell!r}"
