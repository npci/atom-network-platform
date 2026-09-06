# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Script-based UAT for Phase B — the combined test-gen + test-exec step.

The two mock UAT steps are replaced by ONE operator-supplied script (validated
against ``PHASE_B_SCRIPT_ROOT`` by the API layer) that both produces and
executes the change's UAT suite. The script's stdout/stderr is the artefact:
streamed into ``uat_test_runs.log`` every ~2s while it runs (the UI polls the
row), parsed for per-case markers and a final summary, and later read by the
AI triage stage.

Output contract (see backend/examples/phase_b_scripts for working samples):

    per-case lines:   PASS <id> <title>   /  FAIL <id> <title>  /  SKIP ...
    final summary:    TESTS: total=N passed=N failed=N skipped=N

The summary line wins when present; otherwise the counted markers stand; a
script that emits neither is judged by exit code alone (0 → one implicit
passed case, non-zero → one failed). The step ALWAYS advances to TRIAGE once
the script has run — failures are exactly what triage exists to look at; only
a script that could not start leaves the step where it was.
"""
from __future__ import annotations

import logging
import re
import shlex
import time
from typing import Optional

from sqlalchemy.orm import Session

from app.core import diag
from app.core.config import settings
from app.models.base import utcnow
from app.models.phase_b import (
    PhaseBRun, PhaseBStep, TestRunStatus, UATTestRun,
)
# Same ANSI handling as the build path — a colorized script must not leak
# escape codes into the stored log (UI pane + the AI-triage prompt read it).
from app.services.build_runner import _strip_ansi
from app.services.local_runner import stream_local_command

logger = logging.getLogger(__name__)

# Same runaway-output ceiling as the build runner: beyond this the OLDEST
# lines are dropped (the tail is what diagnoses a failure) and the log says so.
_MAX_LOG_LINES = 20_000

_RESULT_LINE = re.compile(r"^\s*(PASS|FAIL|SKIP)\b", re.IGNORECASE)
_SUMMARY_LINE = re.compile(
    r"^\s*TESTS:\s*total=(\d+)\s+passed=(\d+)\s+failed=(\d+)(?:\s+skipped=(\d+))?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_test_log(log: str, exit_code: int) -> dict:
    """Derive {total, passed, failed, skipped} from a UAT script's output.

    Pure + deterministic. The LAST ``TESTS:`` summary line wins when present
    (a wrapper script may run several suites); else PASS/FAIL/SKIP markers are
    counted; else the exit code alone decides.
    """
    summaries = _SUMMARY_LINE.findall(log or "")
    if summaries:
        total, passed, failed, skipped = summaries[-1]
        return {"total": int(total), "passed": int(passed),
                "failed": int(failed), "skipped": int(skipped or 0)}
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for line in (log or "").splitlines():
        m = _RESULT_LINE.match(line)
        if m:
            counts[m.group(1).upper()] += 1
    if any(counts.values()):
        return {"total": sum(counts.values()), "passed": counts["PASS"],
                "failed": counts["FAIL"], "skipped": counts["SKIP"]}
    ok = exit_code == 0
    return {"total": 1, "passed": 1 if ok else 0, "failed": 0 if ok else 1, "skipped": 0}


async def run_uat_script(
    run: PhaseBRun,
    db: Session,
    *,
    test_run: UATTestRun,
    script_path: str,
    base_url: Optional[str] = None,
) -> UATTestRun:
    """Execute the validated UAT script, streaming its log onto *test_run*.

    The caller owns the session (a background task uses its own) and has
    already validated `script_path` via services.script_paths. `base_url`,
    when given, is passed to the script as its first argument.
    """
    argv_tail = f" {shlex.quote(base_url)}" if base_url else ""
    command = f"bash {shlex.quote(script_path)}{argv_tail}"

    lines: list[str] = []
    truncated = False
    exit_code = -1
    started = time.monotonic()
    deadline = started + max(60, int(settings.phase_b_script_timeout_seconds))
    last_flush = started

    def _text() -> str:
        head = ([f"[backend] output exceeded {_MAX_LOG_LINES} lines — oldest lines "
                 "dropped; the diagnostics log holds the full stream"]
                if truncated else [])
        return "\n".join(head + lines)

    logger.info("UAT script started: change=%s test_run=%s script=%s",
                run.change_request_id, test_run.id, script_path)
    stream = stream_local_command(command)
    # Durable, untruncated stream — same guarantee the build path gives via
    # its per-build diag file, so lines the 20k cap drops are not lost.
    blog = diag.open_build_log(f"uat-{run.change_request_id}",
                               utcnow().strftime("%Y%m%d-%H%M%S"))
    try:
        blog.write(f"=== UAT script — change={run.change_request_id} "
                   f"test_run={test_run.id} ===")
        blog.write(f"command: {command}")
        blog.write("-" * 72)
        async for kind, payload in stream:
            if kind == "exit":
                exit_code = int(payload) if isinstance(payload, int) else -1
                break
            line = _strip_ansi(str(payload))
            lines.append(line)
            blog.write(line)
            if len(lines) > _MAX_LOG_LINES:
                del lines[0]
                truncated = True
            now = time.monotonic()
            if now - last_flush >= 2.0:
                test_run.log = _text()
                db.commit()
                last_flush = now
            if now > deadline:
                # Closing the generator kills the bash process
                # (stream_local_command's finally) — a hung suite must not
                # hold a RUNNING row forever.
                msg = (f"[backend] script exceeded the "
                       f"{settings.phase_b_script_timeout_seconds}s ceiling — killed")
                lines.append(msg)
                blog.write(msg)
                await stream.aclose()
                exit_code = -1
                break
    except Exception as e:  # noqa: BLE001 — the run must land in a terminal state
        logger.exception("UAT script stream error: change=%s", run.change_request_id)
        lines.append(f"[backend] stream error: {e}")
        blog.write(f"[backend] stream error: {e}")
        exit_code = -1
    finally:
        blog.write(f"[exit={exit_code} finished={utcnow().isoformat()}]")
        blog.close()

    duration_s = int(time.monotonic() - started)
    log_text = _text()
    counts = parse_test_log(log_text, exit_code)

    test_run.log = (log_text + f"\n[exit={exit_code} duration={duration_s}s]").strip()
    test_run.total = counts["total"]
    test_run.passed = counts["passed"]
    test_run.failed = counts["failed"]
    test_run.skipped = counts["skipped"]
    test_run.status = TestRunStatus.COMPLETED
    test_run.completed_at = utcnow()

    # Failures flow FORWARD: triage is the step that looks at them. Only a
    # legacy in-flight step outside the UAT pair is left untouched.
    if run.current_step in (PhaseBStep.TEST_GEN, PhaseBStep.TEST_EXEC):
        run.current_step = PhaseBStep.TRIAGE
    db.commit()

    logger.info(
        "UAT script finished: change=%s test_run=%s exit=%d total=%d passed=%d failed=%d",
        run.change_request_id, test_run.id, exit_code,
        counts["total"], counts["passed"], counts["failed"],
    )
    return test_run
