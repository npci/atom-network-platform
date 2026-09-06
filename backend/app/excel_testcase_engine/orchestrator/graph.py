# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

# INTEGRATION: LangGraph workflow for the host-embedded engine.
#
# WHY this is a full rewrite of the standalone graph (rather than a drop-in
# copy with import swaps):
#
#   1. The standalone graph used `langgraph.checkpoint.sqlite.AsyncSqliteSaver`
#      to persist state at every node so a NEEDS_INPUT pause could resume
#      across server restarts. The host project already has Redis + Postgres
#      job durability via `app.services.job_registry` (Slice R-1/R-5/R-9).
#      Layering a second checkpointer would create two sources of truth and
#      a confusing ops story. We drop it; resume is handled at the WS layer.
#
#   2. The standalone graph wired `CostRecorder` to a contextvar so every LLM
#      call recorded usage to a SQLite ledger. The host's `core.llm.stream_llm`
#      already emits structured trace events to the host observability layer
#      (Slice 28). We drop the engine cost ledger and let the host meter.
#
#   3. The standalone graph used a custom `JobStore` for per-job artifact
#      paths. Here those paths go to the host's job_registry via the jobs
#      adapter, keyed on the host-issued `registry_job_id`. One source of
#      truth for "what did this job produce."
#
# What stays the same: the seven nodes (enhance, await_input, plan, write,
# render, validate, repair, revalidate, attach_report, deliver) and their
# conditional edges. The state machine semantics — including one-shot repair
# and the warnings fall-through — are unchanged.

from __future__ import annotations

import contextvars
import json
import uuid
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, TypedDict

from langgraph.graph import END, StateGraph

from app.excel_testcase_engine import excel_writer
from app.excel_testcase_engine.adapters import jobs as jobs_adapter
from app.excel_testcase_engine.agents import enhancer, planner, post_processor, repairer, strategist, validator, writer
from app.excel_testcase_engine.excel_writer.exporters import write_companions
from app.excel_testcase_engine.observability import get_logger, stage_timer
from app.excel_testcase_engine.orchestrator.status import JobProgress, JobStatus
from app.excel_testcase_engine.schemas.enriched_brief import EnrichedBrief
from app.excel_testcase_engine.schemas.validation_report import ValidationReport
from app.excel_testcase_engine.schemas.workbook_plan import WorkbookPlan

LOGGER = get_logger("excel_engine.graph")

# WHY a configurable artifact dir: in the standalone build artifacts went to
# `outputs/artifacts/<job_id>/`. In the host project, the operator may want
# them under a different mount (e.g. `/var/lib/platform/excel-engine-artifacts/`)
# without changing engine code. The injector sets this path at startup.
_ARTIFACTS_DIR: Path = Path("outputs") / "excel_engine_artifacts"
_OUTPUTS_DIR: Path = Path("outputs") / "excel_engine_workbooks"


def configure_paths(*, artifacts_dir: Path, outputs_dir: Path) -> None:
    """Bind the host's artifact + workbook output directories. Called by injector."""

    global _ARTIFACTS_DIR, _OUTPUTS_DIR
    _ARTIFACTS_DIR = artifacts_dir
    _OUTPUTS_DIR = outputs_dir
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    _OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)


# WHY contextvar: the progress callback isn't msgpack-serialisable, so it
# can't live on graph state. We bind it for the duration of `run_workflow`
# and the nodes look it up.
_progress_cb: contextvars.ContextVar[Callable[[JobProgress], None] | None] = contextvars.ContextVar(
    "excel_engine_progress_cb", default=None,
)


def _merge(left: dict | None, right: dict | None) -> dict:
    return {**(left or {}), **(right or {})}


class GraphState(TypedDict, total=False):
    """Mutable state passed between graph nodes."""

    job_id: str
    registry_job_id: str
    brief: str
    options: dict
    answers: dict
    enriched: EnrichedBrief | None
    plan: WorkbookPlan | None
    output_path: str | None
    rendered_path: str | None
    report: ValidationReport | None
    repair_attempted: bool
    final_status: JobStatus
    metrics: Annotated[dict[str, Any], _merge]


# Stage → overall-progress band. Single source of truth — streaming.py
# imports this rather than duplicating the table. Writer covers the widest
# band because it's the longest phase; we slice it finely via current/total
# in compute_progress_pct(). The values are public (no underscore) precisely
# so the streaming layer can reuse them.
#
# Keys are lower-case strings (JobStatus.value) so the streaming layer
# can look up by `event.status.value` without importing the enum.
STAGE_PROGRESS_BAND: dict[str, tuple[int, int]] = {
    JobStatus.PENDING.value:                 (0,   2),
    JobStatus.ENHANCING.value:               (2,   10),
    JobStatus.NEEDS_INPUT.value:             (10,  10),
    JobStatus.PLANNING.value:                (10,  20),
    JobStatus.WRITING.value:                 (20,  80),
    JobStatus.RENDERING.value:               (80,  85),
    JobStatus.VALIDATING.value:              (85,  95),
    JobStatus.REPAIRING.value:               (90,  95),
    JobStatus.COMPLETED.value:               (100, 100),
    JobStatus.COMPLETED_WITH_WARNINGS.value: (100, 100),
    JobStatus.FAILED.value:                  (100, 100),
}


def compute_progress_pct(stage: str, current: int, total: int) -> int:
    """Compute overall progress (0-100) from stage band + current/total.

    `stage` is the JobStatus.value string (e.g. "writing"). When the
    stage exposes useful current/total counters (writer, validator), we
    interpolate inside the band. Otherwise we return the band's start.
    """

    band_start, band_end = STAGE_PROGRESS_BAND.get(stage, (0, 100))
    if total > 0 and current >= 0:
        frac = max(0.0, min(1.0, current / total))
        return int(band_start + (band_end - band_start) * frac)
    return band_start


def _emit(state: GraphState, status: JobStatus, message: str = "", **kwargs: Any) -> JobProgress:
    """Push a JobProgress event onto the queue (drained by streaming.py).

    The drain coroutine is responsible for writing to the host job_registry
    (current_stage + progress_pct). _emit deliberately does NOT write to the
    registry itself — that would double the DB writes for every stage
    transition (since the writer's per-batch callback also pushes to the
    same queue and the drain handles both). Single source of truth = drain.
    """
    current = int(kwargs.get("current", 0))
    total = int(kwargs.get("total", 0))
    progress = JobProgress(
        job_id=state.get("job_id", ""),
        status=status,
        message=message,
        current=current,
        total=total,
        open_questions=list(kwargs.get("open_questions") or []),
    )
    callback = _progress_cb.get()
    if callback is not None:
        try:
            callback(progress)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("progress.callback_failed", error=repr(exc))
    return progress


def _options_with_enriched(state: GraphState) -> dict:
    """Return options with `new_api_names` injected from `state["enriched"]`.

    Writer + Validator need `enriched.new_api_names` to build the step-linter's
    per-run allow-list — else genuinely new APIs (approved by the Enhancer,
    accepted by the Planner) trip `invalid_api_in_steps` findings. Piggybacks
    on the existing `options` dict so no signature changes are needed downstream.
    Safe when `enriched` is absent: injects an empty list.
    """
    opts = dict(state.get("options") or {})
    enriched = state.get("enriched")
    if enriched is not None:
        opts["new_api_names"] = list(getattr(enriched, "new_api_names", []) or [])
    else:
        opts.setdefault("new_api_names", [])
    return opts


def _write_artifact(job_id: str, stage: str, payload: object) -> None:
    folder = _ARTIFACTS_DIR / job_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stage}.json"
    serialised = (
        payload.model_dump_json(indent=2)
        if hasattr(payload, "model_dump_json")
        else json.dumps(
            payload.model_dump() if hasattr(payload, "model_dump") else payload,
            ensure_ascii=True, indent=2, default=str,
        )
    )
    path.write_text(serialised, encoding="utf-8")


# --- Nodes -------------------------------------------------------------------


async def node_enhance(state: GraphState) -> dict[str, Any]:
    _emit(state, JobStatus.ENHANCING, "Enhancing brief")
    with stage_timer(LOGGER, "enhance", job_id=state.get("job_id")):
        brief = state["brief"]
        if state.get("answers"):
            brief = brief + "\n\nUser clarifications:\n" + json.dumps(state["answers"], ensure_ascii=True)
        enriched = await enhancer.enhance(brief, state.get("options") or {})
    _write_artifact(state["job_id"], "01-enriched_brief", enriched)
    return {"enriched": enriched, "metrics": {"enhance_ok": True}}


async def node_await_input(state: GraphState) -> dict[str, Any]:
    """Surface open_questions; pause iff options.pause_on_questions is True."""

    enriched = state.get("enriched")
    if not (enriched and enriched.open_questions):
        return {}
    options = state.get("options") or {}
    if options.get("pause_on_questions") and not state.get("answers"):
        _emit(
            state,
            JobStatus.NEEDS_INPUT,
            "Awaiting clarification",
            open_questions=enriched.open_questions,
        )
        return {"final_status": JobStatus.NEEDS_INPUT}
    LOGGER.info("enhancer.questions_surfaced", count=len(enriched.open_questions))
    _emit(
        state,
        JobStatus.ENHANCING,
        f"Proceeding with best guess. Open questions ({len(enriched.open_questions)}).",
        open_questions=enriched.open_questions,
    )
    return {"metrics": {"open_questions": len(enriched.open_questions)}}


async def node_plan(state: GraphState) -> dict[str, Any]:
    _emit(state, JobStatus.PLANNING, "Planning workbook")
    with stage_timer(LOGGER, "plan", job_id=state.get("job_id")):
        # BRD/TSD-only: options carries tsd_sections (plus archetype /
        # change_request_id). Planner and downstream stages read from it.
        plan = await planner.plan(
            state["enriched"],  # type: ignore[arg-type]
            options=state.get("options"),
        )
    _write_artifact(state["job_id"], "02-workbook_plan", plan)
    cases = sum(len(s.test_cases) for s in plan.sheets)
    return {"plan": plan, "metrics": {"planned_cases": cases, "sheets": len(plan.sheets)}}


async def node_strategy(state: GraphState) -> dict[str, Any]:
    """Slice 3 (cert-tc-v2) — synthesise + persist the CertificationStrategy.

    Deterministic-only: no LLM call, no I/O beyond the artifact write. Runs
    right after the planner (post-plan, pre-write) so the strategy is
    on disk before the long writer stage risks anything. Failures here MUST
    NOT abort the run — the strategy artifact is a reviewer convenience;
    the xlsx is the primary deliverable. On unexpected exception, log and
    return unchanged state.
    """

    plan = state.get("plan")
    if plan is None:
        return {}

    # Rebuild an EnrichedBrief-shaped object from state without re-invoking
    # the enhancer. The graph doesn't carry the EnrichedBrief through state
    # (only the raw brief string), so we rehydrate the fields the strategy
    # actually reads. Pull classification/existing/new/assumptions from
    # state.enriched if present, else fall back to safe defaults so the
    # deterministic synthesis still runs.
    enriched = state.get("enriched")
    if enriched is None:
        return {}

    try:
        with stage_timer(LOGGER, "strategy_synthesis", job_id=state.get("job_id")):
            strategy = strategist.synthesize_strategy(enriched, plan)  # type: ignore[arg-type]
        _write_artifact(state["job_id"], "02c-certification_strategy", strategy)
        LOGGER.info(
            "strategy.ok",
            classification=strategy.api_classification,
            affected_apis=len(strategy.affected_apis),
            affected_legs=len(strategy.affected_message_legs),
            business_rules=len(strategy.business_rules),
            fields_added=len(strategy.fields_added),
            fields_modified=len(strategy.fields_modified),
        )
        return {"metrics": {"strategy_written": True}}
    except Exception as exc:  # noqa: BLE001
        # WHY defensive: strategy is advisory. A bug in synthesize_strategy
        # or artifact write must not fail the whole workbook run — the user
        # loses reviewer prose, not their deliverable.
        LOGGER.warning("strategy.synth_failed", error=repr(exc)[:200])
        return {"metrics": {"strategy_written": False}}


async def node_write(state: GraphState) -> dict[str, Any]:
    plan = state["plan"]
    cases = sum(len(s.test_cases) for s in plan.sheets)  # type: ignore[union-attr]
    _emit(state, JobStatus.WRITING, "Writing test cases", total=cases)
    try:
        with stage_timer(LOGGER, "write", job_id=state.get("job_id")):
            # v1: pass options so the Writer can build per-batch Feature
            # grounding blocks from BRD FRs + feature criteria + PM signals.
            # Also inject enriched.new_api_names so the step-linter's allow-list
            # matches the Planner's (else new APIs the Enhancer approved fire
            # spurious `invalid_api_in_steps` findings).
            plan = await writer.write_all(
                plan,
                on_progress=_progress_cb.get(),
                options=_options_with_enriched(state),
            )
    except Exception:
        # WHY persist on failure too: opaque writer failures cost real
        # debug time. The partial plan is always inspectable in
        # <artifact_dir>/<job_id>/03-rendered_plan_partial.json.
        try:
            _write_artifact(state["job_id"], "03-rendered_plan_partial", plan)
        except Exception:
            pass
        raise
    with stage_timer(LOGGER, "post_process", job_id=state.get("job_id")):
        plan = post_processor.post_process(plan)
    _write_artifact(state["job_id"], "03-rendered_plan", plan)
    return {"plan": plan}


async def node_render(state: GraphState) -> dict[str, Any]:
    plan = state["plan"]
    # WHY .resolve(): downstream consumers (download endpoint, _attach_files,
    # write_companions) use this path across request boundaries / threads
    # where cwd is not guaranteed to be the engine's working dir. Storing an
    # absolute path makes the file lookup robust regardless of cwd.
    output_path = Path(
        state.get("output_path") or _OUTPUTS_DIR / f"{state.get('job_id','run')}.xlsx"
    ).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _emit(state, JobStatus.RENDERING, "Rendering Excel workbook")
    with stage_timer(LOGGER, "render", job_id=state.get("job_id")):
        rendered = excel_writer.render(plan, output_path)  # type: ignore[arg-type]
    return {"output_path": str(output_path), "rendered_path": str(Path(rendered).resolve())}


async def node_validate(state: GraphState) -> dict[str, Any]:
    _emit(state, JobStatus.VALIDATING, "Validating workbook")
    with stage_timer(LOGGER, "validate", job_id=state.get("job_id")):
        # v1: pass options so the Validator can run scope + floor + error-code
        # + fr_link checks against the same context the Planner enforced.
        report = await validator.validate(
            state["plan"],  # type: ignore[arg-type]
            Path(state["rendered_path"]),
            options=_options_with_enriched(state),
        )
    _write_artifact(state["job_id"], "04-validation_report", report)
    return {"report": report, "metrics": {"defects": len(report.defects)}}


async def node_repair(state: GraphState) -> dict[str, Any]:
    _emit(state, JobStatus.REPAIRING, "Repairing workbook")
    with stage_timer(LOGGER, "repair", job_id=state.get("job_id")):
        patches = await repairer.repair(
            state["plan"], Path(state["rendered_path"]), state["report"].defects,  # type: ignore[arg-type]
        )
        if patches:
            excel_writer.apply_patches(Path(state["rendered_path"]), patches)
    _write_artifact(state["job_id"], "05-patches", {"patches": [p.model_dump() for p in patches]})
    return {"repair_attempted": True, "metrics": {"patches": len(patches)}}


async def node_revalidate(state: GraphState) -> dict[str, Any]:
    _emit(state, JobStatus.VALIDATING, "Re-validating after repair")
    with stage_timer(LOGGER, "revalidate", job_id=state.get("job_id")):
        report = await validator.validate(
            state["plan"],  # type: ignore[arg-type]
            Path(state["rendered_path"]),
            options=_options_with_enriched(state),
        )
    _write_artifact(state["job_id"], "06-revalidation_report", report)
    return {"report": report}


def _attach_files(state: GraphState) -> None:
    """Write md/docx companions next to the xlsx and stash file paths in
    state.metrics so the subsequent mark_complete call carries them through
    to result_payload (the channel the host download endpoint reads).

    GraphState["metrics"] is annotated with a merge reducer, but mutating
    here is also fine: the very next consumer (mark_complete) is in the
    SAME node call, so it sees the in-place update directly. The merge
    reducer only matters across node boundaries.
    """

    rendered_path = state.get("rendered_path")
    plan = state.get("plan")
    if not (rendered_path and plan):
        return
    # Thread the host's change_request_id into the JSON companion so the
    # cert-simulator contract has the linkage it expects (matches the
    # `change_request_id` field in the reference example fixtures). The
    # WS handler injects it via options.change_request_id on the engine
    # entry call site (see api/agents.py cert_test_cases branch).
    options = state.get("options") or {}
    change_request_id = str(options.get("change_request_id") or "")
    enriched = state.get("enriched")
    feature_name = getattr(enriched, "feature_name", None) if enriched else None

    # Always start with the xlsx — the user-facing test-case sheet is the
    # source of truth and is rendered upstream of this call. Companion
    # failures must NEVER mask a successful workbook.
    files: dict[str, str] = {"xlsx": str(rendered_path)}
    try:
        companions = write_companions(
            plan,
            Path(rendered_path),
            change_request_id=change_request_id,
            feature_name=feature_name,
        ) or {}
    except Exception as exc:  # noqa: BLE001
        # Defensive: write_companions handles its own per-artifact errors
        # internally, so this should be unreachable. We keep the catch so
        # an unexpected explosion (e.g. an import-time failure) still
        # leaves us with the xlsx wired into result_payload.
        LOGGER.warning("companions.unexpected_failure", error=repr(exc))
        companions = {}

    for key, path in companions.items():
        files[key] = str(path)
    if "json" not in files:
        LOGGER.warning(
            "companions.json_missing job=%s xlsx=%s",
            state.get("job_id"), rendered_path,
        )

    metrics = state.get("metrics") or {}
    metrics["files"] = files
    state["metrics"] = metrics  # type: ignore[index]


async def node_attach_report(state: GraphState) -> dict[str, Any]:
    excel_writer.append_validation_report_sheet(
        Path(state["rendered_path"]), state["report"].defects,  # type: ignore[arg-type]
    )
    _attach_files(state)
    _emit(state, JobStatus.COMPLETED_WITH_WARNINGS, "Workbook completed with validation report")
    registry_job_id = state.get("registry_job_id")
    if registry_job_id:
        # WHY mark complete (not failed): the workbook IS delivered; the
        # warnings live in the appended Validation_Report sheet. Treating
        # "completed_with_warnings" as a failed job would lose the file in
        # the host UI's filtered "active jobs" tray.
        jobs_adapter.mark_complete(registry_job_id, summary={
            "status": "completed_with_warnings",
            **(state.get("metrics") or {}),
        })
    return {"final_status": JobStatus.COMPLETED_WITH_WARNINGS}


async def node_deliver(state: GraphState) -> dict[str, Any]:
    _attach_files(state)
    _emit(state, JobStatus.COMPLETED, "Workbook completed")
    registry_job_id = state.get("registry_job_id")
    if registry_job_id:
        jobs_adapter.mark_complete(registry_job_id, summary={
            "status": "completed",
            **(state.get("metrics") or {}),
        })
    return {"final_status": JobStatus.COMPLETED}


# --- Conditional edges -------------------------------------------------------


def route_after_enhance(state: GraphState) -> Literal["await_input", "planner"]:
    enriched = state.get("enriched")
    options = state.get("options") or {}
    if enriched and enriched.open_questions and options.get("pause_on_questions") and not state.get("answers"):
        return "await_input"
    return "planner"


def route_after_await(state: GraphState) -> Literal["planner", "__end__"]:
    if state.get("final_status") == JobStatus.NEEDS_INPUT:
        return "__end__"
    return "planner"


def route_after_validate(state: GraphState) -> Literal["repair", "deliver"]:
    report = state.get("report")
    if report and report.has_critical:
        return "repair"
    return "deliver"


def route_after_revalidate(state: GraphState) -> Literal["attach_report", "deliver"]:
    report = state.get("report")
    if report and report.has_critical:
        return "attach_report"
    return "deliver"


# --- Graph builder -----------------------------------------------------------


def _build_uncompiled() -> StateGraph:
    graph = StateGraph(GraphState)
    graph.add_node("enhance", node_enhance)
    graph.add_node("await_input", node_await_input)
    # WHY node name "planner" (not "plan"): LangGraph forbids node names that
    # collide with state-dict keys, and GraphState already has a `plan` key
    # (the WorkbookPlan). The agent module is still called planner.py.
    graph.add_node("planner", node_plan)
    # Slice 3 — deterministic synthesis + artifact persist. Sits between
    # planner and write so the reviewer's pre-flight document is on
    # disk before the long Writer stage begins.
    graph.add_node("strategy", node_strategy)
    graph.add_node("write", node_write)
    graph.add_node("render", node_render)
    graph.add_node("validate", node_validate)
    graph.add_node("repair", node_repair)
    graph.add_node("revalidate", node_revalidate)
    graph.add_node("attach_report", node_attach_report)
    graph.add_node("deliver", node_deliver)

    graph.set_entry_point("enhance")
    graph.add_conditional_edges("enhance", route_after_enhance, {"await_input": "await_input", "planner": "planner"})
    graph.add_conditional_edges("await_input", route_after_await, {"planner": "planner", "__end__": END})
    graph.add_edge("planner", "strategy")
    graph.add_edge("strategy", "write")
    graph.add_edge("write", "render")
    graph.add_edge("render", "validate")
    graph.add_conditional_edges("validate", route_after_validate, {"repair": "repair", "deliver": "deliver"})
    graph.add_edge("repair", "revalidate")
    graph.add_conditional_edges("revalidate", route_after_revalidate, {"attach_report": "attach_report", "deliver": "deliver"})
    graph.add_edge("attach_report", END)
    graph.add_edge("deliver", END)
    return graph


_compiled = None


def _get_compiled():
    """Lazy-compiled graph. WHY lazy: avoids paying compile cost at import."""

    global _compiled
    if _compiled is None:
        _compiled = _build_uncompiled().compile()
    return _compiled


async def run_workflow(
    brief: str,
    options: dict | None = None,
    on_progress: Callable[[JobProgress], None] | None = None,
    *,
    job_id: str | None = None,
    registry_job_id: str | None = None,
    answers: dict | None = None,
) -> Path:
    """Run the graph end-to-end.

    Args:
        brief:           User's free-text brief.
        options:         dict with `archetype` (A/B/C) and other tunables.
        on_progress:     callback for live JobProgress events (WS layer uses this).
        job_id:          internal engine identifier (defaults to a fresh UUID).
        registry_job_id: the host's `agent_jobs.id` — engine writes file paths,
                         stage names, and final status under this id so the
                         existing resume / sidebar UI works without changes.
        answers:         user clarifications, when resuming a NEEDS_INPUT pause.
    """

    options = dict(options or {})
    job_id = job_id or options.get("job_id") or str(uuid.uuid4())
    output_path = Path(options.get("output") or _OUTPUTS_DIR / f"{job_id}.xlsx")
    _OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    initial: GraphState = {
        "job_id": job_id,
        "registry_job_id": registry_job_id or "",
        "brief": brief,
        "options": options,
        "answers": answers or {},
        "output_path": str(output_path),
        "repair_attempted": False,
        "final_status": JobStatus.PENDING,
        "metrics": {},
    }

    cb_token = _progress_cb.set(on_progress)
    try:
        compiled = _get_compiled()
        final_state = await compiled.ainvoke(initial)
    except Exception as exc:
        # WHY catch + re-raise after marking failed: the host job_registry
        # needs to know this job failed; without that the resume sidebar
        # shows a stuck-running job indefinitely.
        if registry_job_id:
            jobs_adapter.mark_failed(registry_job_id, error=str(exc), stage="Workflow failed")
        raise
    finally:
        _progress_cb.reset(cb_token)

    return Path(final_state.get("rendered_path") or output_path)
