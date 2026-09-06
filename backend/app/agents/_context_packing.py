# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Context packing — fit (system + history + RAG context) into model budget.

Phase 0.3 of the indexing/retrieval implementation plan.

Agents historically built one giant system-prompt string and passed it to
`stream_llm` without checking total tokens vs. model context. The result
was either silent provider truncation (Anthropic returns `stop_reason=
"max_tokens"`) or a 400 from the provider when the input itself was too
big.

This module exposes one function — `pack_within_budget()` — that:

  1. Counts tokens of every input segment via `app.core.tokens`.
  2. Reserves room for `max_response`.
  3. If the input fits, returns it unchanged.
  4. Otherwise drops content in a controlled order:
       a. RAG / context blocks marked `optional`, lowest score first.
       b. RAG / context blocks marked `core`, lowest score first.
       c. Oldest conversation-history turns (preserve newest user msg).
  5. Emits a structured warning log + counters so ops can see how often
     packing actually trims things.

The packer never alters the system *boilerplate* or the most recent user
message — those are mandatory.

Callers should:
    packed = pack_within_budget(
        system_blocks=[{"name": "rules",  "text": rules,    "kind": "core"},
                       {"name": "rag",    "text": rag_ctx,  "kind": "optional",
                        "score": 0.4},
                       ...],
        messages=conversation_history + [{"role": "user", "content": new_msg}],
        max_response_tokens=4000,
        model="claude-sonnet-5",
    )
    system_str, messages = packed.system, packed.messages
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.tokens import count_messages_tokens, count_tokens, model_context_window

logger = logging.getLogger(__name__)


class ContextOverflowError(RuntimeError):
    """Raised when even after packing the prompt cannot fit the model.

    Carries a structured payload so observers can dashboard the failure
    without parsing the message.
    """

    def __init__(
        self,
        *,
        model: str,
        model_max: int,
        prompt_tokens: int,
        max_response: int,
    ):
        self.model = model
        self.model_max = model_max
        self.prompt_tokens = prompt_tokens
        self.max_response = max_response
        super().__init__(
            f"Context overflow: prompt={prompt_tokens} + response={max_response} "
            f"exceeds {model}'s {model_max}-token window even after packing."
        )


@dataclass
class PackedContext:
    """Result of `pack_within_budget`.

    `system` is the joined system-prompt string (joined with `\\n\\n---\\n\\n`),
    or `None` when the caller passed segmented blocks intended to be sent
    as Anthropic-style `system=[{type:"text", ...}]` (Phase 6.2). For now
    we always emit a string and let Phase 6.2 swap to segmented form.
    """
    system: str
    messages: list[dict]
    prompt_tokens: int
    response_budget: int
    model: str
    dropped_blocks: list[str] = field(default_factory=list)
    dropped_history_turns: int = 0


# Counters — surfaced via observability metrics in Phase 0.4 / Phase 7.
# Process-local; reset by `_reset_pack_counters_for_tests`.
_PACK_DROP_BLOCKS = 0
_PACK_DROP_TURNS = 0


def _reset_pack_counters_for_tests() -> None:
    """Test hook only."""
    global _PACK_DROP_BLOCKS, _PACK_DROP_TURNS
    _PACK_DROP_BLOCKS = 0
    _PACK_DROP_TURNS = 0


def get_pack_counters() -> dict[str, int]:
    """Snapshot of dropped-block / dropped-turn counters since process start."""
    return {
        "context_overflow_dropped_chunks_total": _PACK_DROP_BLOCKS,
        "context_overflow_dropped_turns_total":  _PACK_DROP_TURNS,
    }


def _has_tool_use(m) -> bool:
    c = (m or {}).get("content")
    return isinstance(c, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_use" for b in c)


def _is_tool_result(m) -> bool:
    c = (m or {}).get("content")
    return isinstance(c, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in c)


def _join_blocks(blocks: list[dict]) -> str:
    """Concatenate segment texts in order, separated by a small ruler so
    the model sees clear section boundaries."""
    parts: list[str] = []
    for b in blocks:
        txt = b.get("text") or ""
        if not txt:
            continue
        name = b.get("name")
        if name:
            parts.append(f"## {name}\n{txt}")
        else:
            parts.append(txt)
    return "\n\n---\n\n".join(parts)


def pack_within_budget(
    *,
    system_blocks: list[dict],
    messages: list[dict],
    max_response_tokens: int,
    model: str,
    headroom_tokens: int = 256,
) -> PackedContext:
    """Trim system blocks + history until the prompt fits the model window.

    `system_blocks` is a list of dicts:
      - name:  optional human label, included as `## name` header.
      - text:  the segment body.
      - kind:  "core" or "optional". Optional blocks drop first.
      - score: float, lower scores drop first within a kind.

    Drop order:
      1. optional blocks, ascending score
      2. core blocks, ascending score
      3. oldest message turns (never drop the final user turn)

    Raises `ContextOverflowError` if no combination fits.
    """
    global _PACK_DROP_BLOCKS, _PACK_DROP_TURNS

    if not isinstance(system_blocks, list):
        raise TypeError("system_blocks must be a list of dicts")
    if not isinstance(messages, list):
        raise TypeError("messages must be a list of dicts")

    blocks = [dict(b) for b in system_blocks]  # shallow copy — never mutate caller
    msgs = list(messages)
    model_max = model_context_window(model)
    budget = model_max - max_response_tokens - headroom_tokens
    if budget <= 0:
        raise ContextOverflowError(
            model=model, model_max=model_max,
            prompt_tokens=0, max_response=max_response_tokens,
        )

    dropped_blocks: list[str] = []
    dropped_turns = 0

    def _measure() -> int:
        return count_messages_tokens(_join_blocks(blocks), msgs, model=model)

    # First measurement.
    prompt_tokens = _measure()
    if prompt_tokens <= budget:
        return PackedContext(
            system=_join_blocks(blocks),
            messages=msgs,
            prompt_tokens=prompt_tokens,
            response_budget=max_response_tokens,
            model=model,
        )

    # ── Drop step 1: optional blocks, ascending score ─────────────────────────
    def _next_optional_index() -> int:
        candidates = [
            (i, b.get("score") or 0.0)
            for i, b in enumerate(blocks)
            if (b.get("kind") or "core") == "optional"
        ]
        if not candidates:
            return -1
        candidates.sort(key=lambda x: x[1])
        return candidates[0][0]

    while prompt_tokens > budget:
        idx = _next_optional_index()
        if idx < 0:
            break
        dropped = blocks.pop(idx)
        dropped_blocks.append(dropped.get("name") or "<unnamed>")
        _PACK_DROP_BLOCKS += 1
        prompt_tokens = _measure()

    if prompt_tokens <= budget:
        if dropped_blocks:
            logger.warning(
                "Context packer dropped %d optional blocks to fit %s budget "
                "(prompt=%d budget=%d). Dropped: %s",
                len(dropped_blocks), model, prompt_tokens, budget, dropped_blocks,
            )
        return PackedContext(
            system=_join_blocks(blocks), messages=msgs,
            prompt_tokens=prompt_tokens,
            response_budget=max_response_tokens, model=model,
            dropped_blocks=dropped_blocks,
        )

    # ── Drop step 2: core blocks, ascending score ─────────────────────────────
    def _next_core_index() -> int:
        candidates = [
            (i, b.get("score") or 0.0)
            for i, b in enumerate(blocks)
            if (b.get("kind") or "core") == "core"
        ]
        # Never drop the very last remaining block — guarantees the model
        # gets *some* system prompt rather than a blank one.
        if len(candidates) <= 1:
            return -1
        candidates.sort(key=lambda x: x[1])
        return candidates[0][0]

    while prompt_tokens > budget:
        idx = _next_core_index()
        if idx < 0:
            break
        dropped = blocks.pop(idx)
        dropped_blocks.append(dropped.get("name") or "<unnamed>")
        _PACK_DROP_BLOCKS += 1
        prompt_tokens = _measure()

    # ── Drop step 3: oldest history turns (preserve final user msg) ───────────
    # Find the final user-role message; never drop anything at or after it.
    last_user_idx = -1
    for i in range(len(msgs) - 1, -1, -1):
        if (msgs[i] or {}).get("role") == "user":
            last_user_idx = i
            break

    while prompt_tokens > budget:
        # Drop the oldest message that is BEFORE last_user_idx — PAIR-SAFE: an assistant
        # tool_use and the user tool_result that answers it are one protocol unit; dropping
        # only one side leaves an orphan the Anthropic API rejects outright (every tool_use
        # id needs a matching tool_result, and vice versa).
        if last_user_idx <= 0:
            break
        if _has_tool_use(msgs[0]) and len(msgs) > 1 and _is_tool_result(msgs[1]):
            if last_user_idx < 2:
                break              # the pair abuts the final user turn — nothing droppable
            n_pop = 2
        else:
            n_pop = 1
        for _ in range(n_pop):
            msgs.pop(0)
            last_user_idx -= 1
            dropped_turns += 1
            _PACK_DROP_TURNS += 1
        prompt_tokens = _measure()

    if prompt_tokens > budget:
        logger.error(
            "Context packer FAILED to fit budget — model=%s prompt=%d budget=%d "
            "model_max=%d dropped_blocks=%d dropped_turns=%d",
            model, prompt_tokens, budget, model_max,
            len(dropped_blocks), dropped_turns,
        )
        raise ContextOverflowError(
            model=model, model_max=model_max,
            prompt_tokens=prompt_tokens, max_response=max_response_tokens,
        )

    if dropped_blocks or dropped_turns:
        logger.warning(
            "Context packer trimmed prompt to fit %s — prompt=%d budget=%d "
            "dropped_blocks=%d dropped_turns=%d names=%s",
            model, prompt_tokens, budget,
            len(dropped_blocks), dropped_turns, dropped_blocks,
        )

    return PackedContext(
        system=_join_blocks(blocks),
        messages=msgs,
        prompt_tokens=prompt_tokens,
        response_budget=max_response_tokens,
        model=model,
        dropped_blocks=dropped_blocks,
        dropped_history_turns=dropped_turns,
    )


# ── Lightweight assertion helper used by call_llm / stream_llm ────────────────

def assert_within_context(
    system: str | list[dict] | None,
    messages: list[dict],
    max_response: int,
    model: str,
    *,
    headroom_tokens: int = 256,
) -> int:
    """Pre-call gate: raise `ContextOverflowError` if the prompt won't fit.

    Designed for the LLM dispatch layer (`call_llm` / `stream_llm`) — it
    does NOT trim. Trimming is the agent's job via `pack_within_budget`.
    Returns the measured prompt-token count on success so the caller can
    log it.
    """
    model_max = model_context_window(model)
    prompt_tokens = count_messages_tokens(system, messages, model=model)
    if prompt_tokens + max_response + headroom_tokens > model_max:
        raise ContextOverflowError(
            model=model, model_max=model_max,
            prompt_tokens=prompt_tokens, max_response=max_response,
        )
    return prompt_tokens


def trim_messages_to_fit(
    system: str | list[dict] | None,
    messages: list[dict],
    max_response: int,
    model: str,
    *,
    headroom_tokens: int = 256,
) -> tuple[list[dict], int]:
    """LAST-RESORT dispatch-layer trim for `call_llm` / `stream_llm` when the pre-call
    gate overflows: drop the OLDEST history turns — pair-safe (an assistant tool_use and
    its user tool_result drop together; orphaning either side is an API 400), never the
    final user turn, and never message[0] — until the prompt fits. System content is
    untouched (the dispatch layer only has it as an opaque string/segments).

    WHY message[0] IS KEPT: in an agentic run it is the BRIEF, and the loop pins its
    deterministic state there — `_pin_ground_truth` (edited files, planned files) and the
    unsummarized-eviction notice both rewrite `messages[0]`. `_compact_messages` protects
    it for that reason (it iterates from index 1 and never removes a message). Dropping it
    here would strip the harness's own record at the one moment the model is most
    context-starved — i.e. exactly when it is least able to notice the hole.

    Returns ``(messages, dropped_turns)`` — the ORIGINAL list object when nothing needed
    dropping. Raises `ContextOverflowError` when trimming cannot fit (a single huge turn,
    no droppable history): those callers keep today's hard-failure behaviour."""
    global _PACK_DROP_TURNS
    model_max = model_context_window(model)
    budget = model_max - max_response - headroom_tokens
    prompt_tokens = count_messages_tokens(system, messages, model=model)
    if prompt_tokens <= budget:
        return messages, 0

    msgs = list(messages)
    last_user_idx = -1
    for i in range(len(msgs) - 1, -1, -1):
        if (msgs[i] or {}).get("role") == "user":
            last_user_idx = i
            break
    dropped = 0
    # Drop from index 1: index 0 is the retained brief (see docstring).
    while prompt_tokens > budget and last_user_idx > 1:
        if _has_tool_use(msgs[1]) and len(msgs) > 2 and _is_tool_result(msgs[2]):
            if last_user_idx < 3:
                break              # the pair abuts the final user turn — nothing droppable
            n_pop = 2
        else:
            n_pop = 1
        for _ in range(n_pop):
            msgs.pop(1)
            last_user_idx -= 1
            dropped += 1
            _PACK_DROP_TURNS += 1
        prompt_tokens = count_messages_tokens(system, msgs, model=model)

    if prompt_tokens > budget:
        raise ContextOverflowError(
            model=model, model_max=model_max,
            prompt_tokens=prompt_tokens, max_response=max_response,
        )
    logger.warning(
        "trim_messages_to_fit: dropped %d oldest history turn(s) to fit %s "
        "(prompt=%d budget=%d)", dropped, model, prompt_tokens, budget)
    return msgs, dropped
