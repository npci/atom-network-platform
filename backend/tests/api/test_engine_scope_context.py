# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""_load_engine_scope_context: the cert-engine's ONLY structured grounding.

The regression this guards: the BRD/TSD-only refactor dropped the four
pre-extracted signal sets (pm_scope_signals, brd_functional_requirements,
brd_feature_criteria, xsd_change) and left `tsd_sections` as the sole
structured input. `_TSD_SECTION_KEYS` was then referenced without being
defined -- and because the loader wraps everything in `except Exception`,
the resulting NameError was swallowed into a log line and the function
returned {"tsd_sections": {}} on every call. The engine still produced a
workbook, so nothing failed loudly; it was simply ungrounded.

The expected mapping is spelled out here rather than imported from the
module under test. Deriving the input AND the expectation from the same
dict is circular: it passes just as happily against a map that has lost
half its entries. This way the behavioural test keeps running -- and fails
on empty sections, the real symptom -- even if the constant disappears again.
"""
import asyncio
from types import SimpleNamespace

from app.api.agents import _load_engine_scope_context

# Headings the TSD generator emits -> normalised keys engine agents look up
# in options["tsd_sections"].
_EXPECTED = {
    "Control Flow & Sequence":       "control_flow",
    "Failure Handling & Resilience": "failure_handling",
    "Error & Response Handling":     "error_handling",
    "Testing & Verification":        "testing_verification",
    "Interface Specification":       "interface_spec",
}


class _Q:
    def __init__(self, row): self._row = row
    def filter(self, *a, **k): return self
    def order_by(self, *a, **k): return self
    def first(self): return self._row


class _DB:
    def __init__(self, row): self._row = row
    def query(self, *a, **k): return _Q(self._row)


def _load(tsd_content):
    row = SimpleNamespace(content=tsd_content) if tsd_content is not None else None
    return asyncio.run(_load_engine_scope_context("cr-1", _DB(row)))


def test_every_expected_heading_is_extracted():
    tsd = "\n".join(
        ["# Technical Specification"]
        + [f"\n## {h}\n\nBody text for {h}.\n" for h in _EXPECTED]
    )
    sections = _load(tsd)["tsd_sections"]
    assert sorted(sections) == sorted(_EXPECTED.values())
    assert all(v.strip() for v in sections.values()), "a mapped heading extracted empty body"


def test_partial_tsd_yields_only_the_headings_present():
    tsd = "# TSD\n\n## Control Flow & Sequence\n\nOnly this one.\n"
    assert list(_load(tsd)["tsd_sections"]) == ["control_flow"]


def test_missing_tsd_row_is_not_an_error():
    assert _load(None) == {"tsd_sections": {}}


def test_module_mapping_matches_the_expected_contract():
    from app.api.agents import _TSD_SECTION_KEYS

    assert _TSD_SECTION_KEYS == _EXPECTED


def test_headings_match_the_docgen_guides():
    """Drift guard: the loader splits on headings the TSD generator emits.

    If a guide renames a section, extraction silently returns fewer keys --
    the same failure mode as the NameError, just from the other end.
    """
    from pathlib import Path

    import app.docgen.document_guides as guides

    source = Path(guides.__file__).read_text(encoding="utf-8")
    missing = [h for h in _EXPECTED if h not in source]
    assert not missing, f"headings absent from document_guides.py: {missing}"
