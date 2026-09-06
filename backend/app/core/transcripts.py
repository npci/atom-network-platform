# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Per-change transcript capture — the human-navigable log tree.

Problem this solves: transcripts used to land in a single flat folder keyed by an
opaque *run_id*, with every stage's files (`analysis_iter001_…json`, …) interleaved,
so finding "what did the BRD loop do for change X" meant grepping a haystack. When
several changes run at once it was effectively unusable.

New layout — ONE folder per change, sub-divided by pipeline stage, in pipeline order:

    <transcript_dir>/<change_id>/
        01_prompt_enhancement/ iter001_<ms>.json …
        02_enrichment/
        03_deep_research/
        04_canvas/
        05_brd/                (BRD generation calls, in order; loopNN/ when a loop label is set)
        06_tech_spec/
        07_planning/           iter001.json iter002.json …   (plan + its iterations)
        08_codegen/<run_id>/<agent>/ iterNNN_<ms>.json        (analysis / code_change / review / …)
        99_other/<agent>/                                     (anything unmapped)

Design:
- ONE chokepoint. `capture_llm_call()` is invoked from call_llm / stream_llm
  (app/core/llm.py) after every LLM call, so all single-shot + streaming stages are
  captured with no per-stage wiring. The agentic loop routes its richer per-iteration
  dump through `codegen_dir()`.
- change_id needs NO signature threading: it is read from the usage contextvar that
  `UsageContextMiddleware` (every /changes/{id} request) and the orchestrator (codegen)
  already set. Falls back to "unassigned" so nothing is ever silently dropped.
- Best-effort. Never raises into the call path — a capture failure is logged and
  swallowed, exactly like the debug dump it replaces.
- Secret-redacted, per-string, reusing the coding-log redactor so a greedy rule can't
  eat a JSON delimiter and corrupt the file.
"""
from __future__ import annotations

import json
import logging
import time as _time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

# The redact→serialize→write of a transcript record is CPU + blocking-I/O work on a payload
# that can be multiple MB (full system prompt + growing message history). The capture hooks
# run in the async call_llm/stream_llm path and the agentic loop, so doing that work inline
# stalls the event loop on every LLM call — which, by spacing cacheable calls apart, expires
# Anthropic's 5-min prompt cache and silently kills cache reuse. Hand it to a tiny daemon pool
# instead — capture is best-effort, so we fire-and-forget and never await completion. Lazily
# spawns threads on first submit, so a capture-off process pays nothing.
_WRITE_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="transcript-write")

# ── Stage registry ───────────────────────────────────────────────────────────
# agent_name (as passed to call_llm/stream_llm) → numbered stage folder. The numeric
# prefix makes the folders sort in pipeline order in any file browser / `ls`.
_STAGE_FOLDERS: dict[str, str] = {
    "prompt_enhancer": "01_prompt_enhancement",
    "prompt_enhancement": "01_prompt_enhancement",
    "enrichment": "02_enrichment",
    "deep_researcher": "03_deep_research",
    "deep_research": "03_deep_research",
    "canvas": "04_canvas",
    "brd": "05_brd",
    "brd_extractor": "05_brd",
    "tech_spec": "06_tech_spec",
    "tech-spec": "06_tech_spec",
    "tsd": "06_tech_spec",
    "code_planner": "07_planning",
    "planning": "07_planning",
    # The five agentic-loop stages nest under 08_codegen/<run_id>/<agent>/ via codegen_dir();
    # this mapping is the fallback if one is ever captured through the generic path.
    "analysis": "08_codegen",
    "approach_proposal": "08_codegen",
    "xsd_discovery": "08_codegen",
    "code_change": "08_codegen",
    "review": "08_codegen",
}
CODEGEN_STAGE = "08_codegen"

# Documents produced by the docgen bridge share agent_name="docgen_bridge" but tag a
# per-document section ("docgen_brd", "docgen_tsd", …) in the usage context. Map by doc.
_DOC_FOLDERS: dict[str, str] = {
    "brd": "05_brd",
    "tsd": "06_tech_spec",
    "tech_spec": "06_tech_spec",
    "circular": "07b_circular",
    "product_note": "07c_product_note",
    "product_kit": "07c_product_note",
}


def enabled() -> bool:
    return bool(getattr(settings, "transcript_capture", True))


# ── Optional finer sub-label (loops / named iterations) ───────────────────────
# A stage that runs an internal loop (e.g. docgen BRD revise passes) can wrap the loop
# body in `transcript_scope(loop=n)` so its calls land in <stage>/loopNN/ instead of
# interleaving at the stage root. Read by capture_llm_call; entirely optional.
_sub_label: ContextVar[str | None] = ContextVar("transcript_sub_label", default=None)


# ── Pass tracking (agentic loops) ────────────────────────────────────────────
# One "pass" = one invocation of the agent loop. A single run's agent can pass several
# times, because the loop SUSPENDS at human gates (ask_clarifications, propose_plan) and
# resumes when a person answers. The conversation carries forward but `iteration` restarts
# at 1, so without separation the passes interleave inside one folder.
#
# Passes are foldered by WHY they started, not by a bare counter:
#   pass01_initial / pass02_after_clarifications / pass03_after_plan_reopened
_trigger: ContextVar[str | None] = ContextVar("transcript_trigger", default=None)
_pass_dir: ContextVar[str | None] = ContextVar("transcript_pass_dir", default=None)


def set_trigger(name: str | None) -> None:
    """Record WHY the next agent-loop pass is starting (e.g. 'after_clarifications').
    Consumed — and cleared — by the next `begin_pass()`."""
    _trigger.set(_safe_segment(name) if name else None)


def begin_pass(run_id: str, agent: str, *, change_id: str | None = None) -> str | None:
    """Open a new pass folder for a fresh agent-loop invocation. Call once per loop start.

    The folder is `pass<NN>_<trigger>`, where NN is the next free number under
    `<change_id>/08_codegen/<run_id>/<agent>/`. Returns the folder name, or None if
    capture is off / the path is unusable (in which case dumps fall back to the agent dir).
    """
    if not enabled():
        return None
    try:
        cid = _safe_segment(change_id) if change_id else current_change_id()
        agent_dir = _base_dir() / cid / CODEGEN_STAGE / _safe_segment(run_id) / _safe_segment(agent)
        agent_dir.mkdir(parents=True, exist_ok=True)
        n = sum(1 for p in agent_dir.iterdir() if p.is_dir() and p.name.startswith("pass")) + 1
        # An unlabelled FIRST pass is the initial one; an unlabelled later pass is a resume
        # we could not attribute (better than silently calling it 'initial').
        trig = _trigger.get() or ("initial" if n == 1 else "resume")
        label = f"pass{n:02d}_{trig}"
        (agent_dir / label).mkdir(parents=True, exist_ok=True)
        _pass_dir.set(label)
        _trigger.set(None)   # consume, so it can't leak into the next agent's pass
        return label
    except Exception as exc:  # noqa: BLE001 — never break the loop
        logger.warning("begin_pass failed (run=%s agent=%s): %s", run_id, agent, exc)
        return None


@contextmanager
def transcript_scope(*, loop: int | None = None, label: str | None = None):
    """Nest subsequent captured LLM calls under <stage>/<sub>/ for the duration.

    `loop=3` → subfolder ``loop03``; `label="revise"` → subfolder ``revise``.
    Best-effort and re-entrant-safe (resets to the prior value on exit)."""
    sub = None
    if loop is not None:
        sub = f"loop{int(loop):02d}"
    elif label:
        sub = _safe_segment(label)
    token = _sub_label.set(sub)
    try:
        yield
    finally:
        try:
            _sub_label.reset(token)
        except Exception:  # noqa: BLE001
            _sub_label.set(None)


# ── Path helpers ─────────────────────────────────────────────────────────────
def _base_dir() -> Path:
    """Root of the transcript tree — same base the legacy dump used, so migrated and
    new data live together: explicit `agentic_transcript_dump_dir`, else the coding-log
    dir's sibling `transcripts/`."""
    base = (getattr(settings, "agentic_transcript_dump_dir", "") or "").strip()
    if not base:
        base = str(Path(getattr(settings, "coding_log_dir", None) or "/tmp/a2a").parent / "transcripts")
    return Path(base)


def _safe_segment(s: str | None) -> str:
    """Filesystem-safe single path segment (no slashes / traversal / control chars)."""
    s = (s or "").strip() or "unknown"
    out = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in s)
    # `.` is allowed for filenames, so a pure-dot segment ("." / "..") would survive intact —
    # a latent `..` traversal primitive. Callers currently pass DB-generated UUIDs (unreachable),
    # but neutralize it defensively so a future less-constrained caller can't escape the base dir.
    if set(out) <= {"."}:
        out = "unknown"
    return out[:80] or "unknown"


def current_change_id() -> str:
    try:
        from app.core.observability import current_usage_context
        ctx = current_usage_context() or {}
        cid = ctx.get("change_request_id")
        if cid:
            return _safe_segment(cid)
    except Exception:  # noqa: BLE001
        pass
    return "unassigned"


def _stage_folder(agent_name: str | None) -> str:
    """Map an LLM call to its numbered stage folder.

    Resolution order, most-specific first:
      1. exact agent_name in the registry;
      2. the docgen per-document section tag (`docgen_brd` → 05_brd) — authoritative for
         EVERY call made during a docgen pipeline, whatever the call names itself. Docgen
         uses helper agents (`brd_tier_classifier`, `docgen_patch_planner`, …) that are not
         and should not be enumerated here, and they belong with the document they built;
      3. a name prefix heuristic (brd* / tsd* / tech*);
      4. `99_other/<agent>` — visible, never dropped.
    """
    name = (agent_name or "").strip().lower()
    if name in _STAGE_FOLDERS:
        return _STAGE_FOLDERS[name]

    try:
        from app.core.observability import current_usage_context
        section = ((current_usage_context() or {}).get("section_default") or "")
        if section.startswith("docgen_"):
            doc = section[len("docgen_"):]
            if doc in _DOC_FOLDERS:
                return _DOC_FOLDERS[doc]
            return f"05_brd" if doc.startswith("brd") else f"99_other/{_safe_segment(doc)}"
    except Exception:  # noqa: BLE001
        pass

    if name.startswith("brd"):
        return "05_brd"
    if name.startswith(("tsd", "tech_spec", "tech-spec")):
        return "06_tech_spec"
    if name in ("docgen_bridge", "docgen", "docx_assembler") or name.startswith("docgen"):
        return "05_brd"   # a docgen call with no section tag — BRD is the default document
    return f"99_other/{_safe_segment(name)}"


def stamp() -> str:
    """Sortable local timestamp `YYYYMMDDTHHMMSS_mmm` used as the FILENAME PREFIX.

    Why prefix-with-time: a stage's iteration counter restarts on every fresh invocation,
    but repeated passes share one folder — so `iterNNN` alone interleaves separate passes
    when sorted. (The original dumper also suffixed `int(time*1000) % 100000`, which wraps
    every 100 s and therefore scrambled rather than ordered same-numbered files.) With the
    timestamp first, plain lexical order — `ls`, any file browser — IS chronological order.
    """
    t = _time.time()
    return _time.strftime("%Y%m%dT%H%M%S", _time.localtime(t)) + f"_{int(t * 1000) % 1000:03d}"


def _next_index(d: Path) -> int:
    """1-based sequence for the next file in `d`. Counts existing transcript files
    (new `<ts>_iterNNN.json` and legacy `iterNNN_*.json`). Ordering does not depend on
    this number — the timestamp prefix does — it is just a readable call counter."""
    try:
        return sum(1 for _ in d.glob("*iter*.json")) + 1
    except Exception:  # noqa: BLE001
        return 1


def offload_write(path: Path, record: dict[str, Any]) -> None:
    """Redact + serialize + write `record` to `path` on a background thread so the JSON encode
    and disk I/O never block the async event loop the LLM call runs on. The path (and thus any
    call-order numbering) is chosen by the caller BEFORE this returns; only the heavy work is
    deferred. Fire-and-forget + fail-open: a transcript dump must never delay, block, or break
    a call."""
    def _job() -> None:
        try:
            from app.agents.agentic_events import _redact_obj
            payload = json.dumps(_redact_obj(record), default=str, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001 — redaction/serialization must not lose the record
            try:
                payload = json.dumps(record, default=str, ensure_ascii=False, indent=2)
            except Exception:
                return
        try:
            path.write_text(payload, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("transcript write failed (%s): %s", path, exc)

    try:
        _WRITE_POOL.submit(_job)
    except Exception:  # noqa: BLE001 — pool shut down (interpreter exit) → do it inline
        _job()


def _write(d: Path, record: dict[str, Any]) -> None:
    """Queue one transcript file for `d`. The filename's sequence index is computed here,
    synchronously, so concurrent calls keep their arrival order; the write itself is offloaded
    (see `offload_write`). Never raises."""
    idx = _next_index(d)
    offload_write(d / f"{stamp()}_iter{idx:03d}.json", record)


# ── Public API ───────────────────────────────────────────────────────────────
def stage_dir(agent_name: str | None, *, change_id: str | None = None) -> Path | None:
    """Resolve (and create) the directory a call for `agent_name` should write to.
    Applies the active `transcript_scope` sub-label. Returns None if capture is off
    or the directory can't be created."""
    if not enabled():
        return None
    try:
        cid = _safe_segment(change_id) if change_id else current_change_id()
        d = _base_dir() / cid / _stage_folder(agent_name)
        sub = _sub_label.get()
        if sub:
            d = d / sub
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError as exc:
        logger.warning("transcript dir not writable (%s) — skipping", exc)
        return None


def codegen_dir(run_id: str, agent: str, *, change_id: str | None = None) -> Path | None:
    """Directory for one agentic-loop call:
    <change_id>/08_codegen/<run_id>/<agent>/<passNN_trigger>/

    The trailing pass folder is present when `begin_pass()` opened one for this loop;
    without it (older callers / capture races) dumps land directly in the agent dir."""
    if not enabled():
        return None
    try:
        cid = _safe_segment(change_id) if change_id else current_change_id()
        d = _base_dir() / cid / CODEGEN_STAGE / _safe_segment(run_id) / _safe_segment(agent)
        p = _pass_dir.get()
        if p:
            d = d / p
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError as exc:
        logger.warning("codegen transcript dir not writable (%s) — skipping", exc)
        return None


def change_dir(change_id: str) -> Path:
    """Root of one change's transcript tree: <base>/<change_id>/.

    Pure path resolver — same read-side contract as `run_transcript_dir`: no `enabled()`
    gate, no mkdir. Callers walk the numbered stage folders under it and must tolerate
    a missing directory."""
    return _base_dir() / _safe_segment(change_id)


def run_transcript_dir(run_id: str, *, change_id: str) -> Path:
    """The on-disk transcript folder for one agentic run: <change_id>/08_codegen/<run_id>/.

    Pure path resolver — no `enabled()` gate and no mkdir — for READING captured dumps
    back (e.g. the per-change transcript export). The directory may not exist if capture
    was off for the run or the tree was cleaned; callers must check `.is_dir()`."""
    return _base_dir() / _safe_segment(change_id) / CODEGEN_STAGE / _safe_segment(run_id)


def capture_llm_call(*, agent_name: str | None, system: Any, messages: list[dict],
                     response_text: str, usage: dict | None = None,
                     streaming: bool = False) -> None:
    """Capture ONE non-agentic LLM call (single-shot or streamed) into its per-change
    per-stage folder. Called from call_llm / stream_llm. Best-effort; never raises."""
    if not enabled():
        return
    try:
        d = stage_dir(agent_name)
        if d is None:
            return
        record = {
            "change_id": current_change_id(),
            "agent": agent_name or "unknown",
            "ts": _time.strftime("%Y-%m-%dT%H:%M:%S"),
            "streaming": streaming,
            "system": system,
            "messages": messages,
            "response": {"text": response_text or "", "usage": usage or {}},
        }
        _write(d, record)
    except Exception as exc:  # noqa: BLE001 — a transcript dump must never break the call
        logger.warning("transcript capture failed (agent=%s): %s", agent_name, exc)
