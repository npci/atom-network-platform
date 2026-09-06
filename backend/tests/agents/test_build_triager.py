# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the change-aware build-failure triager (§8 verify tier)."""
import json

from app.agents import build_triager as bt

LOG = """
[INFO] Building iupi 1.0
[ERROR] /app/clone/iupi/src/main/java/com/example/Pay.java:[42,10] cannot find symbol
[ERROR] /app/clone/iupi/src/main/java/com/example/Pay.java:[42,10] cannot find symbol
[INFO] Building legacy-mod 1.0
[ERROR] /app/clone/legacy-mod/src/main/java/com/example/Old.java:[7,5] package x does not exist
"""
IGW = "/app/clone/iupi/src/main/java/com/example/Pay.java"
LEGACY = "/app/clone/legacy-mod/src/main/java/com/example/Old.java"
CHANGED = ["iupi/src/main/java/com/example/Pay.java"]   # repo-relative, touches module "iupi"


def test_parse_mvn_errors_dedups():
    errs = bt.parse_mvn_errors(LOG)
    assert len(errs) == 2                       # the duplicate iupi line is collapsed
    assert errs[0].file == IGW and errs[0].line == 42
    assert "cannot find symbol" in errs[0].message


def test_module_name_stable_across_absolute_and_relative():
    assert bt.module_name(IGW) == "iupi"
    assert bt.module_name("iupi/src/main/java/com/example/Pay.java") == "iupi"
    assert bt.module_name("/app/clone/legacy-mod/src/main/java/X.java") == "legacy-mod"


def test_annotate_marks_touched_module():
    errs = bt.annotate_failures(bt.parse_mvn_errors(LOG), CHANGED, skip_patterns=["*legacy*"])
    by_file = {e.file: e for e in errs}
    assert by_file[IGW].touched is True and by_file[IGW].module == "iupi"
    assert by_file[LEGACY].touched is False
    assert by_file[LEGACY].skip_listed is True          # matches the operator skip pattern


async def test_safeguard_touched_failure_never_suppressed(monkeypatch):
    # An adversarial LLM tries to call BOTH errors "legacy noise".
    async def fake_call_llm(**kwargs):
        return json.dumps([
            {"file": IGW, "classification": "UNRELATED_LEGACY", "reasoning": "r", "remediation": "m"},
            {"file": LEGACY, "classification": "UNRELATED_LEGACY", "reasoning": "r", "remediation": "m"},
        ])
    monkeypatch.setattr(bt, "call_llm", fake_call_llm)

    report = await bt.triage_build_failures(LOG, CHANGED, skip_patterns=["*legacy*"])
    by_file = {f["file"]: f for f in report["failures"]}
    # the touched-module error is FORCED to RELATED_REGRESSION despite the LLM
    assert by_file[IGW]["classification"] == "RELATED_REGRESSION"
    # the genuinely untouched legacy error keeps the LLM's call
    assert by_file[LEGACY]["classification"] == "UNRELATED_LEGACY"
    assert report["related_count"] == 1


async def test_fail_open_when_llm_errors(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("gateway down")
    monkeypatch.setattr(bt, "call_llm", boom)
    report = await bt.triage_build_failures(LOG, CHANGED)
    by_file = {f["file"]: f for f in report["failures"]}
    # static classification still stands: touched → regression, untouched → unclassified (surfaced)
    assert by_file[IGW]["classification"] == "RELATED_REGRESSION"
    assert by_file[LEGACY]["classification"] == "UNCLASSIFIED"


async def test_no_compile_errors_reads_as_infra():
    report = await bt.triage_build_failures("[INFO] BUILD SUCCESS", CHANGED)
    assert report["failures"] == []
    assert "infra" in report["summary"].lower()
