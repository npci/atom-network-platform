# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Capture, semantic scoring and the floor ratchet — tested without spending money.

The capture path and the LLM judge both need a provider at runtime. Their
PLUMBING must not. These tests stub the model boundary so the wiring around it —
preflight checks, JSON parsing, failure modes, the write-protection on goldens,
and the ratchet — is exercised on every run, and only the model call itself is
left unverified until someone supplies credentials.
"""
import json

import pytest

from tests.golden import capture as cap
from tests.golden import runner, scoring


# ── Preflight: fail before spending, with a reason ───────────────────────────

def test_capture_refuses_unknown_artifact():
    ok, why = cap.can_capture("not-an-artifact")
    assert not ok
    assert "no generator wired" in why


def test_capture_detects_the_env_example_placeholder(monkeypatch):
    """The placeholder key ships in .env.example. Catching it here turns a 401
    several minutes into a run into an immediate, readable message."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "llm_provider", "claude", raising=False)
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-replace-me", raising=False)
    ok, why = cap.can_capture("canvas")
    assert not ok
    assert "placeholder" in why


def test_capture_refuses_when_no_provider(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "llm_provider", "", raising=False)
    ok, why = cap.can_capture("canvas")
    assert not ok
    assert "LLM_PROVIDER" in why


def test_capture_accepts_a_configured_provider(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "llm_provider", "ollama", raising=False)
    ok, why = cap.can_capture("canvas")
    assert ok, why


def test_generators_exist_for_every_wired_artifact():
    assert set(cap.GENERATORS) == {"canvas", "xsd"}
    for fn in cap.GENERATORS.values():
        assert callable(fn)


def test_capture_drains_a_streaming_agent(monkeypatch):
    """The agents yield chunks; a golden is the concatenation. Verified without
    a model by substituting the stream."""
    async def fake_stream(**_kwargs):
        for chunk in ("## Feature\n\n", "Body text.", ""):
            yield chunk

    import app.agents.canvas as canvas_mod
    monkeypatch.setattr(canvas_mod, "stream_canvas_turn", fake_stream, raising=False)
    monkeypatch.setitem(
        cap.GENERATORS, "canvas",
        lambda inputs: cap._drain(fake_stream()),
    )
    from app.core.config import settings
    monkeypatch.setattr(settings, "llm_provider", "ollama", raising=False)

    assert cap.capture("canvas", {}) == "## Feature\n\nBody text."


# ── Semantic scoring ─────────────────────────────────────────────────────────

def _stub_call_llm(monkeypatch, reply):
    async def fake(**kwargs):
        assert kwargs.get("model"), "judge model must be pinned by the caller"
        return reply
    import app.core.llm as llm_mod
    monkeypatch.setattr(llm_mod, "call_llm", fake, raising=False)


def test_semantic_score_parses_a_judge_verdict(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "llm_provider", "ollama", raising=False)
    _stub_call_llm(monkeypatch, '{"score": 0.42, "reasons": ["lost the sunset date"]}')

    s = scoring.semantic_score("candidate", "golden", model="pinned-1")
    assert s is not None
    assert s.value == pytest.approx(0.42)
    assert "lost the sunset date" in s.findings


def test_semantic_score_tolerates_prose_around_the_json(monkeypatch):
    """Models wrap JSON in commentary. The parser must survive that rather than
    scoring the document zero for the judge's formatting."""
    from app.core.config import settings
    monkeypatch.setattr(settings, "llm_provider", "ollama", raising=False)
    _stub_call_llm(monkeypatch, 'Here is my grade:\n```json\n{"score": 0.9}\n```\nDone.')

    assert scoring.semantic_score("c", "g", model="pinned-1").value == pytest.approx(0.9)


def test_semantic_score_clamps_out_of_range_values(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "llm_provider", "ollama", raising=False)
    _stub_call_llm(monkeypatch, '{"score": 7}')
    assert scoring.semantic_score("c", "g", model="p").value == 1.0


def test_semantic_score_reports_an_unusable_judge_distinctly(monkeypatch):
    """A broken judge must be visible as a judge problem, not silently recorded
    as a bad document."""
    from app.core.config import settings
    monkeypatch.setattr(settings, "llm_provider", "ollama", raising=False)
    _stub_call_llm(monkeypatch, "I refuse to answer.")

    s = scoring.semantic_score("c", "g", model="p")
    assert s.detail.get("judge_error") == 1.0
    assert "unparseable" in s.findings[0]


def test_semantic_score_is_none_without_a_provider(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "llm_provider", "", raising=False)
    assert scoring.semantic_score("c", "g", model="p") is None


# ── Floor ratchet ────────────────────────────────────────────────────────────

def test_floor_may_not_be_lowered():
    """The failure this prevents: a phase that misses its bar lowers the bar and
    the suite records a regression as a pass."""
    case = runner.load_case("case_001")
    current = case["thresholds"]["structural_min"]
    with pytest.raises(SystemExit) as exc:
        runner.raise_floor("case_001", current - 0.1)
    assert "ratchet" in str(exc.value)


def test_floor_may_be_raised(tmp_path, monkeypatch):
    """Operates on a COPY. A test that mutates a committed fixture leaves the
    working tree dirty when it fails, and the next run starts from the wreckage."""
    src = runner.FIXTURES / "case_001.json"
    (tmp_path / "case_001.json").write_text(src.read_text(encoding="utf-8"),
                                            encoding="utf-8")
    monkeypatch.setattr(runner, "FIXTURES", tmp_path)

    current = json.loads(src.read_text())["thresholds"]["structural_min"]
    runner.raise_floor("case_001", current + 0.01)
    raised = json.loads((tmp_path / "case_001.json").read_text())
    assert raised["thresholds"]["structural_min"] > current
    # The committed fixture is untouched.
    assert json.loads(src.read_text())["thresholds"]["structural_min"] == current


# ── Capture must not silently overwrite an accepted baseline ─────────────────

def test_capture_without_write_leaves_the_golden_untouched(monkeypatch):
    path = runner.FIXTURES / "case_001.golden.md"
    before = path.read_text(encoding="utf-8")
    monkeypatch.setattr(cap, "capture", lambda a, i: "REPLACEMENT", raising=False)
    monkeypatch.setattr("tests.golden.capture.capture", lambda a, i: "REPLACEMENT", raising=False)

    out = runner.capture_case("case_001", write=False)
    assert out == "REPLACEMENT"
    assert path.read_text(encoding="utf-8") == before, "golden was overwritten without --write"
