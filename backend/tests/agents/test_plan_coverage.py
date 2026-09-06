# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""plan_coverage — the omission axis: requirement extraction + coverage union +
fail-open. Monkeypatches call_llm_structured (returns the parsed dict directly —
the forced-tool path never yields prose JSON)."""
import asyncio
import json

import app.agents.plan_coverage as PC
from app.agents.plan_coverage import (
    extract_plan_requirements, find_uncovered_requirements, windows,
)


def _llm(monkeypatch, text):
    async def _f(*a, **kw): return json.loads(text)
    monkeypatch.setattr(PC, "call_llm_structured", _f)


# ── windowing (pure) ─────────────────────────────────────────────────────────
def test_windows_small_doc():
    assert windows("") == []
    assert windows("   ") == []
    assert windows("short doc") == ["short doc"]


def test_windows_large_doc_splits_and_caps():
    w = windows("x" * 200000)
    assert 1 < len(w) <= 8                       # capped at _MAX_WINDOWS
    assert all(len(x) <= 20000 for x in w)


# ── requirement extraction ───────────────────────────────────────────────────
def test_extract_requirements_parses(monkeypatch):
    _llm(monkeypatch, '{"requirements":[{"id":"r1","text":"child ReqTransfer carries the share amount"},'
                      '{"id":"r2","text":"reuse ReqTransferStageHandler"}]}')
    reqs = asyncio.run(extract_plan_requirements("PLAN CONTRACT"))
    assert [r["id"] for r in reqs] == ["r1", "r2"]
    assert "share amount" in reqs[0]["text"]


def test_extract_empty_plan_returns_empty():
    assert asyncio.run(extract_plan_requirements("")) == []


def test_extract_fails_open(monkeypatch):
    async def _boom(*a, **kw): raise RuntimeError("down")
    monkeypatch.setattr(PC, "call_llm_structured", _boom)
    assert asyncio.run(extract_plan_requirements("PLAN")) == []


# ── coverage / uncovered ─────────────────────────────────────────────────────
def test_find_uncovered_flags_the_missing_one(monkeypatch):
    _llm(monkeypatch, '{"covered":["r1"]}')      # r1 covered, r2 not
    reqs = [{"id": "r1", "text": "a"}, {"id": "r2", "text": "b"}]
    assert [r["id"] for r in asyncio.run(find_uncovered_requirements(reqs, "doc"))] == ["r2"]


def test_find_uncovered_all_covered(monkeypatch):
    _llm(monkeypatch, '{"covered":["r1","r2"]}')
    reqs = [{"id": "r1", "text": "a"}, {"id": "r2", "text": "b"}]
    assert asyncio.run(find_uncovered_requirements(reqs, "doc")) == []


def test_find_uncovered_fails_open_when_no_window_checked(monkeypatch):
    # Every window errors → checked==0 → [] (never flag everything as omitted).
    async def _boom(*a, **kw): raise RuntimeError("down")
    monkeypatch.setattr(PC, "call_llm_structured", _boom)
    assert asyncio.run(find_uncovered_requirements([{"id": "r1", "text": "a"}], "doc")) == []


def test_find_uncovered_empty_inputs():
    assert asyncio.run(find_uncovered_requirements([], "doc")) == []
    assert asyncio.run(find_uncovered_requirements([{"id": "r1", "text": "a"}], "")) == []
