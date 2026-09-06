# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Planner agent: produce a typed WorkbookPlan with TestCaseStubs.

Always LLM-driven. BRD/TSD-only refactor: the planner used to layer canonical
UPI specs (apis / error codes / scope ownership / coverage matrix / XSD diff)
on top of the brief. Now it trusts the BRD and TSD sections passed in via
``options`` as the sole source of truth. Scenarios come from the TSD's
Testing & Verification section, APIs come from the TSD Interface Specification,
and error codes come from the BRD.
"""

from __future__ import annotations

import re

from pydantic import ValidationError

from app.excel_testcase_engine import domain_vocab
from app.excel_testcase_engine.adapters.llm import get_client
from app.excel_testcase_engine.schemas.llm import Message, SystemBlock
from app.excel_testcase_engine.observability import (
    ConfigurationError,
    LLMProviderError,
    PlanningError,
    get_logger,
)
from app.excel_testcase_engine.schemas.enriched_brief import EnrichedBrief
from app.excel_testcase_engine.schemas.workbook_plan import SheetSpec, TestCaseStub, WorkbookPlan

from ._runtime import load_prompt, parse_json_response, retry_message as _retry_message

LOGGER = get_logger("network.agent.planner")

# Layouts that carry actual test cases — Index/Summary/Subset/Modes/Scope are
# metadata-only and skipped during the per-sheet stubs phase.
_TEST_CASE_LAYOUTS = {"A1", "B1", "C1", "C2", "C3"}

# NO archetype -> role-count minimum lives here any more, deliberately.
#
# The engine used to gate the plan on one: A>=1, B>=2, C>=3 role sheets. That
# coupling was invented, and it was wrong in both directions — it rejected a
# faithful 2-role pack under archetype C, and it pushed the model to invent a
# third role to satisfy an arbitrary number.
#
# `archetype` controls ONLY how much annexure the renderer wraps around the
# pack (see excel_writer/renderer.py): C adds Index/Summary/Subset/Modes, B
# adds a Version Log, A adds nothing, plus status-casing. The render loop
# iterates `plan.sheets` and does not care how many there are, and
# summary_sheet derives its columns from the pack vocabulary rather than from
# `plan.sheets`. So the annexure depth and the role count are independent
# choices, and the role count belongs to the BRD/TSD alone.

# Hard safety cap per pack. TSD-driven runs typically ship 5–30 rows;
# 60 is a runaway guard for degenerate LLM output, not a target.
_MAX_ROWS_PER_PACK = 60


def _role_sheet_count(plan: WorkbookPlan) -> int:
    """Number of test-case-bearing role sheets."""
    return sum(1 for s in plan.sheets if s.layout in _TEST_CASE_LAYOUTS and s.test_cases)


def _placeholder_stubs_for_sheet(
    sheet: SheetSpec, exc: BaseException, brief: EnrichedBrief | None = None,
) -> list[TestCaseStub]:
    """One flagged placeholder when a sheet's stubs phase blows up mid-run.

    Uses APIs from the brief when available so a new-API change gets a
    placeholder naming the new API rather than a silent rewrite to ReqTransfer.
    """
    role = sheet.name or "Unknown"
    err_msg = repr(exc)[:160]
    apis = ((list(brief.apis[:2]) if brief and brief.apis else [])
            or domain_vocab.default_apis() or ["Request", "Response"])
    return [
        TestCaseStub(
            test_id=f"{role[:3].upper()}_PLACEHOLDER_1",
            apis=apis,
            api_type="Pay",
            entities=[role],
            scenario_summary=(
                f"⚠️ Auto-generated placeholder — planner stubs phase failed "
                f"for sheet '{role}'. Regenerate this sheet to obtain real "
                f"test cases. Underlying error: {err_msg}"
            ),
            expected_status="Failure",
            response_code="",
            coverage_tag="happy_path",
        )
    ]


# ── BRD error-code extraction (informational, not enforcement) ────────────

# Matches short uppercase/alphanumeric tokens characteristic of the domain's
# error codes (UPI: U09, T27, GC, ZM, ZH). Extracted from the BRD text so the LLM sees
# a hint of what codes the BRD talks about; the LLM is instructed to use the
# BRD-named codes as-is on each stub.
_ERROR_CODE_RE = re.compile(r"\b(?:[A-Z][0-9A-Z]{1,3}|[0-9]{2})\b")
# Structural tokens + the pack's own acronyms (`domain_acronyms`) — one
# derivation for planner/validator/writer (genericisation sweep; the three
# hand-copied UPI lists drifted: only the planner knew PSP1).
_ERROR_CODE_STOPWORDS: frozenset[str] = domain_vocab.error_code_stopwords()


def _codes_from_brd(brief: EnrichedBrief) -> frozenset[str]:
    """Best-effort extract of error-code-shaped tokens from the BRD text.

    Reads ``brief.original_brief`` (which carries the BRD embedded in the
    engine's brief). Returns an empty set when no codes are named — the
    engine emits stubs with empty ``response_code`` when the BRD is silent.
    """
    text = getattr(brief, "original_brief", "") or ""
    if not text.strip():
        return frozenset()
    candidates = {t for t in _ERROR_CODE_RE.findall(text) if t not in _ERROR_CODE_STOPWORDS}
    return frozenset(candidates)


# ── Phase 1 — Skeleton ─────────────────────────────────────────────────────


def _format_skeleton_user_message(brief: EnrichedBrief) -> str:
    """Ask for the workbook skeleton with empty test_cases lists."""
    return (
        "Plan the SKELETON of a workbook for this enriched brief. Output ONLY "
        "the WorkbookPlan JSON. Each sheet must have its `test_cases` array "
        "EMPTY (`\"test_cases\": []`). Test cases will be planned in a "
        "follow-up phase, one sheet at a time, so keep this response compact.\n\n"
        f"EnrichedBrief: {brief.model_dump_json()}\n\n"
        "Required fields:\n"
        "- filename: a sensible workbook filename ending in .xlsx\n"
        "- archetype: must equal the brief's archetype exactly\n"
        "- sheets: EXACTLY one role sheet per role the brief names — "
        + ", ".join(r for r in (brief.roles or []) if r and r.strip())
        + f" ({len([r for r in (brief.roles or []) if r and r.strip()])} role "
        "sheet(s)) — using those names verbatim. The brief's role list is the "
        "contract: do not drop a role, and do not add one it does not name. "
        "The archetype does NOT dictate how many role sheets there are; it "
        "only selects how much annexure the renderer wraps around them. "
        "You may additionally emit a `Scope` sheet (`layout: \"scope\"`) and a "
        "`UAT MOBILE APP` sheet (`layout: \"uat_mobile\"`) for archetype C "
        "when appropriate. "
        "Do NOT include Index, Summary, Subset, Modes of Certification, or "
        "Version Log — the renderer generates those automatically from the "
        "archetype and must not receive them from you. Each role sheet's "
        "`test_cases` MUST be []. Set realistic `name`, `layout` (exactly "
        "one of A1/B1/C1/C2/C3 for role sheets — never null or any other "
        "value), `tab_color` (bare 6-digit aRGB hex, e.g. \"4472C4\" — no "
        "leading \"#\"), and `metadata`.\n"
        "- global_conventions: workbook-wide conventions (status casing, "
        "header row, freeze panes, dropdown values, etc.)\n"
        "- coverage_audit: empty dict — populated post-stubs.\n\n"
        "Use the APIs named in the enriched brief. Output strictly valid JSON."
    )


async def _plan_skeleton(brief: EnrichedBrief) -> WorkbookPlan:
    """Phase 1: produce sheets-only skeleton, no stubs."""
    client = get_client("planner")
    forced_archetype = brief.archetype
    system = [
        SystemBlock(text=load_prompt("planner.md"), cache=True),
        SystemBlock(
            text=(
                "## Skeleton-only mode\n"
                "You are in SKELETON mode for this call. Do NOT populate "
                "`test_cases` on any sheet — leave the array empty. The "
                "stubs phase will fill them in per-sheet next."
            ),
            cache=False,
        ),
    ]
    base_msg = _format_skeleton_user_message(brief)
    user_msg = base_msg

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = await client.complete(
                system=system,
                messages=[Message(role="user", content=user_msg)],
                max_tokens=8000,
                response_format="json",
                extended_thinking=True,
            )
            plan_obj = WorkbookPlan.model_validate(parse_json_response(response.text))
            if plan_obj.archetype != forced_archetype:
                LOGGER.warning(
                    "planner.skeleton.archetype_drift",
                    llm_proposed=plan_obj.archetype,
                    forced=forced_archetype,
                )
                plan_obj.archetype = forced_archetype  # type: ignore[assignment]
            for sh in plan_obj.sheets:
                sh.test_cases = []

            # The brief's roles are the contract, not the archetype. Retry only
            # when the model DROPPED a role the BRD/TSD named — never to pad the
            # count up to some archetype's idea of a minimum.
            brief_roles = [r for r in (brief.roles or []) if r and r.strip()]
            role_sheets = [s for s in plan_obj.sheets if s.layout in _TEST_CASE_LAYOUTS]
            if len(role_sheets) < len(brief_roles):
                # Logged, not silent: this branch used to `continue` without a
                # trace, so a run that burned an attempt here was
                # indistinguishable in the logs from one where the model
                # returned malformed JSON.
                LOGGER.warning(
                    "planner.skeleton.dropped_roles",
                    attempt=attempt,
                    archetype=plan_obj.archetype,
                    role_sheets=len(role_sheets),
                    brief_roles=len(brief_roles),
                    missing=[
                        r for r in brief_roles
                        if r not in {s.name for s in role_sheets}
                    ],
                )
                user_msg = _retry_message(
                    base_msg,
                    f"Your skeleton has {len(role_sheets)} role sheet(s) but the "
                    f"brief names {len(brief_roles)}: "
                    f"{', '.join(brief_roles)}. Emit one role sheet per role, "
                    "using those exact names. Do not drop any, and do not add "
                    "roles the brief does not name.",
                )
                last_error = PlanningError(
                    f"skeleton: dropped roles ({len(role_sheets)} sheets "
                    f"for {len(brief_roles)} roles)"
                )
                continue
            if len(role_sheets) > len(brief_roles):
                # Allowed — a TSD can justify splitting a role across sheets —
                # but recorded, since it is also what an inventing model looks
                # like and the engine is BRD/TSD-only.
                LOGGER.warning(
                    "planner.skeleton.extra_role_sheets",
                    attempt=attempt,
                    role_sheets=len(role_sheets),
                    brief_roles=len(brief_roles),
                    extra=[
                        s.name for s in role_sheets if s.name not in set(brief_roles)
                    ],
                )
            role_layout_count = len(role_sheets)

            LOGGER.info(
                "planner.skeleton.ok",
                attempt=attempt,
                sheets=len(plan_obj.sheets),
                role_sheets=role_layout_count,
            )
            return plan_obj
        except (ValidationError, ValueError) as exc:
            LOGGER.warning("planner.skeleton.invalid_json", attempt=attempt, error=repr(exc)[:200])
            user_msg = _retry_message(
                base_msg,
                f"Your previous response was not a valid skeleton WorkbookPlan: "
                f"{exc}\nReturn ONLY the JSON object matching the WorkbookPlan "
                "schema exactly — top-level key `filename` (not `workbook_title` "
                "or `workbook_id`) and per-sheet key `name` (not `sheet_name` or "
                "`sheet_id`). Every sheet's `test_cases` array must be [].",
            )
            last_error = exc
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("planner.skeleton.provider_error", error=repr(exc))
            raise LLMProviderError(f"Planner (skeleton phase) provider call failed: {exc!r}") from exc

    raise PlanningError(f"Planner skeleton failed after 3 attempts: {last_error!r}")


# ── Phase 2 — Per-sheet stubs ──────────────────────────────────────────────


def _format_stubs_user_message(
    brief: EnrichedBrief,
    skeleton: WorkbookPlan,
    sheet: SheetSpec,
    per_sheet_cap: int | None = None,
    tsd_sections: dict | None = None,
    brd_codes: frozenset[str] | set[str] = frozenset(),
) -> str:
    """Ask for the test_cases list for one sheet only.

    BRD/TSD-only: no scope constraints, no error-code allowlist, no coverage
    floor. The LLM extracts scenarios from the TSD Testing & Verification
    section and picks error codes from the BRD.
    """
    skeleton_summary = {
        "archetype": skeleton.archetype,
        "filename": skeleton.filename,
        "sheets": [{"name": s.name, "layout": s.layout} for s in skeleton.sheets],
    }
    cap_instruction = (
        f"- Safety cap: at most {per_sheet_cap} test cases for this sheet.\n"
        if per_sheet_cap and per_sheet_cap > 0
        else ""
    )
    initiated_by_instruction = (
        f"- Set `txn_initiated_by` per case relative to THIS sheet's role "
        f"({sheet.name!r} is under certification; the "
        f"{domain_vocab.authority_label()} simulator plays "
        "all other parties): `Bank` when this role fires the first message "
        "of the case, `NPCI` when the simulator fires first toward this "
        "role — see the hard rule in your system prompt.\n"
        if sheet.layout in ("C1", "C2")
        else ""
    )

    tsd_testing = ""
    tsd_interface = ""
    if tsd_sections:
        tsd_testing = (tsd_sections.get("testing_verification") or "").strip()
        tsd_interface = (tsd_sections.get("interface_spec") or "").strip()

    tsd_block = ""
    if tsd_testing:
        tsd_block = (
            "\n## TSD Testing & Verification (authoritative scenario source)\n"
            "Emit ONE test case per scenario named below. Preserve the "
            "scenario's intent and description verbatim.\n"
            f"```\n{tsd_testing[:4000]}\n```\n"
        )
    if tsd_interface:
        tsd_block += (
            "\n## TSD Interface Specification (authoritative API source)\n"
            "Use these API names on each test case — do NOT invent or "
            "translate to canonical names.\n"
            f"```\n{tsd_interface[:2500]}\n```\n"
        )

    codes_hint = ""
    if brd_codes:
        codes_hint = (
            f"\n## Error codes named in the BRD (use these verbatim on Failure cases)\n"
            f"{sorted(brd_codes)}\n"
            "If a scenario has no BRD-named code, leave `response_code` empty. "
            "Do NOT fall back to canonical UPI codes.\n"
        )

    return (
        "Plan the TEST CASES for ONE sheet of an in-progress workbook. Output "
        "ONLY a JSON object with a single key `test_cases` whose value is a "
        "list of TestCaseStub objects.\n\n"
        f"Workbook skeleton (for context): {skeleton_summary}\n\n"
        f"Target sheet: name={sheet.name!r}, layout={sheet.layout!r}, "
        f"metadata={sheet.metadata}\n\n"
        f"EnrichedBrief: {brief.model_dump_json()}\n"
        f"{tsd_block}"
        f"{codes_hint}\n"
        "Required:\n"
        f"{cap_instruction}"
        f"{initiated_by_instruction}"
        "- Pair Success/Failure cases via `pair_id` where applicable.\n"
        "- `api` on each case = one of the APIs from the TSD Interface Spec "
        "or `brief.apis` (verbatim, no invention).\n"
        "- `response_code` = the BRD-named error code for the scenario; empty "
        "when the BRD is silent.\n"
        "- `coverage_tag` = a short slug for the scenario intent (e.g. "
        "\"happy_path\", \"timeout\", \"duplicate_vpa\").\n\n"
        "Output strictly valid JSON of shape: {\"test_cases\": [ ... ]}"
    )


async def _plan_sheet_stubs(
    brief: EnrichedBrief, skeleton: WorkbookPlan, sheet: SheetSpec,
    per_sheet_cap: int | None = None,
    tsd_sections: dict | None = None,
    brd_codes: frozenset[str] | set[str] = frozenset(),
) -> list[TestCaseStub]:
    """Phase 2: produce test_cases for one sheet."""

    client = get_client("planner")
    system = [
        SystemBlock(text=load_prompt("planner.md"), cache=True),
        SystemBlock(
            text=(
                "## Stubs-only mode\n"
                "You are in STUBS mode for this call. Output ONLY a JSON "
                "object with key `test_cases`. Do NOT emit a full WorkbookPlan "
                "or any other top-level fields."
            ),
            cache=False,
        ),
    ]
    base_msg = _format_stubs_user_message(
        brief, skeleton, sheet, per_sheet_cap, tsd_sections, brd_codes,
    )
    user_msg = base_msg

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = await client.complete(
                system=system,
                messages=[Message(role="user", content=user_msg)],
                max_tokens=12000,
                response_format="json",
                extended_thinking=True,
            )
            payload = parse_json_response(response.text)
            stubs_raw = payload.get("test_cases", []) if isinstance(payload, dict) else []
            stubs = [TestCaseStub.model_validate(t) for t in stubs_raw]
            if not stubs:
                LOGGER.warning(
                    "planner.stubs.empty", sheet=sheet.name, attempt=attempt,
                )
                user_msg = _retry_message(
                    base_msg,
                    f"Your previous response for sheet {sheet.name!r} had an "
                    "empty `test_cases` list. Generate the required test cases "
                    "for this sheet.",
                )
                last_error = PlanningError(f"empty stubs for sheet {sheet.name!r}")
                continue
            LOGGER.info(
                "planner.stubs.ok",
                sheet=sheet.name, attempt=attempt, count=len(stubs),
            )
            return stubs
        except (ValidationError, ValueError) as exc:
            LOGGER.warning(
                "planner.stubs.invalid_json",
                sheet=sheet.name, attempt=attempt, error=repr(exc)[:200],
            )
            user_msg = _retry_message(
                base_msg,
                f"Your previous response for sheet {sheet.name!r} was not "
                f"valid: {exc}\nReturn ONLY {{\"test_cases\": [...]}} JSON, "
                "matching the TestCaseStub schema exactly.",
            )
            last_error = exc
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("planner.stubs.provider_error", sheet=sheet.name, error=repr(exc))
            raise LLMProviderError(
                f"Planner (stubs phase) provider call failed for sheet "
                f"{sheet.name!r}: {exc!r}"
            ) from exc

    raise PlanningError(
        f"Planner stubs phase failed for sheet {sheet.name!r} after 3 attempts: "
        f"{last_error!r}"
    )


# ── Orchestration ──────────────────────────────────────────────────────────


def _assert_brief_has_roles(brief: EnrichedBrief) -> None:
    """Require at least one role to build sheets from, before any LLM call.

    This is the ONLY role-count precondition left. There is no upper bound and
    no per-archetype minimum: the pack carries exactly the roles the BRD/TSD
    describes — two if it names two, six if it names six.

    Zero is still fatal, and worth catching here rather than three LLM calls
    later: with no roles there are no test-case-bearing sheets, so the
    renderer would emit a workbook of pure annexure with nothing in it.
    """
    roles = [r for r in (brief.roles or []) if r and r.strip()]
    if roles:
        return

    LOGGER.error("planner.no_roles_in_brief", archetype=brief.archetype)
    raise ConfigurationError(
        "This BRD/TSD does not name any role/party to build test-case sheets "
        "for, so the workbook would have no test cases. Name at least one "
        "role (the parties the flow runs between) in the TSD and regenerate."
    )


async def plan(brief: EnrichedBrief, options: dict | None = None) -> WorkbookPlan:
    """Produce a complete WorkbookPlan via two-phase LLM planning.

    Phase 1 produces the skeleton; Phase 2 fills in per-sheet test cases.
    BRD/TSD-only: the volume of test cases is whatever the TSD Testing &
    Verification section lists (bounded per sheet by the runtime cap, then
    a hard cap of ``_MAX_ROWS_PER_PACK`` per sheet as a runaway guard).

    The role-sheet count follows the brief's roles — i.e. the BRD/TSD — with
    no archetype-derived minimum or maximum imposed on it.
    """

    _assert_brief_has_roles(brief)
    skeleton = await _plan_skeleton(brief)

    import asyncio as _asyncio
    from app.excel_testcase_engine.config import load_runtime_config

    role_sheets = [s for s in skeleton.sheets if s.layout in _TEST_CASE_LAYOUTS]
    runtime = load_runtime_config()
    sem = _asyncio.Semaphore(max(1, runtime.writer.max_concurrent_batches))

    engine_context = options or {}
    tsd_sections = engine_context.get("tsd_sections") or {}
    brd_codes = _codes_from_brd(brief)

    # Per-sheet safety cap: split the runtime test_case_cap evenly across
    # role sheets, then clamp to _MAX_ROWS_PER_PACK. The LLM is instructed
    # to size from the TSD; this is the runaway guard, not a target.
    global_cap = min(runtime.test_case_cap or _MAX_ROWS_PER_PACK, _MAX_ROWS_PER_PACK)
    per_sheet_caps: dict[str, int] = {}
    if global_cap and global_cap > 0 and role_sheets:
        even_cap = max(1, global_cap // len(role_sheets))
        per_sheet_caps = {s.name: even_cap for s in role_sheets}

    LOGGER.info(
        "planner.stubs.start",
        role_sheets=len(role_sheets),
        max_concurrent=runtime.writer.max_concurrent_batches,
        per_sheet_cap=per_sheet_caps,
        tsd_keys=sorted(tsd_sections.keys()),
        brd_codes=sorted(brd_codes),
    )

    async def _stubs_for(sheet):
        async with sem:
            sheet_cap = per_sheet_caps.get(sheet.name)
            return sheet, await _plan_sheet_stubs(
                brief, skeleton, sheet, sheet_cap,
                tsd_sections=tsd_sections, brd_codes=brd_codes,
            )

    # return_exceptions=True: a sheet failing after its 3-attempt budget must
    # NOT cancel siblings. Synthesise a one-stub placeholder so the workbook
    # still ships. Operator sees the failure in logs and can regenerate.
    results = await _asyncio.gather(
        *(_stubs_for(s) for s in role_sheets),
        return_exceptions=True,
    )
    for idx, result in enumerate(results):
        sheet = role_sheets[idx]
        if isinstance(result, BaseException):
            LOGGER.error(
                "planner.stubs.sheet_failed",
                sheet=sheet.name, error=repr(result)[:300],
            )
            sheet.test_cases = _placeholder_stubs_for_sheet(sheet, result, brief)
            continue
        _, stubs = result
        sheet.test_cases = stubs

    # Safety trim + coverage_audit recompute.
    if per_sheet_caps:
        trimmed_total = 0
        for sheet in role_sheets:
            cap = per_sheet_caps.get(sheet.name)
            if cap and cap > 0 and len(sheet.test_cases) > cap:
                trimmed_total += len(sheet.test_cases) - cap
                sheet.test_cases = sheet.test_cases[:cap]
        if trimmed_total:
            LOGGER.info(
                "planner.cap.trimmed",
                trimmed=trimmed_total, per_sheet_caps=per_sheet_caps,
            )

        from collections import defaultdict as _dd
        audit: dict[str, dict[str, int]] = _dd(lambda: _dd(int))
        for sheet in skeleton.sheets:
            for tc in sheet.test_cases:
                for api in tc.apis:
                    audit[api][tc.coverage_tag] += 1
        skeleton.coverage_audit = {api: dict(tags) for api, tags in audit.items()}

    LOGGER.info(
        "planner.ok",
        sheets=len(skeleton.sheets),
        cases=sum(len(s.test_cases) for s in skeleton.sheets),
    )
    return skeleton
