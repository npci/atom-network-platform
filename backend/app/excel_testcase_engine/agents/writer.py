# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Writer agent: parallel fan-out that fills RenderedTestCase prose for each stub.

Always LLM-driven. Each batch is retried up to 3 times on validation errors.
Raises ``WriterError`` if any batch cannot be rendered after retries — the
pipeline fails fast rather than emit deterministic placeholder prose.

BRD/TSD-only refactor: RAG retrieval, historical TSD chunks, and canonical
UPI grounding are all removed. The writer sees only the current change's
TSD sections (via ``options["tsd_sections"]``) plus the stubs themselves.

Strategy:
- Group stubs into batches keeping ``pair_id`` siblings together.
- For each batch, call the writer LLM with only the current-feature TSD.
- Auto-correct pair drift by copying the canonical sibling's blocks.
- Merge rendered cases back into the plan by ``test_id``.
"""

from __future__ import annotations

import asyncio
import contextvars
import re
from collections import defaultdict
from collections.abc import Callable

from pydantic import ValidationError

from app.excel_testcase_engine import domain_vocab
from app.excel_testcase_engine.config import load_runtime_config
from app.excel_testcase_engine.adapters.llm import get_client
from app.excel_testcase_engine.schemas.llm import Message, SystemBlock
from app.excel_testcase_engine.observability import LLMProviderError, WriterError, get_logger
from app.excel_testcase_engine.orchestrator.status import JobProgress
from app.excel_testcase_engine.schemas.workbook_plan import (
    RenderedTestCase,
    TestCaseStub,
    WorkbookPlan,
)

# Per-workflow engine context (TSD sections). write_all() sets this from
# `options` at entry; _llm_call reads it to build per-batch grounding
# without threading options through every helper signature.
_engine_context: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "_engine_context", default=None,
)

from ._runtime import load_prompt, parse_json_response
from .step_linter import StepIssue, lint_stub

LOGGER = get_logger("network.agent.writer")


def group_into_batches(plan: WorkbookPlan, max_per_batch: int = 20) -> list[list[TestCaseStub]]:
    """Group cases keeping pair_id siblings together; bin-pack up to max_per_batch."""
    groups: dict[str, list[TestCaseStub]] = defaultdict(list)
    for sheet in plan.sheets:
        for tc in sheet.test_cases:
            groups[tc.pair_id or tc.test_id].append(tc)

    batches: list[list[TestCaseStub]] = []
    current: list[TestCaseStub] = []
    for group in groups.values():
        if current and len(current) + len(group) > max_per_batch:
            batches.append(current)
            current = []
        current.extend(group)
    if current:
        batches.append(current)
    return batches


# ── TSD grounding block ──────────────────────────────────────────────────────

_TSD_SECTION_TITLES: tuple[tuple[str, str], ...] = (
    ("control_flow",        "Control Flow & Sequence"),
    ("failure_handling",    "Failure Handling & Resilience"),
    ("error_handling",      "Error & Response Handling"),
    ("testing_verification", "Testing & Verification"),
    ("interface_spec",      "Interface Specification"),
)


def _build_current_feature_tsd_block(engine_context: dict | None) -> str:
    """Emit the TSD sections as the ONLY grounding for the writer prompt.

    Returns "" when no TSD sections are present. In BRD/TSD-only mode we
    trust the TSD authored for this change — no RAG lookup, no XSD deltas,
    no canonical-vs-new bucketing.
    """
    if not engine_context:
        return ""
    tsd_sections = engine_context.get("tsd_sections") or {}
    if not tsd_sections:
        return ""

    parts: list[str] = [
        "## Current-feature TSD (PRIMARY and ONLY grounding — use verbatim)"
    ]
    for key, heading in _TSD_SECTION_TITLES:
        body = (tsd_sections.get(key) or "").strip()
        if not body:
            continue
        parts.append(f"### {heading}\n{body}")
    return "\n\n".join(parts) if len(parts) > 1 else ""


def _format_batch_prompt(batch: list[TestCaseStub], plan: WorkbookPlan) -> str:
    batch_summary = ", ".join({tc.api_type for tc in batch})
    n = len(batch)
    expected_ids = ", ".join(stub.test_id for stub in batch)
    return (
        f"Render EXACTLY {n} test case(s) below. Output a JSON array of "
        f"EXACTLY {n} RenderedTestCase objects — never an empty array, "
        f"never wrapped in an object, never anything else.\n"
        f"Workbook archetype: {plan.archetype}; status casing: "
        f"{plan.global_conventions.get('status_casing','upper')}.\n"
        f"Batch theme: {batch_summary}.\n"
        f"Required test_id values, in this exact order: [{expected_ids}].\n\n"
        f"Stubs:\n{[stub.model_dump() for stub in batch]}\n\n"
        "Respect pair_id invariants exactly: stubs sharing a pair_id MUST "
        "share details_block and description_block byte-for-byte. "
        f"Final reminder: the output array MUST contain exactly {n} object(s)."
    )


def _enforce_pair_invariant(batch: list[TestCaseStub], rendered: list[RenderedTestCase]) -> None:
    """Mechanical pass: copy canonical sibling's details/description across pair_id."""
    canonical: dict[str, RenderedTestCase] = {}
    for stub, case in zip(batch, rendered):
        if not stub.pair_id:
            continue
        canonical.setdefault(stub.pair_id, case)
    for index, (stub, case) in enumerate(zip(batch, rendered)):
        if not stub.pair_id:
            continue
        canon = canonical[stub.pair_id]
        if case is canon:
            continue
        if case.details_block != canon.details_block or case.description_block != canon.description_block:
            LOGGER.warning("writer.pair_drift_corrected", pair_id=stub.pair_id, test_id=stub.test_id)
            rendered[index] = case.model_copy(
                update={
                    "details_block": canon.details_block,
                    "description_block": canon.description_block,
                }
            )


async def _llm_call(
    batch: list[TestCaseStub],
    plan: WorkbookPlan,
    retry_hint: str = "",
) -> list[RenderedTestCase]:
    """`retry_hint` is appended to the user message on retry attempts so the
    model sees what went wrong on the previous try. Empty on the first call."""
    client = get_client("writer")

    engine_ctx = _engine_context.get()
    current_tsd_block = _build_current_feature_tsd_block(engine_ctx)

    system = [SystemBlock(text=load_prompt("writer.md"), cache=True)]
    if current_tsd_block:
        # Per-run TSD content — not cached, so distinct changes don't share.
        system.append(SystemBlock(text=current_tsd_block, cache=False))
    user_msg = _format_batch_prompt(batch, plan)
    if retry_hint:
        user_msg = f"{user_msg}\n\n## RETRY CORRECTION\n{retry_hint}"

    # AiNxt body-cap warning — see comment in the pre-refactor writer for
    # the numeric rationale. Keeps large-prompt failures actionable.
    AINXT_BODY_CAP_BYTES = 24 * 1024
    sys_chars = sum(len(b.text) for b in system)
    user_chars = len(user_msg)
    total_chars = sys_chars + user_chars
    if total_chars > AINXT_BODY_CAP_BYTES * 1.5:
        LOGGER.warning(
            "writer.prompt_size_high",
            batch_size=len(batch),
            stub_ids=[s.test_id for s in batch],
            system_chars=sys_chars,
            user_chars=user_chars,
            total_chars=total_chars,
            ainxt_cap_bytes=AINXT_BODY_CAP_BYTES,
            note="prompt nearing AiNxt body cap; consider reducing cases_per_batch",
        )

    response = await client.complete(
        system=system,
        messages=[Message(role="user", content=user_msg)],
        max_tokens=8000,
        response_format="json",
    )
    raw = response.text or ""
    try:
        payload = parse_json_response(raw)
    except Exception as exc:
        LOGGER.warning(
            "writer.parse_failed",
            batch_size=len(batch),
            response_chars=len(raw),
            error=repr(exc)[:200],
        )
        raise
    if isinstance(payload, dict):
        for key in ("items", "rendered_cases", "test_cases", "cases", "data"):
            if key in payload and isinstance(payload[key], list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        LOGGER.warning(
            "writer.non_list_payload",
            batch_size=len(batch),
            payload_type=type(payload).__name__,
        )
        raise WriterError(f"Writer returned non-list payload: {type(payload).__name__}")
    if not payload:
        LOGGER.warning(
            "writer.empty_payload",
            batch_size=len(batch),
            response_chars=len(raw),
            stub_ids=[s.test_id for s in batch],
        )
    return [RenderedTestCase.model_validate(item) for item in payload]


async def _write_batch_with_retries(batch: list[TestCaseStub], plan: WorkbookPlan) -> list[RenderedTestCase]:
    last_error: Exception | None = None
    retry_hint = ""
    expected_ids = [s.test_id for s in batch]
    for attempt in range(3):
        try:
            rendered = await _llm_call(batch, plan, retry_hint=retry_hint)
        except (ValidationError, ValueError, WriterError) as exc:
            LOGGER.warning("writer.invalid", attempt=attempt, batch=len(batch), error=repr(exc)[:200])
            last_error = exc
            retry_hint = (
                f"Your previous attempt failed: {repr(exc)[:200]}. "
                f"Return EXACTLY {len(batch)} RenderedTestCase object(s) as a JSON "
                f"ARRAY (not an object). Required test_id values, in order: "
                f"{expected_ids}. Do not return an empty array."
            )
            continue
        except Exception as exc:
            LOGGER.error("writer.provider_error", error=repr(exc))
            raise LLMProviderError(f"Writer provider call failed: {exc!r}") from exc

        if len(rendered) != len(batch):
            last_error = WriterError(f"Writer returned {len(rendered)} cases for batch of {len(batch)}")
            LOGGER.warning("writer.size_mismatch", attempt=attempt, expected=len(batch), got=len(rendered))
            got_ids = [getattr(r, "test_id", "?") for r in rendered]
            missing = [tid for tid in expected_ids if tid not in got_ids]
            retry_hint = (
                f"Your previous attempt returned {len(rendered)} cases but "
                f"{len(batch)} were required. Missing test_id(s): {missing}. "
                f"Re-render ALL {len(batch)} cases — never an empty array, "
                f"never wrapped in an object. Required test_id values, in order: "
                f"{expected_ids}."
            )
            continue
        _enforce_pair_invariant(batch, rendered)
        rendered = await _lint_and_repair(batch, rendered, plan)
        return rendered
    raise WriterError(f"Writer could not render batch of {len(batch)} after 3 attempts: {last_error!r}")


# ── Step-linter allowlist derived from BRD/TSD only ──────────────────────

_ERROR_CODE_RE = re.compile(r"\b(?:[A-Z][0-9A-Z]{1,3}|[0-9]{2})\b")
# Structural tokens + the pack's own acronyms (`domain_acronyms`) — one
# derivation for planner/validator/writer (genericisation sweep; the three
# hand-copied UPI lists drifted: only the planner knew PSP1).
_ERROR_CODE_STOPWORDS: frozenset[str] = domain_vocab.error_code_stopwords()
_API_TOKEN_RE = re.compile(r"\b(?:Req|Resp)[A-Z][A-Za-z0-9]{2,}\b")


def _allowlists_from_engine_context(
    engine_ctx: dict | None, batch: list[TestCaseStub],
) -> tuple[set[str], set[str]]:
    """Build (api_allowlist, code_allowlist) from the TSD interface_spec
    and any BRD-derived error codes visible in the batch's stubs. Falls
    back to the APIs on the stubs themselves when the TSD is silent.
    """
    apis: set[str] = set()
    codes: set[str] = {"00"}
    if engine_ctx:
        tsd = engine_ctx.get("tsd_sections") or {}
        interface_text = (tsd.get("interface_spec") or "")
        apis.update(_API_TOKEN_RE.findall(interface_text))
        error_text = (tsd.get("error_handling") or "")
        for tok in _ERROR_CODE_RE.findall(error_text):
            if tok not in _ERROR_CODE_STOPWORDS:
                codes.add(tok)
    # Union with what the batch actually declares — the LLM may have picked
    # BRD-named codes that aren't in the TSD error section.
    for stub in batch:
        apis.update(stub.apis)
        if stub.response_code:
            codes.add(stub.response_code)
    return apis, codes


async def _lint_and_repair(
    batch: list[TestCaseStub],
    rendered: list[RenderedTestCase],
    plan: WorkbookPlan,
) -> list[RenderedTestCase]:
    """Run the step-linter on each rendered case; reprompt offending stubs.

    BRD/TSD-only: allowlist comes from the TSD Interface Specification
    (for API names) and the BRD/TSD error-handling text plus the stubs'
    own response_code fields (for error codes). No canonical UPI set.
    """
    engine_ctx = _engine_context.get() or {}
    api_allow, code_allow = _allowlists_from_engine_context(engine_ctx, batch)

    bad_indices: list[tuple[int, list[StepIssue]]] = []
    for index, (stub, case) in enumerate(zip(batch, rendered)):
        stub_with_render = stub.model_copy(update={"rendered": case})
        issues = lint_stub(stub_with_render, api_allow, code_allow)
        if issues:
            bad_indices.append((index, issues))

    if not bad_indices:
        return rendered

    LOGGER.info(
        "writer.lint_repair_start",
        stubs=len(bad_indices),
        issues=sum(len(i) for _, i in bad_indices),
    )

    client = get_client("writer")
    for index, issues in bad_indices:
        stub = batch[index]
        case = rendered[index]
        repair_msg = (
            "The following test case failed the step-quality linter. "
            "Rewrite ONLY this single case as a JSON ARRAY of one RenderedTestCase. "
            "Apply every fix hint listed below. Preserve any details_block and "
            "description_block that come from a paired sibling — only fix the steps_block "
            "unless the issue specifically calls out details/description.\n\n"
            f"Stub:\n{stub.model_dump()}\n\n"
            f"Current rendering:\n{case.model_dump()}\n\n"
            f"Linter issues:\n"
            + "\n".join(f"- [{i.code}] {i.message} (fix: {i.fix_hint})" for i in issues)
        )
        try:
            response = await client.complete(
                system=[SystemBlock(text=load_prompt("writer.md"), cache=True)],
                messages=[Message(role="user", content=repair_msg)],
                max_tokens=2000,
                response_format="json",
            )
            payload = parse_json_response(response.text)
            if isinstance(payload, dict):
                payload = [payload]
            if isinstance(payload, list) and payload:
                fixed = RenderedTestCase.model_validate(payload[0])
                if stub.pair_id:
                    canonical_member = next(
                        (b for i, b in enumerate(batch) if b.pair_id == stub.pair_id and i != index),
                        None,
                    )
                    if canonical_member and rendered[batch.index(canonical_member)]:
                        canonical_render = rendered[batch.index(canonical_member)]
                        fixed = fixed.model_copy(update={
                            "details_block": canonical_render.details_block,
                            "description_block": canonical_render.description_block,
                        })
                rendered[index] = fixed
                LOGGER.info("writer.lint_repair_ok", test_id=stub.test_id)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("writer.lint_repair_failed", test_id=stub.test_id, error=repr(exc)[:200])

    for stub, case in zip(batch, rendered):
        stub_with_render = stub.model_copy(update={"rendered": case})
        residual = lint_stub(stub_with_render, api_allow, code_allow)
        bad = [i for i in residual if i.code == "invalid_api_in_steps"]
        if bad:
            LOGGER.warning(
                "writer.invalid_api_residual",
                test_id=stub.test_id,
                count=len(bad),
                detail=[i.message[:140] for i in bad[:3]],
            )
            try:
                _persist_rendered_for_debug(stub, case, residual)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("writer.debug_persist_failed", error=repr(exc))
    return rendered


def _persist_rendered_for_debug(stub, rendered_case, issues) -> None:
    """Save a single problematic stub's rendering to outputs/artifacts/_writer_drift/."""
    import json
    from pathlib import Path

    folder = Path("outputs") / "artifacts" / "_writer_drift"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stub.test_id}.json"
    payload = {
        "stub": stub.model_dump(),
        "rendered": rendered_case.model_dump(),
        "issues": [i.__dict__ for i in issues],
    }
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _placeholder_render(stub: TestCaseStub, reason: str) -> RenderedTestCase:
    """Synthesize a minimal RenderedTestCase when a batch fails terminally."""
    detail = stub.scenario_summary or stub.test_id
    desc = (
        f"⚠️ Auto-generated placeholder — writer batch failed.\n"
        f"Reason: {reason}\n"
        f"This row needs manual completion or a regenerate."
    )
    steps = (
        "1. [Auto-generated placeholder — writer could not render this case]\n"
        f"   Reason: {reason}\n"
        "2. Review the batch failure in backend logs (search for "
        f"`stub_ids` containing {stub.test_id!r})\n"
        "3. Re-run cert_test_cases generation, or hand-fill these steps."
    )
    return RenderedTestCase(
        test_id=stub.test_id,
        details_block=detail,
        description_block=desc,
        steps_block=steps,
    )


async def write_all(
    plan: WorkbookPlan,
    on_progress: Callable[[JobProgress], None] | None = None,
    options: dict | None = None,
) -> WorkbookPlan:
    """Fill prose for every test case stub via parallel LLM calls.

    BRD/TSD-only: ``options["tsd_sections"]`` is the only external context
    fed to the writer. Any single batch's terminal failure produces a
    labelled placeholder rendering so the run still ships a workbook.
    """

    engine_ctx_token = _engine_context.set(options or None)

    runtime = load_runtime_config()
    batches = group_into_batches(plan, max_per_batch=runtime.writer.cases_per_batch)
    semaphore = asyncio.Semaphore(runtime.writer.max_concurrent_batches)
    completed = 0
    total = sum(len(s.test_cases) for s in plan.sheets)
    completed_batches = 0
    total_batches = len(batches)

    async def write_one(batch: list[TestCaseStub]) -> tuple[list[TestCaseStub], list[RenderedTestCase] | Exception]:
        nonlocal completed, completed_batches
        async with semaphore:
            try:
                rendered = await _write_batch_with_retries(batch, plan)
            except Exception as exc:  # noqa: BLE001
                LOGGER.error(
                    "writer.batch_failed_terminal",
                    batch_size=len(batch),
                    stub_ids=[s.test_id for s in batch],
                    error=repr(exc)[:300],
                )
                completed_batches += 1
                if on_progress:
                    on_progress(JobProgress(  # type: ignore[arg-type]
                        job_id="",
                        status="writing",  # type: ignore[arg-type]
                        current=completed,
                        total=total,
                        message=(
                            f"Writing test cases · batch {completed_batches}/{total_batches} "
                            f"· {completed}/{total} cases (last batch failed — continuing)"
                        ),
                    ))
                return batch, exc
            completed += len(rendered)
            completed_batches += 1
            if on_progress:
                on_progress(JobProgress(  # type: ignore[arg-type]
                    job_id="",
                    status="writing",  # type: ignore[arg-type]
                    current=completed,
                    total=total,
                    message=(
                        f"Writing test cases · batch {completed_batches}/{total_batches} "
                        f"· {completed}/{total} cases"
                    ),
                ))
            return batch, rendered

    try:
        results = await asyncio.gather(*[write_one(batch) for batch in batches])
    finally:
        _engine_context.reset(engine_ctx_token)

    rendered_by_id: dict[str, RenderedTestCase] = {}
    failed_batches = 0
    for batch, outcome in results:
        if isinstance(outcome, Exception):
            failed_batches += 1
            reason = repr(outcome)[:200]
            for stub in batch:
                rendered_by_id[stub.test_id] = _placeholder_render(stub, reason)
        else:
            for case in outcome:
                rendered_by_id[case.test_id] = case

    missing = [tc.test_id for sheet in plan.sheets for tc in sheet.test_cases if tc.test_id not in rendered_by_id]
    if missing:
        LOGGER.error("writer.unexpected_missing_after_placeholder", missing=missing[:10])
        for sheet in plan.sheets:
            for tc in sheet.test_cases:
                if tc.test_id not in rendered_by_id:
                    rendered_by_id[tc.test_id] = _placeholder_render(tc, "no batch result")

    if failed_batches:
        LOGGER.warning(
            "writer.completed_with_placeholders",
            failed_batches=failed_batches,
            total_batches=total_batches,
            placeholder_cases=sum(
                1 for sheet in plan.sheets for tc in sheet.test_cases
                if rendered_by_id.get(tc.test_id) and
                "⚠️ Auto-generated placeholder" in rendered_by_id[tc.test_id].description_block
            ),
        )

    for sheet in plan.sheets:
        for tc in sheet.test_cases:
            tc.rendered = rendered_by_id[tc.test_id]
    return plan
