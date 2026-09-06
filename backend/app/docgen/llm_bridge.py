# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""LangChain ↔ the platform LLM bridge.

The docgen pipeline's pipeline talks to LLMs through LangChain's `ChatModel.invoke(messages)`
interface (returning a response object with `.content`). The platform already has
`app.core.llm.call_llm(system, messages, max_tokens)` which is async and
already routes through Anthropic Claude / OpenAI / Ainxt with retry handling.

This bridge provides a thin **synchronous** LangChain-compatible shim that
delegates every call to the platform's `call_llm`. Used by `pipeline.py` in place
of `ChatOllama` / `ChatOpenAI`.

Why sync? The docgen pipeline's pipeline runs on a worker thread (LangGraph executes nodes
synchronously) and uses plain `llm.invoke(...)`. Adding async would require
rewriting the entire pipeline. The bridge simply runs the async call_llm
inside a fresh event loop on a helper thread.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import logging
from typing import Any, Iterable

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)

from app.core.llm import call_llm

logger = logging.getLogger(__name__)


# ── Single shared executor for all bridge sync calls ─────────────────────────
# Reused across invocations so we don't pay thread-spawn cost per LLM call.
# Sized for a small fan-out (matches MAX_PARALLEL_SECTIONS default of 3 with headroom).
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8,
    thread_name_prefix="docgen-llm-bridge",
)


class _Response:
    """Mimics `langchain_core.messages.AIMessage` enough for the docgen pipeline."""

    __slots__ = ("content",)

    def __init__(self, content: str):
        self.content = content


def _convert_messages(messages: Iterable[BaseMessage]) -> tuple[str | list, list[dict]]:
    """LangChain BaseMessage list → (system, messages_list).

    A SystemMessage's content may be a plain string OR a pre-built list of Anthropic
    system segments ({"type": "text", "text": ..., "cache_control": ...}) — segments
    pass through verbatim so docgen callers can mark stable prefixes cacheable
    (`call_llm` forwards segment lists to Claude untouched; non-Claude providers get
    them flattened to one string by `_coerce_system_to_str`). Plain-string system
    messages keep the legacy joined-string behaviour byte-for-byte."""
    system_parts: list = []          # str | segment-dict, in arrival order
    out: list[dict] = []
    for m in messages:
        content = m.content if hasattr(m, "content") else str(m)
        if isinstance(m, SystemMessage):
            if isinstance(content, list):
                system_parts.extend(s for s in content if isinstance(s, dict) and s.get("text"))
            else:
                system_parts.append(content)
        elif isinstance(m, HumanMessage):
            out.append({"role": "user", "content": content})
        elif isinstance(m, AIMessage):
            out.append({"role": "assistant", "content": content})
        else:
            # Unknown / generic — treat as user
            out.append({"role": "user", "content": content})
    if any(isinstance(p, dict) for p in system_parts):
        segs = [p if isinstance(p, dict) else {"type": "text", "text": p}
                for p in system_parts if (p.get("text") if isinstance(p, dict) else p.strip())]
        return segs, out
    return "\n\n".join(system_parts).strip(), out


def _run_sync(coro):
    """Run an async coroutine from sync code, even if a loop is already running.

    LangGraph nodes execute synchronously. If the *outer* code happens to be
    in an asyncio loop (unlikely but possible), `asyncio.run` would explode —
    so we always punt to a worker thread that owns its own loop.

    NOTE: we use `selector_event_loop` (the stdlib default) explicitly rather
    than relying on `asyncio.run`'s default. uvloop is faster but its
    TCPTransport cleanup interacts badly with httpx async cleanup callbacks
    when the loop is torn down at the end of a one-shot run — we'd see
    `RuntimeError: unable to perform operation on <TCPTransport closed=True>`
    spam on every call. The selector loop has no such issue and the perf
    delta is invisible compared to the LLM round-trip latency.
    """
    def _runner():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)
        finally:
            try:
                # Cancel any orphaned tasks so cleanup callbacks don't fire
                # against a torn-down transport.
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            asyncio.set_event_loop(None)
            loop.close()

    # Run on a copy of the CURRENT context so contextvars set by the caller (e.g. the
    # usage-ledger change/section tag from docgen_runner) reach call_llm in the executor
    # thread — ThreadPoolExecutor.submit does not propagate context on its own.
    ctx = contextvars.copy_context()
    return _EXECUTOR.submit(ctx.run, _runner).result()


class _ClaudeShim:
    """Synchronous LangChain-compatible LLM that proxies to the platform's call_llm.

    Supports the JSON-mode hint by appending a system instruction (Claude
    reliably emits valid JSON when asked unambiguously).

    `agent_name` is forwarded to call_llm so Slice 28 observability traces
    tag every docgen call with a meaningful name (e.g. "brd_docgen_plan")
    instead of falling back to "unknown".
    """

    def __init__(
        self,
        json_mode: bool = False,
        max_tokens: int = 12000,
        label: str = "docgen",
        agent_name: str = "docgen_bridge",
    ):
        self.json_mode = json_mode
        self.max_tokens = max_tokens
        self.label = label
        self.agent_name = agent_name

    def invoke(self, messages, **_kwargs) -> _Response:  # signature matches LangChain
        system_text, msg_list = _convert_messages(messages)
        if self.json_mode:
            json_directive = (
                "\n\nIMPORTANT: Respond with ONLY valid JSON. "
                "Start directly with `{` and end with `}`. "
                "Do not wrap in markdown code fences. Do not add commentary before or after."
            )
            if isinstance(system_text, list):
                # Segmented system: the directive rides as a final UNMARKED segment so it
                # never perturbs the cacheable prefix segments before it.
                system_text = system_text + [{"type": "text", "text": json_directive.strip()}]
            else:
                system_text = (system_text + json_directive).strip()

        if not msg_list:
            # LangChain allows empty user msgs when system carries everything; the platform expects at least one.
            msg_list = [{"role": "user", "content": "Proceed."}]

        # The cached Anthropic / OpenAI / AiNxt async clients in app.core.llm
        # bind their httpx transports to the event loop they were first
        # created on. Reusing them inside a fresh per-call event loop (that's
        # what _run_sync does) leaks the binding and produces
        # `RuntimeError: unable to perform operation on <TCPTransport closed=True>`
        # at cleanup time. Drop the lru_cache entries so a new client is
        # constructed under the current loop on each invoke.
        try:
            from app.core import llm as _llm_module
            for fn_name in ("_get_anthropic_client", "_get_openai_client"):
                fn = getattr(_llm_module, fn_name, None)
                if fn is not None and hasattr(fn, "cache_clear"):
                    fn.cache_clear()
        except Exception:
            pass

        try:
            content = _run_sync(call_llm(
                system_text, msg_list, self.max_tokens,
                agent_name=self.agent_name,
            ))
        except Exception as e:
            logger.error("[docgen-llm-bridge:%s] call_llm failed: %s", self.label, e)
            raise

        return _Response(content)


# ── Factory functions used by pipeline.py ────────────────────────────────────

def make_llm_json(max_tokens: int = 12000, agent_name: str = "docgen_bridge") -> _ClaudeShim:
    """JSON-mode LLM. Pipeline uses this for plan + structured section content.
    Default 12k (was 4k) — required for dense per-section payloads (XML + field
    tables + error tables + edge cases) without mid-string JSON truncation.
    """
    return _ClaudeShim(json_mode=True, max_tokens=max_tokens, label="json", agent_name=agent_name)


def make_llm_content(max_tokens: int = 8000, agent_name: str = "docgen_bridge") -> _ClaudeShim:
    """Free-form LLM. Pipeline uses this for prose, PlantUML source, etc."""
    return _ClaudeShim(json_mode=False, max_tokens=max_tokens, label="content", agent_name=agent_name)


def make_llm(max_tokens: int = 4000, agent_name: str = "docgen_bridge") -> _ClaudeShim:
    """Generic free-form LLM (alias for make_llm_content for docgen compatibility)."""
    return _ClaudeShim(json_mode=False, max_tokens=max_tokens, label="default", agent_name=agent_name)
