# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Build-failure triager (THE BOOK §8 — verify tier).

Change-AWARE triage of a Maven reactor build, the piece the operator skip-pattern
gate (``AGENTIC_VERIFY_SKIP_MODULES`` in verification_plan) doesn't do: for each
compile error, which module owns it and did THIS change touch that module? The
deterministic core is the ground truth; a small LLM pass only RANKS and EXPLAINS
the untouched ones (legacy vs infra) and proposes remediation.

The one non-negotiable safeguard is enforced in CODE, not the prompt: a failure in
a module the change TOUCHED is always a RELATED_REGRESSION and is never suppressed
as "legacy noise" — the research is decisive that a precision-optimized noise
classifier silently drops ~76% of real regressions, which is unacceptable in a
payments build. Static scoping (what to look at) is safe; static suppression
(what to ignore) is not.

Pure helpers (parse_mvn_errors / module_name / annotate_failures) are deterministic
and unit-tested in isolation; ``triage_build_failures`` adds the advisory LLM layer.

STATUS (2026-07-23 feedback audit): ``triage_build_failures`` has NO callers — the
module is registered in llm_router/UI labels but never wired into a pipeline. Its
fail-open design currently protects nothing; wire it into verify-error enrichment
or delete it.
"""
from __future__ import annotations
from app.core.domain.registry import prompt_block
from app.core.prompts import render_prompt

import fnmatch
import json
import logging
import re
from dataclasses import dataclass

from app.core.llm import call_llm
from app.core.json_recovery import parse_llm_json

logger = logging.getLogger(__name__)

# mvn --fail-at-end javac error line, e.g.
#   [ERROR] /app/clone/iupi/src/main/java/Foo.java:[42,10] cannot find symbol
_MVN_ERROR_RE = re.compile(
    r"\[ERROR\]\s+(?P<file>/?[\w.\-/]+\.java):\[(?P<line>\d+)[,:](?P<col>\d+)\]\s*(?P<msg>.*)")

_VALID = {"RELATED_REGRESSION", "UNRELATED_LEGACY", "INFRA", "UNCLASSIFIED"}

# Identity nouns come from the active domain pack; under the default UPI pack
# this renders byte-identically to the previous hardcoded file.
SYSTEM_PROMPT = render_prompt(
    "agents/build_triager/system_prompt.md",
    AUTHORITY=prompt_block("authority", "ecosystem"),
    DOMAIN_LABEL=prompt_block("domain_name", "platform"),
)


@dataclass
class BuildError:
    file: str
    line: int
    message: str
    module: str | None = None
    touched: bool = False        # change touched this module → real regression
    skip_listed: bool = False    # module matches an operator skip pattern


def parse_mvn_errors(log: str) -> list[BuildError]:
    """Extract javac compile errors from a Maven --fail-at-end log. Deterministic;
    returns [] when nothing parses (a non-compile failure is handled elsewhere)."""
    seen: set[tuple[str, int, str]] = set()
    out: list[BuildError] = []
    for m in _MVN_ERROR_RE.finditer(log or ""):
        key = (m.group("file"), int(m.group("line")), m.group("msg").strip())
        if key in seen:
            continue
        seen.add(key)
        out.append(BuildError(file=key[0], line=key[1], message=key[2]))
    return out


def module_name(path: str) -> str:
    """The Maven module a path belongs to, by convention ``<module>/src/...``. Returns
    the module DIRECTORY NAME so it is stable whether the path is absolute
    (``/app/clone/iupi/src/...``) or repo-relative (``iupi/src/...``) — both → ``iupi``."""
    p = (path or "").replace("\\", "/")
    root = p.split("/src/")[0] if "/src/" in p else p.rsplit("/", 1)[0]
    return root.rsplit("/", 1)[-1] or root


def annotate_failures(errors: list[BuildError], changed_files: list[str],
                      skip_patterns: list[str] | None = None) -> list[BuildError]:
    """Tag each error with its module, whether the change touched that module, and
    whether the module is operator-skip-listed. Pure + deterministic."""
    touched = {module_name(f) for f in (changed_files or [])}
    patterns = skip_patterns or []
    for e in errors:
        e.module = module_name(e.file)
        e.touched = e.module in touched
        e.skip_listed = any(fnmatch.fnmatch(e.file, p) or fnmatch.fnmatch(e.module or "", p) for p in patterns)
    return errors


async def triage_build_failures(build_log: str, changed_files: list[str],
                                skip_patterns: list[str] | None = None) -> dict:
    """Triage a failed Maven build. Returns a structured report: every failure tagged
    with module + classification + remediation, with change-touched failures forced to
    RELATED_REGRESSION (never suppressed). Fail-open — LLM/parse failure still returns
    the deterministic static classification."""
    errors = parse_mvn_errors(build_log)
    if not errors:
        return {"failures": [], "related_count": 0, "total": 0,
                "summary": "no javac compile errors parsed — likely an infra/non-compile failure"}
    annotate_failures(errors, changed_files, skip_patterns)

    payload = [{"file": e.file, "line": e.line, "message": e.message[:300],
                "module": e.module, "touched_by_change": e.touched, "skip_listed": e.skip_listed}
               for e in errors]
    logger.info("build_triager — triaging %d compile error(s), %d in touched modules",
                len(errors), sum(1 for e in errors if e.touched))
    try:
        raw = await call_llm(
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "Triage these build errors:\n\n"
                       + json.dumps(payload, indent=2, default=str)}],
            max_tokens=2000, agent_name="build_triager")
        llm = await parse_llm_json(raw, expect_array=True, fallback=[])
    except Exception as exc:                       # noqa: BLE001 — fail-open to static-only
        logger.warning("build_triager LLM pass failed, using static classification: %s", exc)
        llm = []

    by_file: dict[str, dict] = {}
    if isinstance(llm, list):
        for item in llm:
            if isinstance(item, dict) and item.get("file"):
                by_file[item["file"]] = item

    failures = []
    for e in errors:
        info = by_file.get(e.file, {})
        # SAFEGUARD (in code, not prompt): a touched-module failure is ALWAYS a real
        # regression — the LLM cannot downgrade it. Only untouched failures take the
        # LLM's legacy/infra call; anything unrecognised stays UNCLASSIFIED (surfaced).
        if e.touched:
            classification = "RELATED_REGRESSION"
        else:
            classification = info.get("classification") or "UNCLASSIFIED"
            if classification not in _VALID:
                classification = "UNCLASSIFIED"
        failures.append({
            "file": e.file, "line": e.line, "module": e.module, "message": e.message,
            "touched_by_change": e.touched, "skip_listed": e.skip_listed,
            "classification": classification,
            "reasoning": (info.get("reasoning") or "")[:500],
            "remediation": (info.get("remediation") or "")[:500],
        })

    related = [f for f in failures if f["classification"] == "RELATED_REGRESSION"]
    return {
        "failures": failures,
        "related_count": len(related),
        "total": len(failures),
        "safeguard": "failures in change-touched modules are never suppressed as legacy noise",
    }
