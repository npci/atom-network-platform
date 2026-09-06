# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 0.3 — unit tests for `app.agents._context_packing`.

The packer is invoked from `core/llm.{call_llm,stream_llm}` to ensure
prompts fit the model context. These tests cover:

  - happy path (no trimming needed)
  - drop optional blocks first, ascending by score
  - drop core blocks next when optional alone isn't enough
  - drop history turns last (preserve final user turn)
  - raise when even with everything dropped, the prompt is too big
  - the lightweight `assert_within_context` gate

Because the underlying tokeniser path may or may not be available, all
tests stub `count_tokens` / `count_messages_tokens` to deterministic
character-based counts — that's the contract the packer relies on.
"""
from __future__ import annotations

import pytest

from app.agents import _context_packing as cp
from app.agents._context_packing import (
    ContextOverflowError,
    PackedContext,
    assert_within_context,
    pack_within_budget,
)


# ── Test helpers ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def stub_tokeniser(monkeypatch):
    """Use 1 char ≈ 1 token everywhere for predictable arithmetic."""
    def fake_count_tokens(text, model=None):
        return max(1, len(text or ""))

    def fake_count_messages(system, messages, model=None):
        n = 0
        if isinstance(system, str):
            n += fake_count_tokens(system)
        elif isinstance(system, list):
            for seg in system:
                if isinstance(seg, dict):
                    n += fake_count_tokens(seg.get("text") or "")
        for m in messages or []:
            body = m.get("content") if isinstance(m, dict) else None
            if isinstance(body, str):
                n += fake_count_tokens(body)
        return n

    monkeypatch.setattr(cp, "count_tokens", fake_count_tokens)
    monkeypatch.setattr(cp, "count_messages_tokens", fake_count_messages)
    monkeypatch.setattr(cp, "model_context_window", lambda m: 1000)
    cp._reset_pack_counters_for_tests()


# ── Happy path ───────────────────────────────────────────────────────────────

def test_no_trim_when_prompt_fits():
    blocks = [{"name": "rules", "text": "abc", "kind": "core"}]
    msgs = [{"role": "user", "content": "hi"}]
    pkt = pack_within_budget(
        system_blocks=blocks, messages=msgs,
        max_response_tokens=100, model="m",
    )
    assert isinstance(pkt, PackedContext)
    assert pkt.dropped_blocks == []
    assert pkt.dropped_history_turns == 0
    assert "rules" in pkt.system
    assert pkt.messages == msgs


# ── Drop optional first ──────────────────────────────────────────────────────

def test_drops_optional_blocks_lowest_score_first():
    # Budget: 1000 - 200 (response) - 256 (headroom) = 544
    blocks = [
        {"name": "core",      "text": "C" * 100, "kind": "core",     "score": 0.0},
        {"name": "opt-low",   "text": "L" * 300, "kind": "optional", "score": 0.1},
        {"name": "opt-high",  "text": "H" * 300, "kind": "optional", "score": 0.9},
    ]
    msgs = [{"role": "user", "content": "u"}]
    pkt = pack_within_budget(
        system_blocks=blocks, messages=msgs,
        max_response_tokens=200, model="m",
    )
    # Low-score optional should be dropped first; high-score retained
    # because the budget fits after dropping just one.
    assert "opt-low" in pkt.dropped_blocks
    assert "opt-high" not in pkt.dropped_blocks
    assert "core" not in pkt.dropped_blocks


def test_drops_all_optional_then_core_in_order():
    blocks = [
        {"name": "core-low",  "text": "C" * 200, "kind": "core",     "score": 0.1},
        {"name": "core-hi",   "text": "C" * 200, "kind": "core",     "score": 0.9},
        {"name": "opt-low",   "text": "O" * 200, "kind": "optional", "score": 0.0},
        {"name": "opt-hi",    "text": "O" * 200, "kind": "optional", "score": 0.5},
    ]
    msgs = [{"role": "user", "content": "u"}]
    pkt = pack_within_budget(
        system_blocks=blocks, messages=msgs,
        max_response_tokens=200, model="m",
    )
    # Optionals should be dropped before any core block.
    optional_names = {"opt-low", "opt-hi"}
    core_dropped = [n for n in pkt.dropped_blocks if n not in optional_names]
    optional_dropped = [n for n in pkt.dropped_blocks if n in optional_names]
    if core_dropped:
        # If we dropped any core, both optionals must have already been gone.
        assert set(optional_dropped) == optional_names


def test_keeps_at_least_one_core_block():
    """The packer must never empty the system prompt — keep ≥1 core block
    even if that means raising ContextOverflowError later."""
    blocks = [
        {"name": "core1", "text": "x" * 1000, "kind": "core", "score": 0.0},
        {"name": "core2", "text": "x" * 1000, "kind": "core", "score": 0.5},
    ]
    msgs = [{"role": "user", "content": "u"}]
    with pytest.raises(ContextOverflowError):
        pack_within_budget(
            system_blocks=blocks, messages=msgs,
            max_response_tokens=200, model="m",
        )


# ── Drop oldest history turns ────────────────────────────────────────────────

def test_drops_oldest_history_preserving_final_user_msg():
    # Budget: 1000 - 100 (response) - 256 (headroom) = 644
    # Combined: 4 (core) + 5 (## core\n) + 100*4 (history) + 600 (last) > 644
    big = "Q" * 600
    blocks = [{"name": "core", "text": "core", "kind": "core"}]
    msgs = [
        {"role": "user",      "content": "X" * 100},
        {"role": "assistant", "content": "Y" * 100},
        {"role": "user",      "content": "X" * 100},
        {"role": "assistant", "content": "Y" * 100},
        {"role": "user",      "content": big},  # final user msg — must survive
    ]
    pkt = pack_within_budget(
        system_blocks=blocks, messages=msgs,
        max_response_tokens=100, model="m",
    )
    # Final user msg always present
    assert pkt.messages[-1]["content"] == big
    assert pkt.dropped_history_turns >= 1


def test_overflow_when_final_user_message_too_big():
    blocks = [{"name": "core", "text": "core", "kind": "core"}]
    huge = "Z" * 5000  # exceeds even the model window alone
    msgs = [{"role": "user", "content": huge}]
    with pytest.raises(ContextOverflowError):
        pack_within_budget(
            system_blocks=blocks, messages=msgs,
            max_response_tokens=200, model="m",
        )


# ── Counters ─────────────────────────────────────────────────────────────────

def test_counters_increment_on_drop():
    cp._reset_pack_counters_for_tests()
    blocks = [
        {"name": "core",    "text": "c" * 100, "kind": "core"},
        {"name": "opt",     "text": "o" * 600, "kind": "optional"},
    ]
    msgs = [{"role": "user", "content": "u"}]
    pack_within_budget(
        system_blocks=blocks, messages=msgs,
        max_response_tokens=100, model="m",
    )
    counters = cp.get_pack_counters()
    assert counters["context_overflow_dropped_chunks_total"] >= 1


# ── assert_within_context ────────────────────────────────────────────────────

def test_assert_within_context_passes_for_small_prompt():
    n = assert_within_context(
        system="abc", messages=[{"role": "user", "content": "u"}],
        max_response=100, model="m",
    )
    assert n >= 4  # "abc" + "u"


def test_assert_within_context_raises_on_overflow():
    with pytest.raises(ContextOverflowError):
        assert_within_context(
            system="x" * 900,
            messages=[{"role": "user", "content": "y" * 200}],
            max_response=100, model="m",
        )


def test_overflow_error_carries_structured_payload():
    try:
        assert_within_context(
            system="x" * 900,
            messages=[{"role": "user", "content": "y" * 200}],
            max_response=100, model="m",
        )
    except ContextOverflowError as e:
        assert e.model == "m"
        assert e.model_max == 1000
        assert e.prompt_tokens >= 1100
        assert e.max_response == 100
    else:
        pytest.fail("ContextOverflowError not raised")


# ── trim_messages_to_fit — the dispatch-layer last resort ────────────────────
#
# Wired into call_llm / stream_llm, so it runs when the agentic loop's own
# compaction has already failed to make room. `_compact_messages` deliberately
# never removes messages[0] (the brief) — it iterates from index 1 — because the
# loop pins its deterministic state there (_pin_ground_truth, and the
# unsummarized-eviction notice). This trim must honour the same invariant, or
# the last-resort path strips the harness's own record at the exact moment the
# model is least able to notice the hole.

def test_trim_keeps_the_brief_and_the_final_user_turn():
    from app.agents._context_packing import trim_messages_to_fit
    brief = {"role": "user", "content": "BRIEF" + "b" * 195}
    msgs = [brief,
            {"role": "assistant", "content": "a" * 200},
            {"role": "user", "content": "u" * 200},
            {"role": "assistant", "content": "c" * 200},
            {"role": "user", "content": "FINAL" + "f" * 195}]
    out, dropped = trim_messages_to_fit(None, msgs, max_response=100, model="m")
    assert dropped == 2
    assert out[0]["content"].startswith("BRIEF")      # the brief survives
    assert out[-1]["content"].startswith("FINAL")     # so does the final user turn
    assert len(out) == 3
    assert msgs[0] is brief and len(msgs) == 5        # caller's list is not mutated


def test_trim_drops_a_tool_use_pair_together(monkeypatch):
    """Pair-safety must still hold now that dropping starts at index 1: orphaning
    either half of a tool_use/tool_result pair is an API 400."""
    from app.agents._context_packing import trim_messages_to_fit

    def count(system, messages, model=None):
        n = len(system or "")
        for m in messages:
            c = m.get("content")
            n += len(c) if isinstance(c, str) else 200
        return n
    monkeypatch.setattr(cp, "count_messages_tokens", count)

    msgs = [{"role": "user", "content": "B" * 200},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]},
            {"role": "assistant", "content": "a" * 100},
            {"role": "user", "content": "F" * 100}]
    out, dropped = trim_messages_to_fit(None, msgs, max_response=100, model="m")
    assert dropped == 2                                # both halves, never one
    assert out[0]["content"].startswith("B")           # brief still first
    ids = [b.get("id") for m in out if isinstance(m.get("content"), list)
           for b in m["content"] if b.get("type") == "tool_use"]
    answered = [b.get("tool_use_id") for m in out if isinstance(m.get("content"), list)
                for b in m["content"] if b.get("type") == "tool_result"]
    assert ids == answered == []                       # no orphan left behind
