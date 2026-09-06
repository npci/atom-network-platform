# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The bounded agentic loop (THE BOOK §8).

``read → think → tool → result → repeat``, capped at ``agentic_max_iterations``.
One shared core, configured per subagent by ``(system_prompt, tool_subset,
model, caps)`` — XSD-Discovery, Code-Change, Verification, and Review (S9-S11)
all drive this same loop with different tool subsets and prompts.

Responsibilities here (the loop), distinct from the tools (which do the work):
* drive :func:`call_claude_tools` (S5) with tools+system caching;
* dispatch each ``tool_use`` through :func:`agentic_tools.execute_tool`;
* enforce **turn-1 submit_plan** (no mutation before a plan);
* honour **cancellation** + the **iteration cap** at every boundary;
* persist every turn + tool call to ``agentic_events`` (S2/§21);
* return the accumulated ``ChangeSet`` (FileOps) + final text.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.agents import agentic_tools as T
from app.agents.agentic_events import emit_event
from app.core.config import settings
from app.core.llm import call_claude_tools, tool_result_block

logger = logging.getLogger("app.agentic")

_MUTATING = {"edit_file", "create_file", "delete_file"}

# Hard per-request output ceiling for the truncation-retry's doubled budget (Claude
# Sonnet 4.x caps output at 64K). An unclamped double past the model's limit 400s —
# converting a recoverable truncation into a run failure.
_MODEL_MAX_OUTPUT_TOKENS = 64_000
# Event payload cap. Generous enough that the collapsible UI shows a useful, near-
# complete detail (summaries, diffs, error blocks); the truly-full output still
# lives in the LLM transcript. Bounded so a 200KB file read can't bloat the feed.
_HEAD = 4000


@dataclass
class RuntimeResult:
    final_text: str
    change_set: list[T.FileOp]
    plan: dict | None
    iterations: int
    stopped: str                       # "completed" | "max_iterations" | "cancelled" | "awaiting_decision" | "budget_exceeded"
    transcript: list[dict] = field(default_factory=list)
    read_files: list = field(default_factory=list)   # (repo_id, path) read this batch — memory for continuation
    proposal: dict | None = None       # reuse-vs-new options (propose_approach) for the human gate
    concerns: list = field(default_factory=list)      # disruptive-change declines (flag_concern)
    facts: list = field(default_factory=list)         # fact sheet (record_fact + auto-facts) — phase handoff memory
    # SDLC review gaps 7/8/9/11 — the structural-intel tokens this batch queried
    # (ctx.intel_queried: "symbol:<name>" / "path:<repo>:<path>"), so the mandatory
    # cross-module analysis gate can check WHICH symbols the agent actually ran
    # callers()/impact_analysis()/symbol_graph() on before editing, not just whether
    # intel_gate_reason's per-edit check fired once for the batch.
    intel_queried: list = field(default_factory=list)
    # Code-phase schema writes captured as amendment PROPOSALS rather than applied
    # (T._stage_schema_write). Non-empty ⇒ the run has a schema change waiting on a
    # human decision; the orchestrator parks at awaiting_schema_amendment.
    schema_amendments: list = field(default_factory=list)


def _head(s: str | None) -> str:
    s = s or ""
    return s[:_HEAD] + ("…" if len(s) > _HEAD else "")


# ── DEBUG: full per-call transcript dump (settings.agentic_dump_transcripts) ──────
#
# The coding log (agentic_events) only keeps 4000-char heads of each turn/tool call.
# When chasing "what EXACTLY did the model see and return on call N", flip
# AGENTIC_DUMP_TRANSCRIPTS=true and every agentic-loop LLM call writes one JSON file
# with the VERBATIM system prompt, the full messages array sent (packed context +
# growing transcript), the tool set, and the full response (text + thinking +
# tool_use inputs + usage). Fail-open + secret-redacted; OFF in prod.

def _dump_transcript(run_id: str, agent_name: str, iteration: int,
                     system, messages: list[dict], tools: list[dict], turn) -> None:
    """Write the full prompt+response of ONE agentic-loop LLM call into the per-change
    transcript tree: <change_id>/08_codegen/<run_id>/<agent>/iterNNN_<ms>.json.

    `messages` must be the exact array that was sent (call this BEFORE the assistant
    turn is appended and BEFORE compaction mutates it). Best-effort; never raises.

    Gated by `transcript_capture` OR the legacy `agentic_dump_transcripts` toggle (both OFF
    by default — opt-in). change_id is resolved from the usage contextvar the orchestrator
    sets around the drive; falls back to `unassigned/` so a dump is never silently lost."""
    from app.core import transcripts as _transcripts
    if not (getattr(settings, "agentic_dump_transcripts", False) or _transcripts.enabled()):
        return
    try:
        import time as _t
        d = _transcripts.codegen_dir(run_id, agent_name)
        if d is None:
            return
        record = {
            "change_id": _transcripts.current_change_id(),
            "run_id": run_id, "agent": agent_name, "iteration": iteration,
            "ts": _t.strftime("%Y-%m-%dT%H:%M:%S"),
            # The verbatim prompt: cached system segments (role/module/docs) + the full
            # messages array (packed user prompt + every prior tool_result — i.e. the
            # exact context this call saw).
            "system": system,
            "messages": messages,
            "tools_available": [t.get("name") for t in (tools or []) if isinstance(t, dict)],
            # The full response for this call.
            "response": {
                "stop_reason": getattr(turn, "stop_reason", None),
                "text": getattr(turn, "text", "") or "",
                "thinking": getattr(turn, "thinking", "") or "",
                "tool_uses": [{"name": tu.name, "input": tu.input} for tu in getattr(turn, "tool_uses", [])],
                "usage": getattr(turn, "usage", None) or {},
            },
        }
        # Timestamp FIRST so lexical order == chronological order. The loop's `iteration`
        # restarts at 001 on every fresh pass, and several passes share this one
        # <run_id>/<agent>/ folder — sorting on `iterNNN` alone interleaves them. The
        # iteration is kept after the stamp because it is still useful context.
        # Redaction (per-STRING, so a greedy secret rule can't eat a JSON delimiter),
        # serialize, and the disk write all run off the event loop — see offload_write.
        path = d / f"{_transcripts.stamp()}_iter{iteration:03d}.json"
        _transcripts.offload_write(path, record)
    except Exception as exc:  # noqa: BLE001 — a debug dump must never break the loop
        logger.warning("transcript dump failed (run=%s agent=%s iter=%d): %s",
                       run_id, agent_name, iteration, exc)


def _looks_degenerate(text: str) -> bool:
    """Doom-loop-lite (P2, grok-build parity): detect tail repetition — the model stuck emitting
    the same chunk over and over (grok's server flags this as `tail_repetition:N`; we detect it
    client-side). True when the last 3000 chars END with the same ≥12-char unit repeated ≥6×
    consecutively spanning ≥240 chars. Thresholds are deliberately high: legitimate prose can
    repeat a short phrase, but 6 exact consecutive copies of a 12+-char unit at the very end of
    the output is degeneration. Pure string scan — no regex backreferences (no pathological
    backtracking)."""
    tail = (text or "")[-3000:]
    n = len(tail)
    if n < 240:
        return False
    for p in range(12, 401):
        if 6 * p > n:
            break
        unit = tail[n - p:]
        reps = 1
        while n - (reps + 1) * p >= 0 and tail[n - (reps + 1) * p: n - reps * p] == unit:
            reps += 1
        if reps >= 6 and reps * p >= 240:
            return True
    return False


def _perturbed_for_retry(messages: list[dict], note: str) -> list[dict]:
    """Copy of the transcript with a corrective harness note appended to the LAST user turn,
    for a discarded-turn retry. Resampling identical input is no remedy behind a gateway that
    forces temperature=0 (AiNxt injects it for sonnet-4-6/haiku — docs/ainxt_thinking_fidelity.md
    matrix #7): greedy decoding reproduces the same bad turn verbatim. Changing the input is the
    only lever left. The LIVE history is untouched — the note exists only in the retried call,
    so the accepted turn continues a clean transcript."""
    if not messages:
        return messages
    last = messages[-1]
    c = last.get("content")
    if isinstance(c, list):
        last = {**last, "content": list(c) + [{"type": "text", "text": note}]}
    else:
        last = {**last, "content": f"{c or ''}\n\n{note}"}
    return messages[:-1] + [last]


def _is_context_overflow(e: Exception) -> bool:
    """Heuristic: does this error mean the request exceeded the model's context window?
    (Anthropic 400 reads 'prompt is too long: N tokens > M maximum'.) Used to compact-and-
    retry rather than fail the run — context is managed, never a hard stop (§8)."""
    s = str(e).lower()
    return any(k in s for k in ("prompt is too long", "context window", "context length",
                                "maximum context length", "too many tokens", "input is too long"))


_EVICT_MIN_CHARS = 400      # don't bother stubbing an already-small tool result


def _history_tool_chars(messages: list[dict]) -> int:
    """Total chars of tool-result content still in the conversation. This is the bulk that gets
    re-sent (uncached) every turn — the metric that reveals context rot / token growth on a run."""
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("content"), str):
                    total += len(b["content"])
    return total


def _total_msg_chars(messages: list[dict]) -> int:
    """Chars of ALL message content (prose + tool blocks) — the input-size proxy for the
    anchored token estimate below. Rough (str() over block dicts) but monotone with what
    the provider actually tokenizes, which is all the estimator needs."""
    total = 0
    for m in messages:
        c = m.get("content")
        total += len(c) if isinstance(c, str) else len(str(c or ""))
    return total


def _estimated_input_tokens(messages: list[dict], last_known_in: int, chars_at_anchor: int,
                            base_chars: int = 0) -> int:
    """Anchored input estimate (grok-build parity): the provider's REPORTED input total for the
    last call is ground truth for everything sent then; only the chars appended SINCE are
    estimated (~4 chars/token). A whole-history chars/4 guess drifts over a long session; the
    anchor resets that drift every turn that reports usage. With no anchor yet (usage-stripping
    provider, first call), the whole history is estimated — ``base_chars`` (system prompt +
    tool schemas, which the provider DOES tokenize) must ride the estimate there or the
    unanchored path structurally undercounts."""
    chars_now = _total_msg_chars(messages)
    if last_known_in > 0:
        return last_known_in + max(0, chars_now - chars_at_anchor) // 4
    return (chars_now + max(0, base_chars)) // 4


def _policy_window(model: str | None) -> int:
    """Effective context-window POLICY for this call: the configured policy clamped to the
    ACTUAL model's window. The config value is a cost policy sized for 1M-window models —
    on a smaller-window model (Haiku 200K, a 128K gateway model) an unclamped 'policy'
    fires only after the real window has already overflowed."""
    try:
        from app.core.tokens import model_context_window
        return min(int(settings.agentic_context_window_tokens), int(model_context_window(model)))
    except Exception:  # noqa: BLE001 — policy fallback, never breaks the loop
        return int(settings.agentic_context_window_tokens)


def _compact_messages(messages: list[dict], keep_tail: int) -> int:
    """Evict the BULK of OLD tool-result outputs (reconstructable by re-reading) while keeping every
    assistant turn (the reasoning), the first message (the brief), and the last ``keep_tail`` messages
    verbatim. Returns chars reclaimed. Lossless-by-reconstruction: the read_file content cache serves
    an unchanged re-read, and the stub tells the model to re-read/re-run if it still needs the output.
    Deterministic — it never paraphrases reasoning, so it can't mangle what the agent worked out."""
    reclaimed = 0
    for idx in range(1, max(1, len(messages) - keep_tail)):
        m = messages[idx]
        if m.get("role") != "user" or not isinstance(m.get("content"), list):
            continue
        for block in m["content"]:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            txt = block.get("content")
            if isinstance(txt, str) and len(txt) > _EVICT_MIN_CHARS and not txt.startswith("[evicted "):
                reclaimed += len(txt)
                block["content"] = (f"[evicted earlier tool output ({len(txt)} chars) to save context — "
                                    "re-read the file or re-run the tool if you still need it]")
    return reclaimed


def _reclaimable_tool_chars(messages: list[dict], keep_tail: int) -> int:
    """Chars ``_compact_messages`` WOULD reclaim right now — non-stub tool outputs above the
    evict floor, outside the kept tail. Zero means the history is already maximally compacted:
    re-running compaction (and the summary LLM call it triggers) reclaims nothing and just
    thrashes ("compacting too often") when the irreducible retained context — system prompt +
    ground-truth + summary + kept tail — already exceeds the compaction threshold."""
    total = 0
    for idx in range(1, max(1, len(messages) - keep_tail)):
        m = messages[idx]
        if m.get("role") != "user" or not isinstance(m.get("content"), list):
            continue
        for block in m["content"]:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            txt = block.get("content")
            if isinstance(txt, str) and len(txt) > _EVICT_MIN_CHARS and not txt.startswith("[evicted "):
                total += len(txt)
    return total


_SUMMARY_MARKER = "── CONTEXT SUMMARY (your progress so far, kept across compaction) ──"
_STATE_MARKER = "── GROUND TRUTH (harness-recorded, deterministic — trust over memory) ──"


def _clean_summary(summary: str) -> str:
    """Defensively clean the model's summary before folding it into the brief: strip code
    fences, and neutralize an echoed section marker — a summary that contains the marker
    itself would truncate every FUTURE summary refresh (the split() keeps only what precedes
    the first marker occurrence)."""
    s = summary.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    for marker in (_SUMMARY_MARKER, _STATE_MARKER):
        s = s.replace(marker, marker.replace("──", "--"))
    return s.strip()


async def _pin_progress_summary(messages: list[dict], system, model, agent_name: str, assistant_content) -> bool:
    """Claude-Code-style: BEFORE evicting the bulk, ask the model for a tight PROGRESS SUMMARY (goal,
    files + key facts/signatures, edits made, current plan, what's left) and FOLD it into the brief
    (messages[0]). So even if a detail gets evicted the agent stays directed — and can re-read for the
    rest. Folding into msg[0] keeps it permanently in context without breaking role alternation; the
    prior summary is replaced each time. Fail-open: a summary failure never breaks the loop."""
    instr = (
        "Older bulky tool output is about to be trimmed to save context. Write a tight PROGRESS "
        "SUMMARY so you can keep working WITHOUT the full history above — concrete and complete "
        "enough not to need it, under these numbered headings:\n"
        "1. GOAL — the task's intent, plus every binding constraint/correction given to you "
        "(quote short ones verbatim).\n"
        "2. FILES READ — every file you read and the KEY facts / EXACT signatures found in each.\n"
        "3. EDITS MADE — the edits you have ALREADY completed.\n"
        "4. PLAN & REMAINING — your current plan and precisely what is LEFT to do.\n"
        "5. DEAD-ENDS & DECISIONS — approaches ruled out and why, decisions taken.\n"
        "6. NEXT STEP — what you were doing at this exact moment, with a short VERBATIM quote "
        "of your most recent work-in-progress so you resume without drift.\n"
        "If a prior CONTEXT SUMMARY appears above, CARRY its still-relevant facts forward — do "
        "not drop them — but treat it as your own earlier NOTES, not ground truth: it may be "
        "incomplete, and anything load-bearing should be re-verifiable with tools. Include ONLY "
        "information present in this conversation; NEVER invent a file, signature, or result — "
        "omit anything you are unsure of (you can re-read the file or re-run the tool later). "
        "Output ONLY the summary."
    )
    import copy
    # Summarize over a pre-compacted COPY: the live `messages` are at (or past) the window when this
    # runs, so sending them verbatim makes the summary call itself overflow — the safety net then
    # fails exactly when it's needed (observed: every overflow-path summary 400'd and evicted blind).
    # Compacting a copy keeps all assistant reasoning + a generous recent tool tail for the summary,
    # while the caller still evicts from the REAL messages afterwards.
    convo = copy.deepcopy(messages)
    _compact_messages(convo, keep_tail=max(8, settings.agentic_compact_keep_recent_turns))
    if assistant_content:
        # Only the TEXT of the in-flight turn: raw tool_use blocks here have no tool_result
        # following them, which Anthropic rejects (the other way this call always failed).
        text_blocks = [b for b in assistant_content
                       if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
        if text_blocks:
            convo.append({"role": "assistant", "content": text_blocks})
    def _fold_instr(c: list[dict]) -> None:
        # Fold the instruction into the trailing user turn (tool_results end most loop states)
        # instead of appending a second consecutive user message (the API 400s on role repeats).
        if c and c[-1].get("role") == "user":
            tail = c[-1].get("content")
            if isinstance(tail, list):
                c[-1] = {"role": "user", "content": tail + [{"type": "text", "text": instr}]}
            else:
                c[-1] = {"role": "user", "content": f"{tail}\n\n{instr}"}
        else:
            c.append({"role": "user", "content": instr})

    _fold_instr(convo)
    try:
        turn = await call_claude_tools(system=system, messages=convo, tools=[], model=model,
                                       max_tokens=settings.agentic_compact_summary_max_tokens,
                                       agent_name=(agent_name or "agent") + ":compact")
        summary = (getattr(turn, "text", "") or "").strip()
    except Exception as e:  # noqa: BLE001 — best-effort; eviction proceeds regardless
        if not _is_context_overflow(e):
            logger.warning("compaction summary failed (%s) — evicting without it", e)
            return False
        # Input ladder (P1): even the pre-compacted copy overflowed (a huge recent tail).
        # Step down to the harshest compaction (keep_tail=2) and try once more — an
        # incompactable session must degrade to a shorter summary, never to NO summary.
        logger.warning("compaction summary input overflowed — retrying with keep_tail=2")
        try:
            convo2 = copy.deepcopy(messages)
            _compact_messages(convo2, keep_tail=2)
            _fold_instr(convo2)     # same trailing-user merge — a bare append built
                                    # two consecutive user turns here → guaranteed 400
            turn = await call_claude_tools(system=system, messages=convo2, tools=[], model=model,
                                           max_tokens=settings.agentic_compact_summary_max_tokens,
                                           agent_name=(agent_name or "agent") + ":compact")
            summary = (getattr(turn, "text", "") or "").strip()
        except Exception as e2:  # noqa: BLE001
            logger.warning("compaction summary failed at the Lossy step too (%s) — evicting without it", e2)
            return False
    summary = _clean_summary(summary)
    if not summary:
        return False
    base = messages[0].get("content")
    if not isinstance(base, str):               # the brief is always a plain string; bail if not
        return False
    # Cut at the EARLIEST prior marker — summary OR ground-truth — so the fresh summary is always
    # re-inserted at the top (before any state block). Splitting on _SUMMARY_MARKER alone assumed
    # state always sits after summary; but if an earlier compaction's summary FAILED (fail-open),
    # _pin_ground_truth wrote a STATE block with no summary before it, and appending the next summary
    # after that block would then get dropped by _pin_ground_truth's `split(_STATE_MARKER)[0]` —
    # permanently losing every later summary. Stripping both markers keeps the ordering stable.
    _cut = len(base)
    for _m in (_SUMMARY_MARKER, _STATE_MARKER):
        _i = base.find(_m)
        if _i != -1:
            _cut = min(_cut, _i)
    base = base[:_cut].rstrip()
    messages[0] = {"role": messages[0].get("role", "user"),
                   "content": f"{base}\n\n{_SUMMARY_MARKER}\n{summary}"}
    return True


_NO_SUMMARY_NOTE = ("⚠ NOTE: earlier tool output was evicted WITHOUT a progress summary (the "
                    "summary call failed). Your memory of work before that point may be "
                    "incomplete — re-read files / re-run tools instead of trusting recall.")


def _note_unsummarized_eviction(messages: list[dict]) -> None:
    """Pin a LOUD one-time notice into the brief when eviction proceeded without a summary
    (the fail-open path): silently evicting unsummarized history leaves the model working
    confidently from holes it cannot see. Best-effort; never breaks the loop."""
    try:
        base = messages[0].get("content")
        if isinstance(base, str) and _NO_SUMMARY_NOTE not in base:
            messages[0] = {"role": messages[0].get("role", "user"),
                           "content": f"{base}\n\n{_NO_SUMMARY_NOTE}"}
    except Exception:  # noqa: BLE001 — advisory only
        pass


_STATE_MAX_FILES = 60


def _pin_ground_truth(messages: list[dict], ctx) -> None:
    """After compaction, re-inject the harness's DETERMINISTIC record of this run's state into
    the brief (messages[0]) — the edited-file list from the accumulated FileOps and the submitted
    plan. The LLM progress summary above can drop or misremember an edit; this block cannot (it
    is rendered from ctx, the same ground truth every later gate reads). Replaced wholesale on
    every compaction; fail-open — never breaks the loop."""
    try:
        base = messages[0].get("content")
        if not isinstance(base, str):
            return
        lines: list[str] = []
        ops = list(ctx.file_ops.values())
        if ops:
            lines.append(f"Files changed this run ({len(ops)}) — these edits EXIST on disk; "
                         "never redo or re-plan them:")
            for op in ops[:_STATE_MAX_FILES]:
                lines.append(f"  {op.op:<7} [{op.repo_id}] {op.path}")
            if len(ops) > _STATE_MAX_FILES:
                lines.append(f"  … +{len(ops) - _STATE_MAX_FILES} more")
        if ctx.plan:
            summary = str(ctx.plan.get("summary") or "").strip()
            files = ctx.plan.get("files") or []
            if summary:
                lines.append(f"Submitted plan: {summary[:1200]}"
                             + (" …[plan summary clipped]" if len(summary) > 1200 else ""))
            if files:
                # The plan's file list is the work contract — a 400-char summary alone let a
                # compacted agent forget WHICH files remained. Paths only; bounded + marked.
                names = [str((f or {}).get("path") or f)[:160] if isinstance(f, (dict, str)) else "?"
                         for f in files]
                lines.append(f"Planned files ({len(names)}): " + ", ".join(names[:40])
                             + (f" … +{len(names) - 40} more" if len(names) > 40 else ""))
        facts_block = T.format_facts(getattr(ctx, "facts", None) or [])
        if facts_block:
            lines.append(facts_block)
        if not lines:
            return
        base = base.split(_STATE_MARKER)[0].rstrip()     # replace any prior state block
        messages[0] = {"role": messages[0].get("role", "user"),
                       "content": f"{base}\n\n{_STATE_MARKER}\n" + "\n".join(lines)}
    except Exception as e:  # noqa: BLE001 — deterministic re-injection is best-effort
        logger.warning("ground-truth pin failed (%s) — continuing without it", e)


def _repair_dangling_tool_calls(messages: list[dict]) -> int:
    """Crash/seam repair for a REPLAYED transcript: if it ends with an assistant turn whose
    tool_use blocks have no matching tool_result, append synthetic stub results — Anthropic
    rejects the whole request otherwise (every tool_use id needs a result). The designed
    decision-gate seam is already repaired upstream (_analysis_resume_messages answers the
    gate call); this catches any other producer. Returns the number of stubs appended."""
    if not messages:
        return 0
    last = messages[-1]
    if last.get("role") != "assistant" or not isinstance(last.get("content"), list):
        return 0
    ids = [b.get("id") for b in last["content"]
           if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")]
    if not ids:
        return 0
    messages.append({"role": "user", "content": [
        tool_result_block(tid, "(this tool call was interrupted before it ran — re-issue it "
                               "if you still need the result)", is_error=True)
        for tid in ids
    ]})
    return len(ids)


def _seed_read_files(ctx, messages: list[dict]) -> None:
    """Credit the files a REPLAYED transcript already read — but ONLY those whose replayed
    content still matches the file on disk. A replayed session's model holds the file
    contents in its own history, but the fresh RunContext starts with an empty read-set —
    so read-before-edit and the propose/plan evidence gates would bounce it into re-reading
    files it can literally see above. Resolves each read_file's repo the SAME way the tool
    did at read time (T._resolve_repo_id), because the model routinely OMITS repo_id.

    INTEGRITY: the old rule credited by tool name + path alone, so a prior transcript
    could authorize edits and "verified" evidence against code that had since CHANGED,
    and a large-file SKELETON result was indistinguishable from a full read. Now the
    replayed RESULT text is checked against the current clone:
      * skeleton / large-file-head / evicted-stub results never seed (not full reads);
      * a full read seeds read_files + full_reads (+ the stale-edit hash) only when the
        replayed body still matches the file's CURRENT content;
      * a ranged read seeds read_files + that range only when the replayed body still
        matches the same slice of the CURRENT content.
    A mismatch leaves the file unseeded — the gates then bounce the model into a fresh
    read, which is exactly right when the tree changed under the transcript."""
    errored: set = set()
    results: dict = {}
    for m in messages:
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    if b.get("is_error"):
                        errored.add(b.get("tool_use_id"))
                    elif isinstance(b.get("content"), str):
                        results[b.get("tool_use_id")] = b["content"]
    for m in messages:
        if m.get("role") != "assistant" or not isinstance(m.get("content"), list):
            continue
        for b in m["content"]:
            if not (isinstance(b, dict) and b.get("type") == "tool_use"
                    and b.get("name") == "read_file" and b.get("id") not in errored):
                continue
            inp = b.get("input") or {}
            path = inp.get("path")
            replayed = results.get(b.get("id"))
            if (not path or not replayed
                    or replayed.startswith("[evicted ") or replayed.startswith(f"[{path} is large")):
                continue
            try:
                rid = T._resolve_repo_id(ctx, inp.get("repo_id"), path)
                target = T._resolve(ctx, rid, path)
                if not target.is_file():
                    continue
                text = target.read_text(encoding="utf-8", errors="replace")
            except Exception:      # ambiguous / not found on this clone → let the model re-read it
                continue
            start, end = inp.get("start_line"), inp.get("end_line")
            if start or end:
                try:
                    lines = text.splitlines()
                    lo = max((int(start) if start else 1) - 1, 0)
                    hi = int(end) if end else len(lines)
                except (TypeError, ValueError):
                    continue
                expect = "\n".join(lines[lo:hi])
                if expect and replayed.endswith(expect):
                    ctx.read_files.add((rid, path))
                    ctx.read_ranges.setdefault((rid, path), []).append((lo + 1, min(hi, len(lines))))
                    ctx.read_hashes[(rid, path)] = T._sha256(text)
            elif text and replayed.endswith(text):
                ctx.read_files.add((rid, path))
                ctx.full_reads.add((rid, path))
                ctx.read_hashes[(rid, path)] = T._sha256(text)


def _describe(name: str, ti: dict | None) -> str:
    """A short, human-readable action line for the code-gen activity log (screen +
    file). Keeps the JSONL coding log readable as an operation narrative."""
    ti = ti or {}
    path = ti.get("path") or ""
    labels = {
        "read_file": f"👁 Read {path}",
        "edit_file": f"✏️ Edit {path}",
        "create_file": f"➕ Create {path}",
        "delete_file": f"🗑 Delete {path}",
        "grep": f"🔎 grep '{ti.get('pattern', '')}'",
        "glob": f"🔎 glob '{ti.get('pattern', '')}'",
        "symbol_graph": f"🔗 Symbol '{ti.get('symbol', '')}'",
        "ast_query": f"🌳 AST {path}",
        "find_existing_xsd": f"📐 XSD search '{ti.get('query', '')}'",
        "module_context": f"📦 Module {ti.get('module') or '(list all)'}",
        "verify_change": "🧪 Self-verify (compile + tests)",
        "submit_plan": "📝 Plan submitted",
        "propose_approach": "🧭 Proposed reuse-vs-new options for your decision",
        "propose_revision": "⚠ Your request is disruptive — proposed safer alternatives",
        "flag_concern": (f"⚠ Declined disruptive change: {(ti.get('declined_change') or '')[:60]}"
                         if ti.get("declined_change")
                         else f"ℹ Objection recorded (applying the request): {(ti.get('message') or '')[:60]}"),
    }
    if name in labels:
        return labels[name]
    if name == "read_doc":
        if ti.get("heading"):
            return f"📄 Doc §{ti['heading']}"
        if ti.get("query"):
            return f"📄 Doc search '{ti['query']}'"
        return "📄 Doc outline"
    if name == "run_command":
        av = ti.get("argv") or []
        cmd = av if isinstance(av, str) else " ".join(str(a) for a in av)
        return "▶ " + cmd[:80]
    return name


def _detail(name: str, ti: dict | None, result: str) -> str:
    """The human-meaningful CONTENT of a tool call for the transparency log: the
    actual edit (old→new), the created file's content, or the submitted plan — not
    just '(edited X)'. Falls back to the tool result text."""
    ti = ti or {}
    if name == "edit_file":
        return (f"--- before\n{ti.get('old_string', '')}\n"
                f"+++ after\n{ti.get('new_string', '')}")
    if name == "create_file":
        return ti.get("content") or result
    if name == "submit_plan":
        bits = [ti.get("summary", "")]
        if ti.get("files"):
            bits.append("Files:\n" + "\n".join(f"  - {f}" for f in ti["files"]))
        if ti.get("reuse_decisions"):
            bits.append("Reuse decisions:\n" + "\n".join(f"  - {d}" for d in ti["reuse_decisions"]))
        return "\n\n".join(b for b in bits if b) or result
    if name in ("propose_approach", "propose_revision"):
        bits = [str(ti.get("summary", ""))]
        _opts = ti.get("options")
        for o in (_opts if isinstance(_opts, list) else []):
            if not isinstance(o, dict):
                # Schema drift: the model can pass options as plain strings (seen on the
                # 'convert the codebase to Rust' run, 2026-07-03) — render, never raise.
                bits.append(f"• {str(o)[:300]}")
                continue
            rec = " (recommended)" if o.get("id") and o.get("id") == ti.get("recommended") else ""
            tag = o.get("approach") or "safer"
            bits.append(f"• [{tag}] {o.get('title', o.get('id', ''))}{rec}"
                        + (f" → {o.get('target_api')}" if o.get("target_api") else "")
                        + f"\n  {o.get('how_it_fits', '')}"
                        + (f"\n  tradeoffs: {o.get('tradeoffs')}" if o.get('tradeoffs') else ""))
        return "\n".join(b for b in bits if b) or result
    if name == "flag_concern":
        return (f"[{ti.get('severity', 'warning')}] {ti.get('message', '')}"
                + (f"\n  declined: {ti.get('declined_change')}" if ti.get("declined_change") else ""))
    return result


async def run_agent_loop(
    *,
    run_id: str,
    selected_repo_ids: list[str],
    system,
    user_prompt: str,
    tools: list[dict] | None = None,
    model: str | None = None,
    agent_name: str = "code_change",
    max_iterations: int | None = None,
    max_tokens: int | None = None,
    require_plan: bool = True,
    db=None,
    doc_sections: dict | None = None,
    thinking_budget: int | None = None,
    cancel_check: Callable[[], bool] | None = None,
    heartbeat: Callable[[], None] | None = None,
    workspace_run_id: str | None = None,
    schema_only: bool = False,
    code_phase: bool = False,
    initial_messages: list[dict] | None = None,
    initial_facts: list | None = None,
    completion_check: Callable[[], str | None] | None = None,
    progress: Callable[[list], None] | None = None,
) -> RuntimeResult:
    """Drive one subagent to completion (or a cap). Side effects (edits) land in
    the workspace; the returned ChangeSet is the set of FileOps performed.
    ``doc_sections`` ({"brd": {...}, "tsd": {...}}) backs the pull-based read_doc tool.

    ``initial_messages`` seeds the conversation with a prior drive's transcript (already
    ending in a user turn) so a re-drive CONTINUES instead of restarting from ``user_prompt``
    — used by the analysis phase to avoid re-exploring the codebase after a clarification/
    plan-revision. When None (the default), the loop starts fresh from ``user_prompt``."""
    import time
    tools = tools if tools is not None else T.TOOL_SCHEMAS
    cap = max_iterations or settings.agentic_max_iterations
    out_tokens = max_tokens or settings.agentic_max_output_tokens
    ctx = T.RunContext(run_id=run_id, selected_repo_ids=list(selected_repo_ids), db=db,
                       doc_sections=doc_sections or {}, workspace_run_id=workspace_run_id,
                       schema_only=schema_only, code_phase=code_phase)
    if initial_facts:
        # Cross-drive memory: a re-drive builds a FRESH RunContext, so the prior drive's
        # fact sheet (persisted on the run's handoff) is seeded back and pinned below —
        # without this the facts exist in the DB but not in the model's context, and the
        # new drive's returned sheet would silently drop them.
        ctx.facts = [dict(f) for f in initial_facts if isinstance(f, dict) and f.get("fact")]
        ctx.facts_rev = len(ctx.facts)
    logger.info("agent_loop[%s] START run=%s repos=%s cap=%d out_tokens=%d model=%s tools=%d prompt_chars=%d",
                agent_name, run_id, selected_repo_ids, cap, out_tokens, model or "(default)",
                len(tools), len(user_prompt or ""))
    # Open a transcript pass folder for THIS loop invocation. `iteration` restarts at 1 on
    # every resume from a human gate, so without this the passes interleave. The folder is
    # named for the trigger the caller recorded (pass02_after_clarifications, …).
    try:
        from app.core import transcripts as _tr
        _pass_label = _tr.begin_pass(run_id, agent_name)
        if _pass_label:
            logger.info("agent_loop[%s] transcript pass -> %s", agent_name, _pass_label)
    except Exception:  # noqa: BLE001 — transcript bookkeeping must never break the loop
        pass
    messages: list[dict] = ([dict(m) for m in initial_messages]
                            if initial_messages else [{"role": "user", "content": user_prompt}])
    if initial_messages:
        _seed_read_files(ctx, messages)
        stubs = _repair_dangling_tool_calls(messages)
        if stubs:
            logger.warning("agent_loop[%s] run=%s replayed transcript ended in %d unanswered "
                           "tool_use(s) — appended stub results so the request is wire-valid",
                           agent_name, run_id, stubs)
    if ctx.facts:
        # A seeded sheet is visible from turn 1 (and lands in this pass's transcript dump
        # via messages[0]) — not only after the first NEW fact write.
        _pin_ground_truth(messages, ctx)
    # Deny every remote git write for the whole model-driven loop: a tool-issued
    # `git push` must not reach origin before human approval. The guard is otherwise
    # fail-open when no policy is set, so this is the security boundary for the loop.
    # Reset in `finally` so we never leak the policy to a sibling task.
    from app.agents import git_guard
    _guard_tok = git_guard.set_policy(git_guard.deny_remote_policy())

    def _emit(kind: str, payload: dict) -> None:
        if db is None:
            return
        emit_event(db, run_id, kind, payload)
        # Commit each event as it happens so the WS subscriber streams it LIVE (the
        # drive loop otherwise commits only at phase boundaries, which made a whole
        # phase's tool calls flood in at once). Events are append-only telemetry; the
        # agent loop performs no other DB writes to commit here. CONTRACT: the
        # orchestrator MUST NOT have uncommitted phase-state on this session when it
        # enters the tool loop — a mid-loop commit here would persist that partial
        # state. Guarded so a non-Session handle (e.g. a test sentinel) is a no-op.
        commit = getattr(db, "commit", None)
        if callable(commit):
            try:
                commit()
            except Exception:  # noqa: BLE001 — telemetry commit must never break the loop
                rollback = getattr(db, "rollback", None)
                if callable(rollback):
                    rollback()

    def _sync_fact_sheet(iteration: int) -> None:
        """Fact-sheet observability, ONE chokepoint: whenever the sheet changed this turn,
        (1) re-pin it into the brief — messages[0] under the GROUND TRUTH marker, the copy
        every transcript dump and replayed drive carries — and (2) log a `fact_sheet` run
        event so the activity feed / events API / devlog show each update as it happens.
        Rev-gated: a re-pin rewrites the brief and costs a prompt-cache re-read, so only a
        changed sheet pays it (the decay this fixes: a fact read at iter028 was contradicted
        at iter066 with everything still in context)."""
        nonlocal facts_rev_pinned
        if getattr(ctx, "facts_rev", 0) == facts_rev_pinned:
            return
        _pin_ground_truth(messages, ctx)
        facts_rev_pinned = ctx.facts_rev
        _emit("fact_sheet", {"iteration": iteration, "count": len(ctx.facts),
                             "sheet": _head(T.format_facts(ctx.facts)),
                             "action": f"📌 fact sheet updated — {len(ctx.facts)} fact(s) on record"})

    stopped = "max_iterations"
    final_text = ""
    cumulative_tokens = 0                 # tracked for telemetry only — NOT a fail condition
    # Anchored input estimate (P1): the provider's reported input total for the LAST call +
    # chars appended since, ÷4. Drives the preflight check below so a burst of huge tool
    # results compacts BEFORE the next call instead of 400-ing it.
    last_known_in = 0
    chars_at_anchor = 0
    at_floor_notified = False             # emitted the one-time "context at floor" note this stretch
    nudges_used = 0                       # in-loop convergence nudges spent (bounded)
    iters_since_edit = 0                  # harness-truth exploration counter (code phase)
    prev_ops_n = 0
    facts_rev_pinned = getattr(ctx, "facts_rev", 0)   # fact-sheet revision last pinned into the brief
    # Fixed per-call prompt overhead the provider tokenizes but the message array doesn't
    # carry: system segments + tool schemas. Feeds the UNanchored estimate below (a
    # usage-stripping provider never anchors, so without this the preflight undercounts).
    try:
        _base_chars = len(str(system)) + len(str(tools))
    except Exception:  # noqa: BLE001 — estimate only; a stringify failure must not end the run
        _base_chars = 0
    _pol_window = _policy_window(model)   # config policy clamped to the REAL model window
    i = 0
    loop_t0 = time.monotonic()
    while i < cap:
        if cancel_check and cancel_check():
            _emit("loop_cancelled", {"iteration": i})
            logger.info("agent_loop[%s] run=%s CANCELLED at iteration=%d", agent_name, run_id, i)
            git_guard.reset_policy(_guard_tok)
            return RuntimeResult("", ctx.changeset(), ctx.plan, i, "cancelled", messages)
        # Heartbeat the run's lease EACH iteration — a long phase (esp. with
        # auto-continue) otherwise outlives the lease TTL and gets wrongly reclaimed
        # by the recovery beat, double-driving the run (§3).
        if heartbeat is not None:
            try:
                heartbeat()
            except Exception:  # noqa: BLE001 — a heartbeat hiccup must not break the loop
                pass
        # Durably checkpoint the cumulative read-set each iteration so a crash / sleep-suspend
        # mid-loop can RESUME with memory instead of re-exploring from turn 1 (the read-only
        # review's counterpart to the code phase's per-round code_resume). Cumulative through the
        # PREVIOUS iteration's tool executions — the last iteration's reads are re-captured on the
        # caller's round-complete persist. Cheap and fail-open; a hiccup must not break the loop.
        if progress is not None:
            try:
                progress(sorted(ctx.read_files))
            except Exception:  # noqa: BLE001 — a checkpoint hiccup must not break the loop
                pass

        # Preflight overflow check (P1): the post-turn trigger below sees only what the LAST
        # call reported — a burst of huge tool results appended since then can push the NEXT
        # request over the window and 400 it. Estimate the pending input (usage anchor + new
        # chars) and compact BEFORE calling instead of relying on the overflow-catch retry.
        # Preflight compaction is ALWAYS ON (core safety, not a toggle): estimate the pending
        # input and compact before the call if it is over the policy window.
        _est = _estimated_input_tokens(messages, last_known_in, chars_at_anchor, _base_chars)
        if _est > int(_pol_window * settings.agentic_compact_at_fraction):
            # THRASH GUARD: only compact when there's actually reclaimable bulk. Once every
            # old tool output is a stub, the retained floor (system + ground-truth + summary
            # + kept tail) can still sit above the threshold — re-summarizing + re-compacting
            # every iteration then reclaims 0 and burns a summary LLM call per turn (the
            # "compacting too often" report). The call proceeds regardless; the model's real
            # limit is far above the policy window (raise AGENTIC_CONTEXT_WINDOW_TOKENS for
            # more working headroom on this run).
            if _reclaimable_tool_chars(messages, settings.agentic_compact_keep_recent_turns) < _EVICT_MIN_CHARS:
                if not at_floor_notified:
                    at_floor_notified = True
                    logger.warning("agent_loop[%s] run=%s at context FLOOR: est %d > policy %d "
                                   "with nothing left to evict — not re-compacting each turn",
                                   agent_name, run_id, _est,
                                   int(_pol_window * settings.agentic_compact_at_fraction))
                    _emit("context_at_floor",
                          {"iteration": i + 1, "est_tokens": _est,
                           "window": _pol_window,
                           "action": "ℹ Context is at its floor — the retained state (system + "
                                     "summary + recent turns) exceeds the compaction threshold, so "
                                     "there is nothing left to evict. Not compacting further. Raise "
                                     "AGENTIC_CONTEXT_WINDOW_TOKENS if this run needs more room."})
            else:
                logger.warning("agent_loop[%s] run=%s preflight: est input %d tokens over policy — "
                               "compacting before the call", agent_name, run_id, _est)
                # Progress summary before eviction is ALWAYS ON (keeps the agent oriented after
                # the bulk is evicted). Fail-open inside _pin_progress_summary.
                _summarized = await _pin_progress_summary(messages, system, model, agent_name, None)
                _reclaimed = _compact_messages(messages, settings.agentic_compact_keep_recent_turns)
                if not _summarized:
                    _note_unsummarized_eviction(messages)
                _pin_ground_truth(messages, ctx)
                # Re-anchor: the estimate that fired no longer describes the compacted array.
                last_known_in, chars_at_anchor = 0, 0
                at_floor_notified = False        # we reclaimed real bulk — re-arm the floor note
                _emit("history_compacted", {"iteration": i + 1, "trigger": "preflight",
                                            "est_tokens": _est, "summarized": _summarized,
                                            "reclaimed_chars": _reclaimed})
        logger.info("agent_loop[%s] run=%s iter=%d/%d → calling LLM (msgs=%d, cum_tokens=%d)",
                    agent_name, run_id, i + 1, cap, len(messages), cumulative_tokens)
        # The UI shows a "🧠 Awaiting model response — Xs" indicator while this event is the
        # NEWEST event on the stream — so a slow (or stalled) LLM call no longer looks
        # identical to a hung agent (the user's grep-setScheduler confusion). The indicator
        # auto-hides the moment the next tool_call / llm_turn lands.
        _emit("llm_call_started", {"iteration": i + 1, "agent": agent_name,
                                   "msgs": len(messages), "cum_tokens": cumulative_tokens,
                                   "started_at": time.time()})
        turn_t0 = time.monotonic()
        try:
            turn = await call_claude_tools(
                system=system, messages=messages, tools=tools,
                model=model, max_tokens=out_tokens, agent_name=agent_name,
                thinking_budget=thinking_budget,
            )
        except Exception as e:                          # noqa: BLE001 — overflow → compact + retry once
            if not _is_context_overflow(e):
                raise
            # Hard overflow BEFORE proactive compaction caught it (window mis-set, or one turn
            # leapt past the limit). Compact aggressively and retry ONCE — never stop the flow.
            logger.warning("agent_loop[%s] run=%s context overflow at iter=%d — compacting + retrying: %s",
                           agent_name, run_id, i + 1, e)
            _summarized = await _pin_progress_summary(messages, system, model, agent_name, None)
            _reclaimed = _compact_messages(messages, keep_tail=2)
            _pin_ground_truth(messages, ctx)
            if not _summarized:
                _note_unsummarized_eviction(messages)
            if _reclaimed:
                # Re-anchor: the pre-compaction usage anchor now overestimates the shrunken
                # array, which would fire a redundant preflight compaction (and summary
                # call) next turn. Unchanged array (nothing evictable) → anchor still honest.
                last_known_in, chars_at_anchor = 0, 0
            _emit("history_compacted", {"iteration": i + 1, "trigger": "overflow_retry",
                                        "reclaimed_chars": _reclaimed})
            turn = await call_claude_tools(
                system=system, messages=messages, tools=tools,
                model=model, max_tokens=out_tokens, agent_name=agent_name,
                thinking_budget=thinking_budget,
            )
        # Turn-level recovery (grok-build parity): a max_tokens-truncated turn is DISCARDED and
        # retried once with double the budget — the cut-off turn otherwise enters history as if
        # complete (a partial tool_use input becomes a phantom tool error; partial prose becomes
        # lost reasoning). An empty turn (no usable assistant blocks — degenerate/stalled output)
        # gets one fresh retry before the loop reads it as "model done". Both are single retries
        # inside the SAME iteration; the retried turn replaces the discarded one wholesale.
        # (AiNxt-openai strips stop_reason → the truncation branch simply never fires there.)
        if settings.agentic_turn_recovery:
            _retry_tokens = min(out_tokens * 2, _MODEL_MAX_OUTPUT_TOKENS)
            if turn.stop_reason == "max_tokens" and _retry_tokens > out_tokens:
                logger.warning("agent_loop[%s] run=%s turn TRUNCATED at max_tokens=%d — discarding "
                               "and retrying once at %d", agent_name, run_id, out_tokens, _retry_tokens)
                _emit("turn_truncated", {"iteration": i + 1, "max_tokens": out_tokens,
                                         "retry_max_tokens": _retry_tokens})
                turn = await call_claude_tools(
                    system=system, messages=messages, tools=tools,
                    model=model, max_tokens=_retry_tokens, agent_name=agent_name,
                    thinking_budget=thinking_budget,
                )
            elif turn.stop_reason == "max_tokens":
                # Already at the model's output ceiling — a same-budget retry would just
                # truncate again; keep the turn (its partial tool_use errors out and recovers).
                logger.warning("agent_loop[%s] run=%s turn truncated at the model output cap (%d) "
                               "— no retry headroom, keeping the turn", agent_name, run_id, out_tokens)
            if not turn.assistant_content:
                logger.warning("agent_loop[%s] run=%s EMPTY turn (stop=%s) — retrying once before "
                               "treating it as done", agent_name, run_id, turn.stop_reason)
                _emit("empty_turn_retry", {"iteration": i + 1, "stop_reason": turn.stop_reason})
                turn = await call_claude_tools(
                    system=system, messages=_perturbed_for_retry(messages, (
                        "[harness] Your previous response was empty and was discarded. "
                        "Respond now with your next tool call or your final answer.")),
                    tools=tools,
                    model=model, max_tokens=out_tokens, agent_name=agent_name,
                    thinking_budget=thinking_budget,
                )
            # Doom-loop-lite (P2): degenerate tail repetition — the model stuck emitting the
            # same chunk. DISCARD and resample once with a perturbation note (a fresh sample
            # alone is no remedy at forced temperature=0 — see _perturbed_for_retry); accept
            # the retry as-is either way so this can never spin.
            elif (not turn.tool_uses
                  and (_looks_degenerate(turn.text) or _looks_degenerate(turn.thinking))):
                # tool_uses guard: a turn whose THINKING rambled but that still emitted a
                # valid tool call is doing work — discarding it would lose the call.
                logger.warning("agent_loop[%s] run=%s DEGENERATE turn (tail repetition) — "
                               "discarding and resampling once", agent_name, run_id)
                _emit("degenerate_turn", {"iteration": i + 1, "text_tail": _head(turn.text[-300:])})
                turn = await call_claude_tools(
                    system=system, messages=_perturbed_for_retry(messages, (
                        "[harness] Your previous response was discarded because it degenerated "
                        "into repetition. Do not repeat earlier output — take the next concrete "
                        "action (a tool call or your final answer) now.")),
                    tools=tools,
                    model=model, max_tokens=out_tokens, agent_name=agent_name,
                    thinking_budget=thinking_budget,
                )
        i += 1
        turn_ms = int((time.monotonic() - turn_t0) * 1000)
        # Token usage — telemetry only (no spend cap); context is managed by window compaction below.
        _usage = getattr(turn, "usage", None) or {}
        _in = _usage.get("input_tokens") or 0
        _out = _usage.get("output_tokens") or 0
        cumulative_tokens += _in + _out
        # Re-anchor the input estimate on real usage (input + cached prefix = what the model
        # actually received). `messages` is still exactly the array that was sent.
        # Char floor: AiNxt reports only the UNCACHED TAIL as input_tokens and hardcodes the
        # cache counters to 0 (docs/ainxt_messages_compat.md D10) — trusting that poisoned this
        # anchor to ~2K while the real prompt was 100s of K, so the window triggers never fired
        # and transcripts ballooned until the model stopped converging. chars/4 is the harness's
        # own lower bound on what was actually sent; on providers with honest usage the reported
        # total wins the max() and nothing changes.
        _char_floor = _total_msg_chars(messages) // 4
        _anchor_total = max(_in + (_usage.get("cache_read_tokens") or 0)
                            + (_usage.get("cache_write_tokens") or 0), _char_floor)
        if _anchor_total > 0:
            last_known_in = _anchor_total
            chars_at_anchor = _total_msg_chars(messages)
        # Per-call token/cost is persisted to the `llm_usage_records` ledger at the observability
        # chokepoint (core.observability._persist_usage_row), tagged with this run via the usage
        # context the orchestrator set — so the Usage dashboard rolls it up per-change/phase/section.
        logger.info("agent_loop[%s] run=%s iter=%d ← stop=%s tools=[%s] text_chars=%d in_tok=%d out_tok=%d "
                    "cum_tok=%d turn_ms=%d",
                    agent_name, run_id, i, turn.stop_reason,
                    ",".join(t.name for t in turn.tool_uses) or "-",
                    len(turn.text or ""), _in, _out, cumulative_tokens, turn_ms)
        # DEBUG full-fidelity dump: `messages` here is still the EXACT array sent to the
        # model this turn (assistant not yet appended; compaction below hasn't run). No-op
        # unless AGENTIC_DUMP_TRANSCRIPTS is on.
        _dump_transcript(run_id, agent_name, i, system, messages, tools, turn)

        # A6 (architecture review High #5) — per-run token budget guard, re-enforced.
        # `agentic_max_tokens_per_run` (the OLD cap) was deliberately disabled because
        # it killed legitimate large changes on a per-LOOP-INVOCATION counter that reset
        # every phase. This checks the DURABLE cross-phase ledger total instead (every
        # drive, every phase, every fix round of THIS run_id), so a genuinely
        # non-converging run is caught even if no single phase looks expensive.
        # Soft-stop: the run ends with stopped="budget_exceeded" (resumable — an
        # operator can raise agentic_token_budget_hard_cap and re-drive) rather than
        # raising mid-turn, which would risk leaving on-disk edits in a half-written
        # state. Checked every iteration (cheap: an indexed SUM query) rather than only
        # at phase boundaries, so a single runaway phase cannot blow through the whole
        # budget before the guard has a chance to fire.
        _budget_cap = int(getattr(settings, "agentic_token_budget_hard_cap", 0) or 0)
        if _budget_cap > 0:
            from app.core.observability import get_cumulative_run_tokens
            _run_total = get_cumulative_run_tokens(run_id)
            _warn_frac = float(getattr(settings, "agentic_token_budget_warn_fraction", 0.8))
            if _run_total >= _budget_cap and getattr(settings, "agentic_token_budget_enforce", True):
                logger.error("agent_loop[%s] run=%s TOKEN BUDGET EXCEEDED: %d >= cap %d — "
                             "stopping (resumable with a raised budget)",
                             agent_name, run_id, _run_total, _budget_cap)
                _emit("token_budget_exceeded",
                      {"iteration": i, "cumulative_run_tokens": _run_total,
                       "hard_cap": _budget_cap,
                       "action": f"⛔ Run token budget exhausted ({_run_total:,}/{_budget_cap:,}) — "
                                 "stopping to avoid unbounded spend. Raise "
                                 "AGENTIC_TOKEN_BUDGET_HARD_CAP and resume if this run needs more."})
                git_guard.reset_policy(_guard_tok)
                return RuntimeResult(final_text, ctx.changeset(), ctx.plan, i,
                                     "budget_exceeded", messages,
                                     read_files=sorted(ctx.read_files), facts=ctx.facts,
                                     intel_queried=sorted(ctx.intel_queried),
                                     schema_amendments=list(
                                         (getattr(ctx, "schema_amendments", None) or {}).values()))
            elif _run_total >= _budget_cap * _warn_frac and not getattr(ctx, "_budget_warned", False):
                ctx._budget_warned = True
                logger.warning("agent_loop[%s] run=%s token budget at %.0f%% (%d/%d)",
                               agent_name, run_id, 100.0 * _run_total / _budget_cap,
                               _run_total, _budget_cap)
                _emit("token_budget_warning",
                      {"iteration": i, "cumulative_run_tokens": _run_total, "hard_cap": _budget_cap,
                       "action": f"⚠ Run token spend at {100.0 * _run_total / _budget_cap:.0f}% of budget "
                                 f"({_run_total:,}/{_budget_cap:,})"})
        # Context-window management (Claude-Code style) — we NEVER fail on token spend. When the
        # input the model just received approaches its context WINDOW (real token usage), or — when
        # a provider strips usage — when accumulated tool output passes the char fallback, COMPACT
        # the conversation (evict the bulky, reconstructable tool outputs; keep ALL reasoning + the
        # recent tail) and KEEP GOING. A large change FINISHES instead of dying on a spend cap; the
        # run stays bounded by the iteration + continuation caps, not a token guillotine.
        # Post-turn compaction is ALWAYS ON (core safety, not a toggle).
        _window = _pol_window                 # config policy clamped to the REAL model window
        # What the model RECEIVED this turn is input + cached prefix: with prompt caching on,
        # `input_tokens` is only the uncached tail (observed: in=2 while cache_read=370K), so
        # gating on it alone means the window trigger NEVER fires and transcripts balloon past
        # the policy window (439K on a 200K policy — every turn re-reading it as cache).
        # AiNxt recreates that exact blindness by zeroing the cache counters — the char
        # floor computed at the anchor above keeps this trigger honest there.
        _in_total = max(_in + (_usage.get("cache_read_tokens") or 0)
                        + (_usage.get("cache_write_tokens") or 0), _char_floor)
        _over_window = _in_total > int(_window * settings.agentic_compact_at_fraction)
        _over_chars = _history_tool_chars(messages) > settings.agentic_compact_history_threshold_chars
        # THRASH GUARD (mirrors the preflight path): skip the summary+evict cycle when the
        # history is already at its floor — nothing evictable left, so it would reclaim 0 and
        # burn a summary LLM call every turn. The preflight check above already emitted the
        # one-time context_at_floor note, so stay silent here.
        _reclaimable = _reclaimable_tool_chars(messages, settings.agentic_compact_keep_recent_turns)
        if (_over_window or _over_chars) and _reclaimable >= _EVICT_MIN_CHARS:
            # 1) Capture a progress summary (ALWAYS ON — keeps the agent directed; lets it recover
            #    details). Fail-open inside _pin_progress_summary.
            _summarized = await _pin_progress_summary(messages, system, model, agent_name,
                                                      turn.assistant_content)
            # 2) Evict the bulky, reconstructable tool outputs.
            _reclaimed = _compact_messages(messages, settings.agentic_compact_keep_recent_turns)
            # 3) Re-inject the DETERMINISTIC run state (edited files, plan) — the summary
            #    above is an LLM's recollection; this block is the harness's record.
            _pin_ground_truth(messages, ctx)
            if not _summarized:
                _note_unsummarized_eviction(messages)
            at_floor_notified = False        # reclaimed real bulk — re-arm the floor note
            if _reclaimed:
                # Re-anchor (same as the preflight path): the usage anchor captured
                # above describes the PRE-compaction array — left stale it overestimates
                # next turn's preflight and burns a redundant summary call. Only when
                # eviction actually shrank the array; an unchanged array keeps the
                # provider-reported anchor (chars/4 alone would underestimate it).
                last_known_in, chars_at_anchor = 0, 0
            if _reclaimed or _summarized:
                _emit("history_compacted",
                      {"iteration": i, "input_tokens": _in_total, "window": _window,
                       "trigger": "window" if _over_window else "chars", "summarized": _summarized,
                       "reclaimed_chars": _reclaimed, "after_chars": _history_tool_chars(messages)})

        if turn.text:                       # carry the latest prose so cap/cancel exits aren't blank
            final_text = turn.text
        if turn.thinking and settings.expose_llm_reasoning:
            _emit("reasoning", {"iteration": i, "action": "💭 " + _head(turn.thinking)})
        _emit("llm_turn", {"iteration": i, "stop_reason": turn.stop_reason,
                           "text": _head(turn.text), "tools": [t.name for t in turn.tool_uses],
                           # Lever 4 telemetry: turn latency + token usage so prod can see where
                           # wall-clock goes and whether the prompt cache is actually hitting
                           # (cache_read > 0 means the conversation prefix was reused).
                           "turn_ms": turn_ms, "in_tokens": _in, "out_tokens": _out,
                           "cache_read_tokens": _usage.get("cache_read_tokens"),
                           "cache_write_tokens": _usage.get("cache_write_tokens"),
                           # How much of the input is accumulated tool output (re-sent uncached
                           # every turn). Growing each turn = context rot; the signal that says
                           # whether compaction is needed before we flip it on by default.
                           "history_tool_chars": _history_tool_chars(messages)})

        # A turn with no usable blocks would make the next request's assistant
        # message empty (Anthropic 400). After the one recovery retry above, treat
        # it as the model being done — but emit the diagnostic so an abnormal end
        # (degenerate output masquerading as completion) is visible in the event
        # feed, not silently indistinguishable from a clean finish.
        if not turn.assistant_content:
            _emit("empty_response", {"iteration": i, "stop_reason": turn.stop_reason,
                                     "usage": getattr(turn, "usage", None) or {}})
            stopped = "completed"
            break
        messages.append({"role": "assistant", "content": turn.assistant_content})

        if not turn.wants_tools:
            # In-loop convergence nudge (P1, TodoGate analog): before accepting "done", ask the
            # orchestrator's deterministic completion check (unsatisfied acceptance predicates).
            # Unmet deliverables come back as ONE bounded corrective user turn and the loop
            # continues — closing the gap here instead of burning a full continuation round.
            # Bounded (agentic_convergence_nudges) and fail-open: a check error never blocks a stop.
            if completion_check is not None and nudges_used < settings.agentic_convergence_nudges:
                try:
                    unmet = completion_check()
                except Exception as e:  # noqa: BLE001 — the check must never break the loop
                    logger.warning("completion_check failed (%s) — accepting the stop", e)
                    unmet = None
                if unmet:
                    nudges_used += 1
                    logger.info("agent_loop[%s] run=%s convergence nudge %d/%d — unmet deliverables",
                                agent_name, run_id, nudges_used, settings.agentic_convergence_nudges)
                    _emit("convergence_nudge", {"iteration": i, "nudge": nudges_used,
                                                "detail": _head(unmet)})
                    messages.append({"role": "user", "content": (
                        "You declared done, but the deterministic completeness check still reports "
                        "UNMET deliverables (checked against your actual diff, not your prose):\n"
                        f"{unmet}\n"
                        "Address each item now with the appropriate tool calls, or — if an item is "
                        "genuinely already satisfied at a different location or under a different "
                        "name — state exactly where (file + symbol) and finish. Do NOT insert a "
                        "token just to satisfy a check when your implementation is already correct.")})
                    continue
            stopped = "completed"
            break

        results: list[dict] = []
        for tu in turn.tool_uses:
            tool_t0 = time.monotonic()
            # Turn-1 plan enforcement: no mutation before a plan exists (§8).
            if require_plan and tu.name in _MUTATING and ctx.plan is None:
                text, is_error = "call submit_plan before editing files", True
            # Tool-policy gate (§8): no blind .java edit — require blast-radius intel first.
            elif (gate := T.intel_gate_reason(ctx, tu.name, tu.input)):
                text, is_error = gate, True
            else:
                text, is_error = T.execute_tool(ctx, tu.name, tu.input)
            tool_ms = int((time.monotonic() - tool_t0) * 1000)
            try:
                _action = _describe(tu.name, tu.input)
                _detail_text = _detail(tu.name, tu.input, text)
            except Exception:  # noqa: BLE001 — display formatting must never fail the run
                _action, _detail_text = tu.name, text
            logger.info("agent_loop[%s] run=%s iter=%d tool=%s ok=%s ms=%d result_chars=%d :: %s",
                        agent_name, run_id, i, tu.name, not is_error, tool_ms, len(text or ""),
                        _head(_action))
            _emit("tool_call", {"iteration": i, "name": tu.name, "input": _head(str(tu.input)),
                                "action": _action,
                                "detail": _head(_detail_text),
                                # Lever 4: this is the tool's OWN execution time — isolates a
                                # genuinely slow read (e.g. network FS) from the LLM round-trip
                                # that follows it (which is the usual cause of "reads feel slow").
                                "is_error": is_error, "result": _head(text), "ms": tool_ms})
            try:
                from app._devlog import capture as _dc
                _dc.record_tool_io(run_id=run_id, agent=agent_name, iteration=i,
                                   name=tu.name, tool_input=tu.input, output=text,
                                   is_error=is_error, elapsed_ms=tool_ms)
            except Exception:
                pass
            results.append(tool_result_block(tu.id, text, is_error=is_error))

        # Decision gate: propose_approach asked to stop for the human's reuse-vs-new
        # choice — end this pass cleanly (no edits happened; the orchestrator gates here).
        if ctx.awaiting_decision:
            # Facts written by the gate turn's own tools (e.g. the occupancy auto-facts
            # from ask_clarifications) must still land in the persisted transcript and
            # the event feed even though this pass ends before the results are appended.
            _sync_fact_sheet(i)
            stopped = "awaiting_decision"
            break

        # stop_reason said tool_use but no tool blocks executed → don't send an
        # empty user turn (Anthropic 400); the model has nothing more to do.
        if not results:
            stopped = "completed"
            break
        # Harness-truth exploration nudge (P2, laziness-detector analog): counters the model
        # cannot fabricate. In the CODE phase, a long run of iterations without one edit is the
        # documented convergence failure ("reads the whole codebase without editing") — remind
        # it with the harness's own record. Read-only phases (analysis/review) never nudge.
        _every = settings.agentic_exploration_nudge_every
        if code_phase and _every > 0:
            if len(ctx.file_ops) > prev_ops_n:
                iters_since_edit = 0
            else:
                iters_since_edit += 1
            prev_ops_n = len(ctx.file_ops)
            if iters_since_edit >= _every:
                iters_since_edit = 0                     # re-arm; fires again after N more
                _emit("exploration_nudge", {"iteration": i, "edits": len(ctx.file_ops)})
                results.append({"type": "text", "text": (
                    f"[harness record — deterministic] {i} iterations this pass, "
                    f"{len(ctx.file_ops)} file edit(s) total, none in the last {_every} "
                    "iterations. If you have gathered enough evidence, START IMPLEMENTING now — "
                    "exploration that doesn't lead to an edit is wasted budget. If something "
                    "genuinely blocks you, state it explicitly (or call ask_decision).")})
        messages.append({"role": "user", "content": results})
        _sync_fact_sheet(i)
    else:
        _emit("loop_capped", {"iterations": i})
        logger.warning("agent_loop[%s] run=%s HIT ITERATION CAP (%d) — stopping", agent_name, run_id, cap)

    _emit("loop_done", {"iterations": i, "stopped": stopped, "ops": len(ctx.file_ops)})
    logger.info("agent_loop[%s] DONE run=%s stopped=%s iters=%d ops=%d read_files=%d cum_tok=%d total_ms=%d",
                agent_name, run_id, stopped, i, len(ctx.file_ops), len(ctx.read_files),
                cumulative_tokens, int((time.monotonic() - loop_t0) * 1000))
    git_guard.reset_policy(_guard_tok)
    return RuntimeResult(final_text, ctx.changeset(), ctx.plan, i, stopped, messages,
                         read_files=sorted(ctx.read_files),
                         proposal=ctx.proposal, concerns=list(ctx.concerns),
                         facts=list(getattr(ctx, "facts", None) or []),
                         intel_queried=sorted(ctx.intel_queried),
                         schema_amendments=list(
                             (getattr(ctx, "schema_amendments", None) or {}).values()))
