# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase A Excellence — Slice 1 critic LLM tests.

Covers:
- read_critic_config resolution (per-checkpoint > env > generator-cross)
- _parse_findings handles clean JSON, fenced JSON, embedded JSON, garbage
- critique() honours the disabled flag without calling the LLM
- critique() never raises when the LLM call fails
- runner.run_advisory threads critic findings into the judge
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
from tests._optional_stubs import stub_jwt, stub_pgvector

stub_jwt()
stub_pgvector()

from app.services.evaluation import critic as critic_mod  # noqa: E402
from app.services.evaluation import runner as runner_mod  # noqa: E402
from app.services.evaluation.checkpoints import CheckpointId, VerdictValue  # noqa: E402


class _FakeRow:
    def __init__(self, key, value):
        self.key = key
        self.value = value
    def __iter__(self):
        return iter((self.key, self.value))


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows
    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, app_configs: dict[str, str] | None = None):
        self._configs = app_configs or {}
    def execute(self, _stmt, params=None):
        prefix = (params or {}).get("prefix", "")
        if not prefix:
            return _FakeResult([])
        rows = [
            _FakeRow(k, v) for k, v in self._configs.items()
            if k.startswith(prefix.replace("%", ""))
        ]
        return _FakeResult(rows)


# ── read_critic_config ───────────────────────────────────────────────────────

class TestReadCriticConfig:
    def test_default_enabled_with_cross_provider_when_blank(self, monkeypatch):
        monkeypatch.setattr(critic_mod.settings, "eval_critic_enabled_by_default", True)
        monkeypatch.setattr(critic_mod.settings, "eval_critic_default_provider", "")
        monkeypatch.setattr(critic_mod.settings, "eval_critic_default_model", "")
        monkeypatch.setattr(critic_mod.settings, "eval_critic_cross_provider", True)
        monkeypatch.setattr(critic_mod.settings, "llm_provider", "claude")

        cfg = critic_mod.read_critic_config(_FakeDb(), CheckpointId.BRD_TO_TECH_SPEC)
        assert cfg["enabled"] is True
        assert cfg["provider"] == "openai"  # cross from claude
        assert cfg["model"] is None  # provider picks its own default

    def test_per_checkpoint_overrides_win(self, monkeypatch):
        monkeypatch.setattr(critic_mod.settings, "eval_critic_default_provider", "ollama")
        monkeypatch.setattr(critic_mod.settings, "eval_critic_default_model", "llama3.1:8b")
        monkeypatch.setattr(critic_mod.settings, "llm_provider", "claude")
        db = _FakeDb({
            "eval_critic.brd_to_tech_spec.provider": "anthropic",
            "eval_critic.brd_to_tech_spec.model":    "claude-3-5-sonnet",
        })
        cfg = critic_mod.read_critic_config(db, CheckpointId.BRD_TO_TECH_SPEC)
        assert cfg["provider"] == "anthropic"
        assert cfg["model"] == "claude-3-5-sonnet"
        assert cfg["enabled"] is True  # default

    def test_enabled_false_disables_critic(self):
        db = _FakeDb({"eval_critic.brd_to_tech_spec.enabled": "false"})
        cfg = critic_mod.read_critic_config(db, CheckpointId.BRD_TO_TECH_SPEC)
        assert cfg["enabled"] is False

    def test_cross_provider_off_uses_generator_provider(self, monkeypatch):
        monkeypatch.setattr(critic_mod.settings, "eval_critic_default_provider", "")
        monkeypatch.setattr(critic_mod.settings, "eval_critic_cross_provider", False)
        monkeypatch.setattr(critic_mod.settings, "llm_provider", "claude")
        cfg = critic_mod.read_critic_config(_FakeDb(), CheckpointId.BRD_TO_TECH_SPEC)
        assert cfg["provider"] == "claude"


# ── _parse_findings ──────────────────────────────────────────────────────────

class TestParseFindings:
    def test_parses_clean_json(self):
        raw = (
            '{"findings": ['
            '{"dimension":"requirement_coverage","score":0.4,"issue":"FR-04 missing"},'
            '{"dimension":"testability","score":0.9,"issue":""}'
            '],"summary":"Coverage is incomplete."}'
        )
        out = critic_mod._parse_findings(raw)
        assert any("FR-04 missing" in f for f in out)
        assert any("Coverage is incomplete." in f for f in out)
        # Empty issue is dropped from findings, only summary remains for it
        assert not any("score=0.90" in f for f in out)

    def test_parses_fenced_json(self):
        raw = "```json\n{\"findings\":[],\"summary\":\"Looks fine\"}\n```"
        out = critic_mod._parse_findings(raw)
        assert out == []

    def test_parses_embedded_json(self):
        raw = "Here's my analysis:\n{\"findings\":[],\"summary\":\"Embedded fine\"}"
        out = critic_mod._parse_findings(raw)
        assert out == []

    def test_garbage_returns_empty(self):
        assert critic_mod._parse_findings("not json at all") == []
        assert critic_mod._parse_findings("") == []
        assert critic_mod._parse_findings(None) == []  # type: ignore[arg-type]


# ── critique() ───────────────────────────────────────────────────────────────

@pytest.mark.critic_enabled
class TestCritique:
    @pytest.mark.asyncio
    async def test_disabled_short_circuits_without_llm(self, monkeypatch):
        called = {"hit": False}

        async def _should_not_be_called(*args, **kwargs):
            called["hit"] = True
            return "{}"

        monkeypatch.setitem(sys.modules, "app.core.llm", SimpleNamespace(call_llm=_should_not_be_called))
        db = _FakeDb({"eval_critic.brd_to_tech_spec.enabled": "false"})

        result = await critic_mod.critique(
            db=db,
            checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
            source_artifacts={"brd_document": {"content": "FR-01"}},
            target_artifacts={"tech_spec_document": {"content": "..."}},
        )
        assert result.enabled is False
        assert result.findings == []
        assert called["hit"] is False

    @pytest.mark.asyncio
    async def test_llm_failure_does_not_raise_and_returns_empty(self, monkeypatch):
        async def _boom(*args, **kwargs):
            raise RuntimeError("upstream timeout")

        monkeypatch.setitem(sys.modules, "app.core.llm", SimpleNamespace(call_llm=_boom))
        result = await critic_mod.critique(
            db=_FakeDb(),
            checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
            source_artifacts={"brd_document": {"content": "FR-01"}},
            target_artifacts={"tech_spec_document": {"content": "spec"}},
        )
        assert result.enabled is True
        assert result.findings == []
        assert "upstream timeout" in (result.error or "")

    @pytest.mark.asyncio
    async def test_good_response_produces_findings(self, monkeypatch):
        async def _ok(*args, **kwargs):
            return (
                '{"findings":['
                '{"dimension":"requirement_coverage","score":0.4,"issue":"FR-04 not in spec"}'
                '],"summary":"Needs work"}'
            )
        monkeypatch.setitem(sys.modules, "app.core.llm", SimpleNamespace(call_llm=_ok))
        result = await critic_mod.critique(
            db=_FakeDb(),
            checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
            source_artifacts={"brd_document": {"content": "FR-01\nFR-02\nFR-03\nFR-04"}},
            target_artifacts={"tech_spec_document": {"content": "FR-01, FR-02, FR-03"}},
        )
        assert result.enabled is True
        assert any("FR-04 not in spec" in f for f in result.findings)
        assert any("Needs work" in f for f in result.findings)
        assert result.judge_model and result.judge_model.startswith("critic:")

    @pytest.mark.asyncio
    async def test_skips_when_both_artifacts_empty(self, monkeypatch):
        called = {"hit": False}
        async def _should_not_be_called(*args, **kwargs):
            called["hit"] = True
            return "{}"
        monkeypatch.setitem(sys.modules, "app.core.llm", SimpleNamespace(call_llm=_should_not_be_called))
        result = await critic_mod.critique(
            db=_FakeDb(),
            checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
            source_artifacts={"brd_document": {}},
            target_artifacts={"tech_spec_document": {}},
        )
        assert result.findings == []
        assert called["hit"] is False
        assert "skipped" in (result.error or "").lower()


# ── runner integration: critic findings reach the judge ─────────────────────

class _DummyDbSession:
    pass


@pytest.fixture
def captured_save(monkeypatch):
    captured: dict = {}
    def _fake_save(db, change_request_id, verdict):
        captured["verdict"] = verdict
        return SimpleNamespace(id="row-1")
    monkeypatch.setattr(runner_mod, "_persist_verdict", _fake_save)
    return captured


@pytest.mark.critic_enabled
class TestRunnerThreadsCriticFindings:
    @pytest.mark.asyncio
    async def test_critic_finding_with_missing_keyword_becomes_fail(self, monkeypatch, captured_save):
        """A critic finding containing 'missing' triggers the contract's
        hard-fail mapping (UNMAPPED_REQUIREMENT) → FAIL. This matches the
        existing rule-based judge keyword logic."""
        async def _critic_with_hard_finding(*args, **kwargs):
            return critic_mod.CriticResult(
                findings=["[critic:requirement_coverage score=0.40] FR-04 is missing from the spec"],
                judge_model="critic:openai:gpt-4o-mini",
                provider="openai",
                enabled=True,
                latency_ms=120,
            )
        import app.services.evaluation.critic as cmod
        monkeypatch.setattr(cmod, "critique", _critic_with_hard_finding)

        result = await runner_mod.run_advisory(
            db=_DummyDbSession(),
            change_request_id="cr-critic-1",
            checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
            source_artifacts={"brd_document": {"type": "brd", "content": (
                "## Background\n"
                "## Functional Requirements\n"
                "FR-01 do thing\n"
                "## Compliance\n"
            )}},
            target_artifacts={"tech_spec_document": {"type": "tech_spec", "content": (
                "## Overview\n"
                "## Functional Requirements\n"
                "FR-01 implemented\n"
                "## API\n"
                "## Error Code Table\n"
                "| U30 |\n"
            )}},
        )
        assert result is not None
        v = captured_save["verdict"]
        assert v.verdict == VerdictValue.FAIL  # hard keyword 'missing' in critic finding
        assert any("FR-04" in r for r in v.reasons)
        assert v.critic_model == "critic:openai:gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_critic_soft_finding_becomes_warn(self, monkeypatch, captured_save):
        """A critic finding without hard keywords yields WARN, not FAIL."""
        async def _critic_with_soft_finding(*args, **kwargs):
            return critic_mod.CriticResult(
                findings=["[critic:testability score=0.55] Acceptance criteria are vague"],
                judge_model="critic:openai:gpt-4o-mini",
                provider="openai",
                enabled=True,
                latency_ms=80,
            )
        import app.services.evaluation.critic as cmod
        monkeypatch.setattr(cmod, "critique", _critic_with_soft_finding)

        result = await runner_mod.run_advisory(
            db=_DummyDbSession(),
            change_request_id="cr-critic-soft",
            checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
            source_artifacts={"brd_document": {"type": "brd", "content": (
                "## Background\n"
                "## Functional Requirements\n"
                "FR-01 do thing\n"
                "## Compliance\n"
            )}},
            target_artifacts={"tech_spec_document": {"type": "tech_spec", "content": (
                "## Overview\n"
                "## Functional Requirements\n"
                "FR-01 implemented\n"
                "## API\n"
                "## Error Code Table\n"
                "| U30 |\n"
            )}},
        )
        v = captured_save["verdict"]
        assert v.verdict == VerdictValue.WARN
        assert any("Acceptance criteria are vague" in r for r in v.reasons)

    @pytest.mark.asyncio
    async def test_critic_disabled_leaves_pass(self, monkeypatch, captured_save):
        async def _critic_disabled(*args, **kwargs):
            return critic_mod.CriticResult(
                findings=[], judge_model=None, provider=None,
                enabled=False, latency_ms=0,
            )
        import app.services.evaluation.critic as cmod
        monkeypatch.setattr(cmod, "critique", _critic_disabled)

        result = await runner_mod.run_advisory(
            db=_DummyDbSession(),
            change_request_id="cr-critic-2",
            checkpoint_id=CheckpointId.BRD_TO_TECH_SPEC,
            source_artifacts={"brd_document": {"type": "brd", "content": (
                "## Background\n"
                "## Functional Requirements\n"
                "FR-01 do thing\n"
                "## Compliance\n"
            )}},
            target_artifacts={"tech_spec_document": {"type": "tech_spec", "content": (
                "## Overview\n"
                "## Functional Requirements\n"
                "FR-01 implemented\n"
                "## API\n"
                "## Error Code Table\n"
                "| U30 |\n"
            )}},
        )
        assert result is not None
        v = captured_save["verdict"]
        assert v.verdict == VerdictValue.PASS
        assert v.critic_model is None


# ── grounding seam ───────────────────────────────────────────────────────────

from app.services.evaluation import grounding as grounding_mod  # noqa: E402
from app.services.evaluation.contracts import get_contract  # noqa: E402


class TestGroundingSeam:
    def _contract(self):
        return get_contract(CheckpointId.BRD_TO_TECH_SPEC)

    def test_disabled_returns_empty(self, monkeypatch):
        monkeypatch.setattr(grounding_mod.settings, "eval_grounding_enabled", False)
        res = grounding_mod.retrieve_grounding(_FakeDb(), self._contract(), "some target text")
        assert res.enabled is False
        assert res.empty
        assert res.provenance() == []

    def test_retrieval_exception_fails_open(self, monkeypatch):
        monkeypatch.setattr(grounding_mod.settings, "eval_grounding_enabled", True)

        def _boom(*_a, **_k):
            raise RuntimeError("index unavailable")

        # Patch the retrieval surface the seam imports.
        import app.rag.retrieval as retr_mod
        monkeypatch.setattr(retr_mod, "retrieve", _boom)
        res = grounding_mod.retrieve_grounding(_FakeDb(), self._contract(), "target")
        assert res.empty  # fail-open: no grounding, critic proceeds rubric-only
        assert "index unavailable" in (res.error or "")

    def test_success_collects_snippets_and_provenance(self, monkeypatch):
        monkeypatch.setattr(grounding_mod.settings, "eval_grounding_enabled", True)
        monkeypatch.setattr(grounding_mod.settings, "eval_grounding_top_k", 2)

        def _fake_retrieve(query, db, top_k=6, **_k):
            return [
                {"source_file": "upi_error_codes.md", "doc_category": "npci_error_code",
                 "content": "Z1 = invalid purpose code", "score": 0.81},
                {"source_file": "VpaService.java", "doc_category": "java_source",
                 "content": "class VpaService {}", "score": 0.55},
                {"source_file": "empty.md", "doc_category": "upi_product_doc",
                 "content": "   ", "score": 0.3},  # dropped: blank content
            ]

        import app.rag.retrieval as retr_mod
        monkeypatch.setattr(retr_mod, "retrieve", _fake_retrieve)
        res = grounding_mod.retrieve_grounding(_FakeDb(), self._contract(), "purpose code Z1")
        assert len(res.snippets) == 2  # blank dropped
        kinds = {s.kind for s in res.snippets}
        assert kinds == {"product", "code"}
        prov = res.provenance()
        assert prov[0]["source_file"] == "upi_error_codes.md"
        block = grounding_mod.format_grounding_block(res)
        assert "upi_error_codes.md" in block and "[1]" in block

    def test_build_prompt_injects_kb_block_when_grounded(self):
        contract = self._contract()
        res = grounding_mod.GroundingResult(snippets=[
            grounding_mod.GroundingSnippet(
                source_file="upi_error_codes.md", doc_category="npci_error_code",
                content="Z1 = invalid purpose code", score=0.8),
        ])
        prompt = critic_mod._build_prompt(
            contract,
            {"brd_document": {"content": "FR-01"}},
            {"tech_spec_document": {"content": "spec uses HTTP 400"}},
            res,
        )
        assert "KNOWLEDGE BASE CONTEXT" in prompt["user"]
        assert "upi_error_codes.md" in prompt["user"]
        assert "authoritative" in prompt["system"].lower()

    def test_build_prompt_unchanged_when_no_grounding(self):
        contract = self._contract()
        prompt = critic_mod._build_prompt(
            contract,
            {"brd_document": {"content": "FR-01"}},
            {"tech_spec_document": {"content": "spec"}},
            None,
        )
        assert "KNOWLEDGE BASE CONTEXT" not in prompt["user"]
