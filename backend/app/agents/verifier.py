# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Pluggable verification backends (THE BOOK §9 / dependency-decoupling).

Verification answers one question — *"does the CHANGED target code still build?"* —
but the build toolchain must **never** be a hard dependency of the platform. So the
backend is chosen at runtime from what is actually installed:

* :class:`LocalToolchainVerifier` — runs the deterministic VerificationPlan
  (``mvn``/``javac``) in the worker. Used only when the toolchain is present.
* :class:`DeferredVerifier` — no local toolchain: returns ``status="unverified"`` so
  the run completes to human approval **flagged for CI**, instead of crashing. A hook
  is left here for a future CI-triggering backend.

The selected backend's :class:`~app.agents.verification_plan.VerificationOutcome` is
the AUTHORITATIVE verdict; the in-loop ``verify_change`` tool is diagnostic only.
"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from app.agents import toolchain_report, verification_plan
from app.agents.verification_plan import VerificationOutcome
from app.core.config import settings

logger = logging.getLogger("app.agentic")


@runtime_checkable
class Verifier(Protocol):
    name: str

    def verify(self, db, run_id: str, change_set, *, app_blast_radius: bool = True) -> VerificationOutcome:
        """Verify ``change_set`` (an object exposing ``.operations``) for ``run_id``.

        ``app_blast_radius`` (default True) → on a schema/core change, full-build the
        in-scope app repos so a broken consumer fails the gate. Phase A passes False
        (compile schema + install core to ~/.m2 only; app rebuild is Phase B's job)."""
        ...


class LocalToolchainVerifier:
    """Authoritative local build: deterministic plan executed with the worker's
    mvn/javac. Verdict comes from real exit codes (never the model's say-so)."""

    name = "local"

    def verify(self, db, run_id: str, change_set, *, app_blast_radius: bool = True) -> VerificationOutcome:
        plan, touched = verification_plan.build_plan(db, run_id, change_set,
                                                     app_blast_radius=app_blast_radius)
        if not plan:
            # Nothing buildable changed (e.g. docs only) — there's nothing to fail.
            return VerificationOutcome(status="verified", gates={}, plan=[])
        # 3c — append scoped test steps (RUN the change's own test classes) so a green verdict means
        # the behaviour RAN, not just compiled. Gated (agentic_run_feature_tests) + no-op when the
        # change owns no tests. A failure → required_tests (loops back); infra → environment (fail-open).
        verification_plan.append_feature_test_steps(plan, run_id, change_set)
        _ft = None
        if getattr(settings, "agentic_require_feature_tests", True):   # WS3a — require a real feature test
            _ok, _why = verification_plan.feature_test_gate(change_set)
            _ft = _ok
            if not _ok:
                logger.info("verify: run=%s feature_tests gate FAILED — %s", run_id, _why)
        return verification_plan.run_plan(db, run_id, plan, touched_modules=touched, feature_tests_ok=_ft)


class DeferredVerifier:
    """No local toolchain: don't fail, don't pretend — defer to CI. The run reaches
    human approval flagged ``unverified`` so a human/CI confirms the build."""

    name = "deferred"

    def __init__(self, reason: str = "no local build toolchain — deferred to CI"):
        self._reason = reason

    def verify(self, db, run_id: str, change_set, *, app_blast_radius: bool = True) -> VerificationOutcome:
        return VerificationOutcome(status="unverified", gates={}, reason=self._reason)


def select_verifier(report=None, mode: str | None = None) -> Verifier:
    """Pick a backend from config + the live toolchain. Pure selection (cheap):

    * ``mode="local"`` → force local (caller asserts the toolchain exists);
    * ``mode in {"ci","off"}`` → always defer;
    * ``mode="auto"`` (default) → local iff ``mvn``+``javac`` are present, else defer.
    """
    from app.core.config import settings

    mode = (mode or settings.agentic_verifier or "auto").lower()
    if mode == "local":
        return LocalToolchainVerifier()
    if mode in ("ci", "off"):
        return DeferredVerifier(f"verifier mode '{mode}' — build deferred to CI")
    # auto
    report = report or toolchain_report.build_toolchain_report()
    if report.build_ready:
        return LocalToolchainVerifier()
    return DeferredVerifier("build toolchain (mvn/javac) not present — deferred to CI")
