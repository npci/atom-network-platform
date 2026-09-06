# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the shared `build_impact_block` helper (sub-slice 21a).

Verifies the extracted helper behaves identically to Slice 20c's inline
version, plus thin coverage of BRD/Tech-Spec wiring (db parameter
accepted, impact section threaded into context).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.agents.impact_block import build_impact_block
from app.core.config import settings
from app.kg.impact_analyzer import ImpactReport


# ──────────────────────────────────────────────────────────────────────────────
# build_impact_block
# ──────────────────────────────────────────────────────────────────────────────

class TestBuildImpactBlockShared:

    def test_flag_off_returns_empty(self, monkeypatch):
        monkeypatch.setattr(settings, "use_impact_analyzer", False)
        out = build_impact_block(MagicMock(), ["some long enough description text"])
        assert out == ""

    def test_db_none_returns_empty(self, monkeypatch):
        monkeypatch.setattr(settings, "use_impact_analyzer", True)
        out = build_impact_block(None, ["some long enough description text"])
        assert out == ""

    def test_no_description_with_min_length_returns_empty(self, monkeypatch):
        monkeypatch.setattr(settings, "use_impact_analyzer", True)
        out = build_impact_block(MagicMock(), ["", "hi", None, "  "])
        assert out == ""

    def test_first_long_enough_source_wins(self, monkeypatch):
        monkeypatch.setattr(settings, "use_impact_analyzer", True)
        captured = {}
        def fake_analyze(**kw):
            captured.update(kw)
            return ImpactReport(targets=["t"], files_affected=["X.java"])
        monkeypatch.setattr(
            "app.kg.impact_analyzer.analyze_impact", fake_analyze,
        )
        # First entry too short; second is long enough → that's what should
        # be passed to analyze_impact.
        out = build_impact_block(
            MagicMock(),
            ["short", "this one is long enough for the analyzer to see it"],
        )
        assert "this one is long enough" in captured["change_description"]
        assert "X.java" in out

    def test_analyze_impact_exception_returns_empty(self, monkeypatch):
        monkeypatch.setattr(settings, "use_impact_analyzer", True)
        monkeypatch.setattr(
            "app.kg.impact_analyzer.analyze_impact",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("AGE down")),
        )
        out = build_impact_block(
            MagicMock(), ["any sufficiently long description text here"],
        )
        assert out == ""

    def test_no_targets_returns_empty(self, monkeypatch):
        monkeypatch.setattr(settings, "use_impact_analyzer", True)
        monkeypatch.setattr(
            "app.kg.impact_analyzer.analyze_impact",
            lambda **kw: ImpactReport(),
        )
        out = build_impact_block(
            MagicMock(), ["any sufficiently long description text here"],
        )
        assert out == ""

    def test_no_files_affected_returns_empty(self, monkeypatch):
        monkeypatch.setattr(settings, "use_impact_analyzer", True)
        monkeypatch.setattr(
            "app.kg.impact_analyzer.analyze_impact",
            lambda **kw: ImpactReport(targets=["t1"], files_affected=[]),
        )
        out = build_impact_block(
            MagicMock(), ["any sufficiently long description text here"],
        )
        assert out == ""

    def test_happy_path_emits_block_with_custom_title(self, monkeypatch):
        monkeypatch.setattr(settings, "use_impact_analyzer", True)
        monkeypatch.setattr(
            "app.kg.impact_analyzer.analyze_impact",
            lambda **kw: ImpactReport(
                targets=["t1"], files_affected=["A.java", "B.py", "C.ts"],
            ),
        )
        out = build_impact_block(
            MagicMock(),
            ["Modify NetworkSwitchService.processBalance for retry"],
            block_title="Code Likely Affected by This Requirement",
        )
        assert "## Code Likely Affected by This Requirement" in out
        assert "- A.java" in out
        assert "- B.py" in out
        assert "- C.ts" in out

    def test_max_files_truncation_with_overflow_marker(self, monkeypatch):
        monkeypatch.setattr(settings, "use_impact_analyzer", True)
        many = [f"f{i}.java" for i in range(40)]
        monkeypatch.setattr(
            "app.kg.impact_analyzer.analyze_impact",
            lambda **kw: ImpactReport(targets=["t"], files_affected=many),
        )
        out = build_impact_block(
            MagicMock(), ["sufficiently long description for analyzer"],
            max_files=10,
        )
        assert "- f0.java" in out
        assert "- f9.java" in out
        assert "- f10.java" not in out
        assert "…and 30 more" in out

    def test_max_description_chars_clamps_input(self, monkeypatch):
        monkeypatch.setattr(settings, "use_impact_analyzer", True)
        captured = {}
        def fake_analyze(**kw):
            captured.update(kw)
            return ImpactReport(targets=[], files_affected=[])
        monkeypatch.setattr(
            "app.kg.impact_analyzer.analyze_impact", fake_analyze,
        )
        big = "x" * 10_000
        build_impact_block(
            MagicMock(), [big], max_description_chars=2000,
        )
        assert len(captured["change_description"]) == 2000


# ──────────────────────────────────────────────────────────────────────────────
# code_change.py compatibility wrapper still works
# ──────────────────────────────────────────────────────────────────────────────

class TestCodeChangeCompatWrapper:

    def test_wrapper_delegates_to_shared(self, monkeypatch):
        from app.agents import code_change as cc
        monkeypatch.setattr(settings, "use_impact_analyzer", True)
        monkeypatch.setattr(
            "app.kg.impact_analyzer.analyze_impact",
            lambda **kw: ImpactReport(
                targets=["t"], files_affected=["Retry.java"],
            ),
        )
        out = cc._build_impact_block(
            MagicMock(),
            "Modify NetworkSwitchService for retry behaviour", "BRD body",
        )
        # Default block title from shared helper.
        assert "## Blast Radius — Files Likely Related to This Change" in out
        assert "- Retry.java" in out

