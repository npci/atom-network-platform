# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for sub-slice 20c — impact-analyzer pre-flight in the code-change agent.

Covers `_build_impact_block` in isolation (pure helper, fully stubbed
analyze_impact) plus a thin integration test showing the block lands in
the SYSTEM_PROMPT_TEMPLATE output.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.agents import code_change as cc
from app.core.config import settings
from app.kg.impact_analyzer import ImpactReport


# ──────────────────────────────────────────────────────────────────────────────
# _build_impact_block
# ──────────────────────────────────────────────────────────────────────────────

class TestBuildImpactBlock:

    def test_flag_off_returns_empty_string(self, monkeypatch):
        monkeypatch.setattr(settings, "use_impact_analyzer", False)
        out = cc._build_impact_block(MagicMock(), "some tech spec", "brd")
        assert out == ""

    def test_flag_on_but_description_too_short_returns_empty(self, monkeypatch):
        monkeypatch.setattr(settings, "use_impact_analyzer", True)
        out = cc._build_impact_block(MagicMock(), "hi", "")
        assert out == ""

    def test_analyze_impact_exception_returns_empty(self, monkeypatch):
        monkeypatch.setattr(settings, "use_impact_analyzer", True)

        def boom(**kw):
            raise RuntimeError("AGE crashed")
        monkeypatch.setattr(
            "app.kg.impact_analyzer.analyze_impact", boom,
        )

        out = cc._build_impact_block(MagicMock(), "long tech spec text" * 5, "")
        assert out == ""

    def test_no_targets_returns_empty(self, monkeypatch):
        monkeypatch.setattr(settings, "use_impact_analyzer", True)
        monkeypatch.setattr(
            "app.kg.impact_analyzer.analyze_impact",
            lambda **kw: ImpactReport(),
        )
        out = cc._build_impact_block(MagicMock(), "long tech spec text" * 5, "")
        assert out == ""

    def test_no_files_affected_returns_empty(self, monkeypatch):
        """Target resolved but zero downstream files → empty block (LLM
        doesn't benefit from an empty section)."""
        monkeypatch.setattr(settings, "use_impact_analyzer", True)
        monkeypatch.setattr(
            "app.kg.impact_analyzer.analyze_impact",
            lambda **kw: ImpactReport(targets=["t1"], files_affected=[]),
        )
        out = cc._build_impact_block(MagicMock(), "long tech spec text" * 5, "")
        assert out == ""

    def test_happy_path_emits_markdown_with_files(self, monkeypatch):
        monkeypatch.setattr(settings, "use_impact_analyzer", True)
        monkeypatch.setattr(
            "app.kg.impact_analyzer.analyze_impact",
            lambda **kw: ImpactReport(
                targets=["network-service-id"],
                callers={"caller-1": 1, "caller-2": 2},
                files_affected=["NetworkSwitchService.java", "PaymentController.java", "network-retry-spec.md"],
            ),
        )
        out = cc._build_impact_block(MagicMock(), "Modify NetworkSwitchService.processBalance", "")

        assert "Blast Radius" in out
        assert "- NetworkSwitchService.java" in out
        assert "- PaymentController.java" in out
        assert "- network-retry-spec.md" in out
        # Summary line with counts
        assert "1 target symbol" in out
        assert "3 file" in out

    def test_long_files_list_truncated_to_25(self, monkeypatch):
        monkeypatch.setattr(settings, "use_impact_analyzer", True)
        files = [f"f{i}.java" for i in range(40)]
        monkeypatch.setattr(
            "app.kg.impact_analyzer.analyze_impact",
            lambda **kw: ImpactReport(
                targets=["t"], files_affected=files,
            ),
        )
        out = cc._build_impact_block(MagicMock(), "spec " * 10, "")

        # First 25 listed
        assert "- f0.java" in out
        assert "- f24.java" in out
        # 26th NOT listed as own line
        assert "- f25.java" not in out
        # Truncation marker
        assert "…and 15 more" in out

    def test_brd_used_when_tech_spec_empty(self, monkeypatch):
        """Fallback: if tech_spec is empty/whitespace, use BRD instead."""
        monkeypatch.setattr(settings, "use_impact_analyzer", True)
        captured = {}
        def fake_analyze(**kw):
            captured.update(kw)
            return ImpactReport(targets=["t"], files_affected=["x.java"])
        monkeypatch.setattr(
            "app.kg.impact_analyzer.analyze_impact", fake_analyze,
        )

        out = cc._build_impact_block(MagicMock(), "", "BRD body text that is long enough")
        assert "BRD body text" in captured["change_description"]
        assert "x.java" in out

    def test_description_truncated_to_5000_chars(self, monkeypatch):
        monkeypatch.setattr(settings, "use_impact_analyzer", True)
        captured = {}
        def fake_analyze(**kw):
            captured.update(kw)
            return ImpactReport(targets=[], files_affected=[])
        monkeypatch.setattr(
            "app.kg.impact_analyzer.analyze_impact", fake_analyze,
        )

        big = "x" * 10000
        cc._build_impact_block(MagicMock(), big, "")
        assert len(captured["change_description"]) == 5000


# ──────────────────────────────────────────────────────────────────────────────
# _build_system_prompt integration
# ──────────────────────────────────────────────────────────────────────────────

class TestSystemPromptIntegration:

    def _stub_retrieval(self, monkeypatch):
        """Stub retrieve + build_context + _get_file_tree so we can focus
        on the impact-block integration without touching RAG internals."""
        monkeypatch.setattr(cc, "retrieve", lambda *a, **kw: [])
        monkeypatch.setattr(cc, "build_context", lambda chunks, **kw: "(no context)")
        # _get_file_tree now takes an optional phase_b_run_id and returns a 4-tuple
        # (tree_text, path_to_repo, repo_label_to_id, repo_summaries).
        monkeypatch.setattr(cc, "_get_file_tree",
                            lambda db, phase_b_run_id=None: ("file_tree_stub", {}, {}, []))

    def test_impact_block_absent_when_flag_off(self, monkeypatch):
        self._stub_retrieval(monkeypatch)
        monkeypatch.setattr(settings, "use_impact_analyzer", False)

        prompt, _, _, _, doc_ctx = cc._build_system_prompt(
            MagicMock(), "change-id",
            tech_spec="Modify NetworkSwitchService for retry logic",
            brd="Some BRD text",
        )
        assert "Blast Radius" not in prompt
        # Core template sections still present
        assert "## Existing Codebase — Directory Tree" in prompt
        assert "file_tree_stub" in prompt

    def test_impact_block_present_when_flag_on_and_report_nonempty(self, monkeypatch):
        self._stub_retrieval(monkeypatch)
        monkeypatch.setattr(settings, "use_impact_analyzer", True)
        monkeypatch.setattr(
            "app.kg.impact_analyzer.analyze_impact",
            lambda **kw: ImpactReport(
                targets=["t"], files_affected=["Retry.java"],
            ),
        )

        prompt, _, _, _, doc_ctx = cc._build_system_prompt(
            MagicMock(), "change-id",
            tech_spec="Modify NetworkSwitchService for retry logic. " * 5,
            brd="Some BRD text",
        )
        assert "Blast Radius" in prompt
        assert "- Retry.java" in prompt

    def test_impact_block_absent_on_analyzer_exception_but_prompt_still_renders(
        self, monkeypatch,
    ):
        """Flag on + analyzer crashes → empty block, rest of the prompt
        should still render cleanly (no KeyError on {impact_block})."""
        self._stub_retrieval(monkeypatch)
        monkeypatch.setattr(settings, "use_impact_analyzer", True)

        def boom(**kw):
            raise RuntimeError("AGE exploded mid-preflight")
        monkeypatch.setattr(
            "app.kg.impact_analyzer.analyze_impact", boom,
        )

        prompt, _, _, _, doc_ctx = cc._build_system_prompt(
            MagicMock(), "change-id",
            tech_spec="Modify NetworkSwitchService. " * 5,
            brd="",
        )
        # Prompt renders despite the analyzer failure.
        assert "## Existing Codebase — Directory Tree" in prompt
        assert "Blast Radius" not in prompt
