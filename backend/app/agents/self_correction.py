# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Self-correction loop — orchestrator (Slice 15).

Plan §7.4: "Compile / type-check in the sandbox. If it fails, feed errors
back to the editor agent for ≤ N self-correction iterations." Works with
the Slice 14 sandbox + any LLM-based fix generator.

Design:
  - The orchestrator is pure: `self_correct(initial_code, generate_fix,
    run_sandbox, max_iterations=3)` takes two callables and never touches
    Docker, the network, or a real LLM. Fully unit-testable.
  - `generate_fix_via_llm` is a convenience default: uses `app.core.llm.call_llm`
    with a fix-focused prompt. Caller can swap in any async (current_code,
    stderr) → new_code callable (e.g. for a local model, or a no-op mock).
  - Sandbox is passed as a sync callable: `(files) -> SandboxResult`. In
    production this wraps `app.services.sandbox.run_in_sandbox(files, cmd)`.

Never raises — infra failures are captured in `SelfCorrectionResult`.
"""
from __future__ import annotations
from app.core.prompts import load_prompt

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SelfCorrectionResult:
    """Outcome of a full self-correction run."""
    final_code: dict[str, str]           # files state at last attempt
    iterations: int                      # 0 = clean on first compile
    success: bool                        # True iff sandbox returned exit_code=0
    final_stderr: str                    # only populated on failure
    attempts: list[dict] = field(default_factory=list)   # per-iteration log


# Type aliases for readability
_SandboxFn = Callable[[dict[str, str]], object]    # returns SandboxResult-shaped
_FixFn     = Callable[[dict[str, str], str], Awaitable[dict[str, str]]]


async def self_correct(
    initial_code: dict[str, str],
    *,
    generate_fix: _FixFn,
    run_sandbox: _SandboxFn,
    max_iterations: int = 3,
) -> SelfCorrectionResult:
    """Run compile → fix → compile … up to `max_iterations` times.

    Args:
        initial_code: Files dict produced by the editor (path → content).
        generate_fix: async callable (current_code, error_stderr) → fixed_code.
                      Should return a FULL files dict for the next attempt, not
                      a diff. Fail-open: if the fix generator returns an empty
                      dict, the loop stops.
        run_sandbox: sync callable (files) → SandboxResult-shaped object with
                     `.exit_code`, `.stdout`, `.stderr` attributes.
        max_iterations: hard cap on FIX attempts. Total sandbox invocations is
                        `max_iterations + 1` (one initial + up to N fixes).

    Returns:
        SelfCorrectionResult. Never raises.
    """
    attempts: list[dict] = []
    code = dict(initial_code)     # shallow copy so we don't mutate caller's dict

    for iteration in range(max_iterations + 1):
        try:
            sandbox_result = run_sandbox(code)
        except Exception as e:
            logger.error("self_correct: sandbox call raised at iter %d: %s", iteration, e)
            attempts.append({
                "iteration": iteration, "exit_code": -1,
                "stdout_excerpt": "", "stderr_excerpt": f"sandbox raised: {e}",
            })
            return SelfCorrectionResult(
                final_code=code, iterations=iteration, success=False,
                final_stderr=f"sandbox raised: {e}", attempts=attempts,
            )

        exit_code = getattr(sandbox_result, "exit_code", -1)
        stdout    = getattr(sandbox_result, "stdout", "") or ""
        stderr    = getattr(sandbox_result, "stderr", "") or ""

        attempts.append({
            "iteration":      iteration,
            "exit_code":      exit_code,
            "stdout_excerpt": stdout[-500:],
            "stderr_excerpt": stderr[-500:],
        })

        if exit_code == 0:
            return SelfCorrectionResult(
                final_code=code, iterations=iteration,
                success=True, final_stderr="", attempts=attempts,
            )

        # Cap reached after initial attempt + max_iterations fix attempts.
        if iteration >= max_iterations:
            return SelfCorrectionResult(
                final_code=code, iterations=iteration,
                success=False, final_stderr=stderr, attempts=attempts,
            )

        # Try a fix.
        try:
            fixed = await generate_fix(code, stderr)
        except Exception as e:
            logger.warning("self_correct: generate_fix raised at iter %d: %s", iteration, e)
            return SelfCorrectionResult(
                final_code=code, iterations=iteration, success=False,
                final_stderr=stderr + f"\n[fix generation raised: {e}]",
                attempts=attempts,
            )

        if not isinstance(fixed, dict) or not fixed:
            # Fix generator fail-open: empty dict signals "give up".
            logger.info("self_correct: generator returned empty fix; stopping")
            return SelfCorrectionResult(
                final_code=code, iterations=iteration, success=False,
                final_stderr=stderr + "\n[fix generator returned empty]",
                attempts=attempts,
            )
        code = fixed

    # Belt-and-braces fallthrough (unreachable given the loop guards).
    return SelfCorrectionResult(
        final_code=code, iterations=max_iterations,
        success=False, final_stderr="loop exited unexpectedly", attempts=attempts,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Default LLM-based fix generator
# ──────────────────────────────────────────────────────────────────────────────

_FIX_SYSTEM_PROMPT = load_prompt("agents/self_correction/fix_system_prompt.md")

_FIX_MAX_TOKENS = 4000


async def generate_fix_via_llm(current_code: dict[str, str], error_stderr: str) -> dict[str, str]:
    """Default LLM-based fix generator suitable for `self_correct`'s `generate_fix`.

    Returns a NEW {path: content} dict merging `current_code` with the LLM's
    suggested replacements. Empty dict on any failure (matches the orchestrator's
    fail-open contract).
    """
    if not error_stderr or not current_code:
        return {}

    # Build the LLM payload — current files (truncated per-file to keep the prompt
    # bounded) plus the stderr with BOTH ends kept: Maven puts the actionable mojo
    # failure at the END behind a long reactor preamble, javac errors at the START —
    # a head-only slice fed the fixer pure noise (the same clipped-error class as the
    # §5 verify-gate gotcha).
    import json as _json

    err = error_stderr if len(error_stderr) <= 6000 else (
        error_stderr[:2000] + "\n…[middle truncated]…\n" + error_stderr[-4000:])
    files_payload = {
        path: (content[:6000] + ("\n// ...truncated..." if len(content) > 6000 else ""))
        for path, content in current_code.items()
    }
    # Bound the payload by omitting WHOLE files largest-first — never by slicing the
    # serialized JSON, which handed the model a string cut mid-structure.
    payload_json = _json.dumps(files_payload, indent=2)
    for path in sorted(files_payload, key=lambda p: len(files_payload[p]), reverse=True):
        if len(payload_json) <= 12000:
            break
        files_payload[path] = "// [omitted for size — request unchanged unless named in the error]"
        payload_json = _json.dumps(files_payload, indent=2)
    user_payload = (
        f"COMPILER STDERR:\n```\n{err}\n```\n\n"
        f"CURRENT FILES:\n```json\n{payload_json}\n```\n\n"
        f"Return the JSON with corrected files."
    )

    try:
        from app.core.llm import call_llm
        from app.core.json_recovery import parse_llm_json

        raw = await call_llm(
            system=_FIX_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_payload}],
            max_tokens=_FIX_MAX_TOKENS,
            agent_name="self_correction",
        )
    except Exception as e:
        logger.warning("generate_fix_via_llm: LLM call failed: %s", e)
        return {}

    parsed = await parse_llm_json(raw, fallback=None, llm_self_correct=False)
    if not isinstance(parsed, dict):
        return {}

    changes = parsed.get("files")
    if not isinstance(changes, dict):
        return {}

    # Normalise + merge: keep all original files, overwrite changed ones.
    fixed: dict[str, str] = dict(current_code)
    for path, content in changes.items():
        if not isinstance(path, str) or not path.strip():
            continue
        if "../" in path or path.startswith("/"):
            # Orchestrator doesn't validate paths itself; filter obvious
            # unsafe entries here so the sandbox doesn't reject the whole batch.
            continue
        if not isinstance(content, str):
            continue
        fixed[path] = content

    # If nothing actually changed, signal "give up" to the orchestrator.
    if fixed == current_code:
        return {}

    return fixed
