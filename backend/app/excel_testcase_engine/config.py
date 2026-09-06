# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

# INTEGRATION: Engine-local config shim.
#
# WHY: the standalone build had a YAML-driven `config/` package that exposed
# `PACKAGE_ROOT` and `load_runtime_config()` to the agents. We don't want
# YAML in the host project — the host has its own settings (Pydantic
# BaseSettings) at `app.core.config.settings`. Instead this small shim
# exposes the two things the engine actually uses:
#
#   1. PACKAGE_ROOT — the engine package directory, used by:
#        - prompts loader (reads markdown from prompts/)
#        - npci_specs loader (reads JSON/JSONL artifacts)
#
#   2. load_runtime_config() — concurrency knobs for the writer fan-out and
#      validator fan-in. We pull these from the host settings when defined,
#      otherwise fall back to engine defaults proven in the standalone tests.
#
# WHY a shim rather than a full re-implementation: the engine's writer and
# validator only need a handful of integer knobs. Anything richer can be
# added later as a `settings.engine_*` attribute on the host BaseSettings
# without changing engine code — the shim is the indirection point.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# WHY hard path: this file lives at <pkg>/config.py, so parent is the package
# root the engine reads its prompts and npci_specs from.
PACKAGE_ROOT: Path = Path(__file__).resolve().parent


@dataclass
class WriterRuntimeConfig:
    """Concurrency + batch sizing for writer fan-out."""

    max_concurrent_batches: int = 8
    # cases_per_batch reduced 20 → 5 (2026-05-06).
    # AiNxt + Sonnet 4.6 was returning 0–1 cases when asked to expand a batch
    # of 19–20 stubs in a single call (writer.size_mismatch loop, log
    # 2026-05-06 06:23–06:25). Smaller batches let the LLM produce ALL cases
    # reliably; total wall-time is comparable since batches now run in
    # parallel via max_concurrent_batches=8. Override per-deployment via
    # `engine_writer_cases_per_batch` if needed.
    cases_per_batch: int = 5


@dataclass
class ValidatorRuntimeConfig:
    """Concurrency + switches for the validator's per-sheet semantic pass."""

    max_concurrent_sheets: int = 6
    enable_xsd_check: bool = True


@dataclass
class RetryRuntimeConfig:
    """Tenacity retry knobs (kept for compat — engine uses these in retry.py)."""

    max_attempts: int = 3
    exponential_min_seconds: int = 2
    exponential_max_seconds: int = 30


@dataclass
class RuntimeConfig:
    """Top-level runtime config the engine asks for."""

    writer: WriterRuntimeConfig
    validator: ValidatorRuntimeConfig
    retry: RetryRuntimeConfig
    # Cap on total test cases across the workbook. Enforced in the planner
    # orchestration via a per-sheet hard cap (=cap//role_sheet_count) injected
    # into the stubs prompt + a deterministic trim. Set to None or 0 to
    # disable (engine reverts to coverage-matrix-driven count). Override via
    # `engine_test_case_cap` host setting.
    test_case_cap: int = 25


_cached: RuntimeConfig | None = None


def load_runtime_config() -> RuntimeConfig:
    """Return engine runtime config, optionally overridden by host settings.

    Reading from `app.core.config.settings` is best-effort: when the host
    doesn't expose `engine_writer_concurrency` etc., we fall back to engine
    defaults. That keeps the integration backwards-compatible — the host
    operator can tune these later without an engine code change.
    """

    global _cached
    if _cached is not None:
        return _cached

    writer_max = 8
    cases_per_batch = 5  # See WriterRuntimeConfig docstring (was 20).
    # WHY 6 (not 20): with provider proxies like AiNxt that enforce a
    # request-timeout (~30s) on each LLM call, a 20-stub batch produces a
    # 40K+ char input prompt and drives the model long enough to trip the
    # gateway, which then returns the placeholder string `Error generating
    # response`. Six stubs/batch keeps each call comfortably under that
    # window. Operator can override via `engine_writer_cases_per_batch`.
    validator_max = 6
    enable_xsd = True
    retry_attempts = 3
    test_case_cap = 25

    try:
        # Late import — the host settings module is heavy.
        from app.core.config import settings  # noqa: WPS433
        # WHY getattr-with-default: every host setting is optional. Adding
        # them later (e.g. `engine_writer_max_concurrent_batches: int = 8`)
        # picks up automatically with no engine change.
        writer_max = int(getattr(settings, "engine_writer_max_concurrent_batches", writer_max))
        cases_per_batch = int(getattr(settings, "engine_writer_cases_per_batch", cases_per_batch))
        validator_max = int(getattr(settings, "engine_validator_max_concurrent_sheets", validator_max))
        enable_xsd = bool(getattr(settings, "engine_validator_enable_xsd_check", enable_xsd))
        retry_attempts = int(getattr(settings, "engine_retry_max_attempts", retry_attempts))
        test_case_cap = int(getattr(settings, "engine_test_case_cap", test_case_cap))
    except Exception:
        # Pure defensiveness — never fail engine import on settings drift.
        pass

    _cached = RuntimeConfig(
        writer=WriterRuntimeConfig(
            max_concurrent_batches=writer_max,
            cases_per_batch=cases_per_batch,
        ),
        validator=ValidatorRuntimeConfig(
            max_concurrent_sheets=validator_max,
            enable_xsd_check=enable_xsd,
        ),
        retry=RetryRuntimeConfig(max_attempts=retry_attempts),
        test_case_cap=test_case_cap,
    )
    return _cached


__all__ = ["PACKAGE_ROOT", "RuntimeConfig", "load_runtime_config"]
