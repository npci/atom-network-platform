# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""WS3b — shared-validator widening detector. A change to a shared helper (ValidatorCommons / …Util)
called by ≥2 other validators must SELF-HEAL (loop back to isolate the change), never escalate to a
human. Models the fa4631e3 case where validateHead's allow-list widened prodType for every message."""
from types import SimpleNamespace

from app.agents import agentic_orchestrator as O


def test_is_shared_helper():
    assert O._is_shared_helper("a/ValidatorCommons.java")
    assert O._is_shared_helper("a/ApiUtils.java")
    assert O._is_shared_helper("a/AbstractBase.java")
    assert not O._is_shared_helper("a/ReqTransferValidator.java")
    assert not O._is_shared_helper("a/foo.xsd")


def _cs(path):
    return SimpleNamespace(operations=[SimpleNamespace(op="modify", repo_id="r", path=path)])


def test_widening_detected_with_two_other_callers(monkeypatch, tmp_path):
    d = tmp_path / "repo" / "src" / "main" / "java" / "v"
    d.mkdir(parents=True)
    (d / "ValidatorCommons.java").write_text("public class ValidatorCommons { static void validateHead(){} }")
    (d / "ReqTransferValidator.java").write_text("class ReqTransferValidator { void v(){ ValidatorCommons.validateHead(); } }")
    (d / "ReqHbtValidator.java").write_text("class ReqHbtValidator { void v(){ ValidatorCommons.validateHead(); } }")
    monkeypatch.setattr(O.workspace_local, "repo_dir", lambda ws, rid: tmp_path / "repo")
    monkeypatch.setattr(O, "_ws_id", lambda run: "ws")
    out = O._shared_validator_widening(None, SimpleNamespace(id="run", change_request_id="cr"),
                                       _cs("src/main/java/v/ValidatorCommons.java"))
    assert len(out) == 1
    f = out[0]
    assert f["shared_heal"] is True
    assert f["category"] == "correctness" and f["severity"] == "error"   # NOT must-block → never human-escalated
    assert not O.is_must_block(f["category"], f["severity"])
    assert "Isolate the new rule" in f["suggested_fix"]


def test_no_finding_when_only_one_caller(monkeypatch, tmp_path):
    d = tmp_path / "repo" / "src" / "main" / "java" / "v"
    d.mkdir(parents=True)
    (d / "ValidatorCommons.java").write_text("class ValidatorCommons {}")
    (d / "ReqTransferValidator.java").write_text("uses ValidatorCommons")
    monkeypatch.setattr(O.workspace_local, "repo_dir", lambda ws, rid: tmp_path / "repo")
    monkeypatch.setattr(O, "_ws_id", lambda run: "ws")
    out = O._shared_validator_widening(None, SimpleNamespace(id="r", change_request_id="c"),
                                       _cs("src/main/java/v/ValidatorCommons.java"))
    assert out == []


def test_no_finding_for_non_shared_file():
    # A message-specific validator is not a shared helper → returns [] before any FS access.
    out = O._shared_validator_widening(None, SimpleNamespace(id="r", change_request_id="c"),
                                       _cs("src/main/java/v/ReqTransferValidator.java"))
    assert out == []
