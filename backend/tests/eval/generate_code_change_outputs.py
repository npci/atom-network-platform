# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Slice 25b — Live-agent harness for the code-change eval gold set.

Loads `code_change_gold.jsonl`, drives `stream_code_change_turn` for
each case end-to-end (real LLM call, real Code RAG retrieval, real
impact analyzer if flagged on), accumulates the streamed `<<FILE:>>`-
delimited output to disk as `<case_id>.txt`, then prints a per-case
+ aggregate timing/length report.

The on-disk outputs are exactly what `run_code_change_eval.py --outputs
<dir>` expects, so the workflow is:

    # 1. Generate live agent outputs (costs real LLM tokens)
    cd backend && python -m tests.eval.generate_code_change_outputs \\
        --out /tmp/cc_outputs --change-request-id <uuid>

    # 2. Score against the gold set + refresh the baseline
    cd backend && python -m tests.eval.run_code_change_eval \\
        --outputs /tmp/cc_outputs --refresh-baseline

The harness needs:
  - `ANTHROPIC_API_KEY` (or whichever LLM provider settings expects)
  - A populated Code RAG index (otherwise retrieval returns empty
    context and the agent generates from prompt knowledge alone)
  - An optional `--change-request-id` of an existing change_request
    row; the agent reads the row's `id` for scoping. When omitted, the
    harness creates a synthetic stub row + cleans it up on exit.

Marked `@pytest.mark.eval` so it never runs under regular CI. Operator-
triggered only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

HERE = Path(__file__).resolve().parent
GOLD_PATH = HERE / "code_change_gold.jsonl"


# ──────────────────────────────────────────────────────────────────────────────
# Pure helpers (testable without the live agent)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class CaseRunStats:
    """Per-case timing + length stats for the harness report."""
    case_id:        str
    output_chars:   int = 0
    output_chunks:  int = 0
    elapsed_s:      float = 0.0
    parse_ok:       bool = False
    parsed_files:   int = 0
    error:          str | None = None

    def to_dict(self) -> dict:
        return {
            "case_id":       self.case_id,
            "output_chars":  self.output_chars,
            "output_chunks": self.output_chunks,
            "elapsed_s":     round(self.elapsed_s, 2),
            "parse_ok":      self.parse_ok,
            "parsed_files":  self.parsed_files,
            "error":         self.error,
        }


def load_gold_cases(gold_path: Path = GOLD_PATH) -> list[dict]:
    """Read JSONL → list of case dicts. Raises if file missing or malformed."""
    if not gold_path.exists():
        raise FileNotFoundError(f"gold set not found: {gold_path}")
    cases: list[dict] = []
    for ln, line in enumerate(gold_path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            cases.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise ValueError(f"{gold_path}:{ln} not valid JSON: {e}") from e
    return cases


def filter_cases(cases: list[dict], *, only_ids: Iterable[str] | None) -> list[dict]:
    """If `only_ids` is non-None, keep cases whose `id` matches.

    Filters silently — IDs in `only_ids` that don't exist in the gold set
    are reported via logger.warning but don't raise (allows
    `--cases foo,bar` to be an "include if present" filter).
    """
    if not only_ids:
        return cases
    wanted = {i.strip() for i in only_ids if i and i.strip()}
    if not wanted:
        return cases
    matched = [c for c in cases if c.get("id") in wanted]
    matched_ids = {c.get("id") for c in matched}
    missing = wanted - matched_ids
    if missing:
        logger.warning("requested case ids not found in gold set: %s", sorted(missing))
    return matched


def output_path_for(out_dir: Path, case_id: str) -> Path:
    """Filesystem path the runner expects: `<out_dir>/<case_id>.txt`."""
    if not case_id or "/" in case_id or "\\" in case_id:
        raise ValueError(f"unsafe case_id for output filename: {case_id!r}")
    return out_dir / f"{case_id}.txt"


# ──────────────────────────────────────────────────────────────────────────────
# Live-agent driver
# ──────────────────────────────────────────────────────────────────────────────

async def _run_one_case(
    case: dict,
    *,
    db,
    change_request_id: str,
    out_dir: Path,
    user_message: str = "Proceed with the implementation per the Tech Spec and BRD.",
) -> CaseRunStats:
    """Drive `stream_code_change_turn` for a single gold case.

    Streamed chunks are accumulated into a single string and written to
    `<out_dir>/<case_id>.txt`. Stats include timing + parse-success
    against the agent's own `parse_files_from_output`.
    """
    from app.agents.code_change import parse_files_from_output, stream_code_change_turn

    cid = case.get("id", "?")
    stats = CaseRunStats(case_id=cid)
    out_file = output_path_for(out_dir, cid)
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    full = ""
    chunks = 0
    try:
        async for chunk in stream_code_change_turn(
            db=db,
            change_request_id=change_request_id,
            tech_spec=case.get("tech_spec") or "",
            brd=case.get("brd") or "",
            conversation_history=[],
            new_user_message=user_message,
        ):
            full += chunk
            chunks += 1
        stats.elapsed_s = time.monotonic() - started
        stats.output_chars = len(full)
        stats.output_chunks = chunks
        out_file.write_text(full, encoding="utf-8")

        # Best-effort parse-success report (the scoring runner re-parses
        # via the same function, so this is a sanity preview).
        try:
            parsed = parse_files_from_output(full)
            stats.parsed_files = len(parsed)
            stats.parse_ok = bool(parsed)
        except Exception:
            stats.parse_ok = False
    except Exception as e:
        stats.elapsed_s = time.monotonic() - started
        stats.error = f"{type(e).__name__}: {e}"
        logger.warning("case %s failed: %s", cid, e)
        # Still write whatever we accumulated so partial output is
        # available for inspection.
        if full:
            try:
                out_file.write_text(full, encoding="utf-8")
            except Exception:
                pass

    return stats


def _ensure_change_request_row(db, change_request_id: str | None) -> tuple[str, bool]:
    """Resolve a usable change_request_id for the agent.

    Returns `(change_request_id, created_here)`. When `created_here` is
    True the harness should clean it up at the end.
    """
    from app.models.change_request import ChangeRequest, ChangeStatus
    from app.models.base import generate_uuid

    if change_request_id:
        row = db.get(ChangeRequest, change_request_id)
        if row is None:
            raise ValueError(f"change_request_id not found: {change_request_id}")
        return change_request_id, False

    from app.models.user import User

    # `created_by` is a NOT NULL FK to users.id, so the stub needs a real user.
    # Borrow any existing one rather than inventing an account.
    owner_id = db.query(User.id).limit(1).scalar()
    if owner_id is None:
        raise ValueError(
            "no users in the DB — the eval harness needs one to own its synthetic "
            "change request. Seed a user (scripts/create_user.py) and re-run."
        )

    new_id = generate_uuid()
    # ChangeStatus values are pipeline phases; pick a benign mid-pipeline
    # value so the synthetic row doesn't accidentally drive any status-
    # gated UI behaviour.
    stub = ChangeRequest(
        id=new_id,
        title="[eval-harness] code-change baseline",
        # NOT NULL. There is no `description` column — an older harness passed one
        # and every live run died in the ChangeRequest constructor.
        initial_prompt="Synthetic change-request created by Slice 25b harness; safe to delete.",
        status=ChangeStatus.TECH_SPEC,
        created_by=owner_id,
    )
    db.add(stub)
    db.commit()
    return new_id, True


def _delete_change_request_row(db, change_request_id: str) -> None:
    from app.models.change_request import ChangeRequest
    row = db.get(ChangeRequest, change_request_id)
    if row is not None:
        db.delete(row)
        db.commit()


# ──────────────────────────────────────────────────────────────────────────────
# Main entry
# ──────────────────────────────────────────────────────────────────────────────

async def _run_all_async(
    cases: list[dict],
    *,
    out_dir: Path,
    change_request_id: str | None,
    user_message: str,
) -> list[CaseRunStats]:
    """Open one DB session, ensure a change-request row exists, run every case."""
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        cr_id, created_here = _ensure_change_request_row(db, change_request_id)
        try:
            results: list[CaseRunStats] = []
            for case in cases:
                logger.info("running case %s (tech_spec=%d chars)",
                            case.get("id"), len(case.get("tech_spec") or ""))
                stats = await _run_one_case(
                    case, db=db, change_request_id=cr_id,
                    out_dir=out_dir, user_message=user_message,
                )
                results.append(stats)
                logger.info(
                    "  done case=%s elapsed=%.1fs chars=%d parsed_files=%d parse_ok=%s",
                    stats.case_id, stats.elapsed_s, stats.output_chars,
                    stats.parsed_files, stats.parse_ok,
                )
            return results
        finally:
            if created_here:
                try:
                    _delete_change_request_row(db, cr_id)
                except Exception as e:
                    logger.warning("could not delete synthetic change-request: %s", e)
    finally:
        db.close()


def run(
    *,
    out_dir: Path,
    only_ids: list[str] | None,
    change_request_id: str | None,
    user_message: str,
) -> int:
    cases = load_gold_cases()
    cases = filter_cases(cases, only_ids=only_ids)
    if not cases:
        logger.error("no cases to run after filtering")
        return 2

    logger.info("running %d gold case(s) → %s", len(cases), out_dir)
    results = asyncio.run(_run_all_async(
        cases, out_dir=out_dir, change_request_id=change_request_id,
        user_message=user_message,
    ))

    # Aggregate report
    total = len(results)
    parse_ok = sum(1 for r in results if r.parse_ok)
    errored = sum(1 for r in results if r.error)
    total_chars = sum(r.output_chars for r in results)
    total_elapsed = sum(r.elapsed_s for r in results)

    logger.info("")
    logger.info("=== harness report ===")
    logger.info("  cases:        %d", total)
    logger.info("  parse_ok:     %d / %d", parse_ok, total)
    logger.info("  errored:      %d", errored)
    logger.info("  total_chars:  %d", total_chars)
    logger.info("  total_elapsed: %.1fs", total_elapsed)
    logger.info("")
    logger.info("Outputs written to: %s", out_dir)
    logger.info("Score with: python -m tests.eval.run_code_change_eval --outputs %s --refresh-baseline",
                out_dir)

    return 0 if errored == 0 else 1


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Drive the code-change agent across the gold set; save outputs.",
    )
    ap.add_argument("--out", type=Path, required=True,
                    help="Directory to write <case_id>.txt files.")
    ap.add_argument("--cases", type=str, default=None,
                    help="Comma-separated case IDs to run (default: all).")
    ap.add_argument("--change-request-id", type=str, default=None,
                    help="Existing change_request UUID to scope Code RAG retrieval. "
                         "When omitted, a synthetic stub is created + cleaned up.")
    ap.add_argument("--user-message", type=str,
                    default="Proceed with the implementation per the Tech Spec and BRD.",
                    help="The new_user_message passed to the streaming agent.")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    only_ids = [s.strip() for s in (args.cases or "").split(",") if s.strip()] or None
    return run(
        out_dir=args.out,
        only_ids=only_ids,
        change_request_id=args.change_request_id,
        user_message=args.user_message,
    )


if __name__ == "__main__":
    raise SystemExit(main())
