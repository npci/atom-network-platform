# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The platform's certification harnesses — named engines, selected by config.

Moved here from `app/packs/network/certification.py` (which remains as a re-import
shim) on the genericisation ruling: the harness REGISTRY is platform machinery,
not domain content. `SimPackHarness` in particular contains no domain term at
all — it opens a session and calls `cert_pack_run.run_round`, which derives
everything domain-specific from the API registry and the active pack's
vocabulary. Keeping it inside the UPI pack made the platform's only
domain-neutral certification engine unreachable for every other domain.

`CertAgentHarness` and `PrecertEngineHarness` stay genuinely UPI-bound
IMPLEMENTATIONS (bank_id, the Nfinite precert stack) — but the registry that
NAMES them is neutral, exactly like `services/cert_orchestrator` where their
real work already lives.

How a domain gets one of these:

* a Python pack declares `certification()` returning a harness (NetworkPack does);
* a CONFIG pack names one with `certification_harness: <key>` in its YAML —
  the config supplies the NAME of platform-registered behaviour, never the
  behaviour itself, which is what keeps a YAML pack a data file;
* omission still means the domain has no certification body, and
  `certification_dispatch` skips — absence stays a true statement.
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.domain.contract import CertResult

logger = logging.getLogger(__name__)

__all__ = [
    "CertAgentHarness", "PrecertEngineHarness", "SimPackHarness",
    "harness_by_key", "default_harness", "_to_result",
]


def _to_result(summary: dict[str, Any]) -> CertResult:
    """Map an orchestrator summary onto the contract type.

    `passed` stays None unless the summary actually says. A run that has been
    dispatched but not adjudicated is not a failure, and reporting it as one
    would flip an assignment to a verdict nobody reached.
    """
    summary = summary or {}
    passed = summary.get("passed")
    if passed is None:
        status = str(summary.get("status") or "").lower()
        if status in {"certified", "passed", "pass"}:
            passed = True
        elif status in {"failed", "fail"}:
            passed = False
    return CertResult(
        passed=passed,
        run_id=summary.get("run_id") or summary.get("cflow_id"),
        report_uri=summary.get("report_uri"),
        details=summary,
    )


class CertAgentHarness:
    """Certification via the cert-agent service (REST). UPI-bound in practice:
    the orchestrator it drives requires `partner.cert_agent_bank_id`."""

    key = "cert_agent"

    async def run(self, *, change_id: str, partner_id: str, role: str,
                  test_data: dict[str, Any],
                  test_data_per_case: dict[str, Any] | None = None,
                  dispatch_meta: dict[str, Any] | None = None) -> CertResult:
        # Imported here, not at module load: cert_orchestrator pulls in httpx,
        # the A2A stack and the ORM, and the registry resolves packs during
        # module import of several agents.
        from app.services.cert_orchestrator import orchestrate_cert_run

        summary = await orchestrate_cert_run(
            change_id, partner_id, role, test_data, test_data_per_case or {},
            dispatch_meta=dispatch_meta,
        )
        return _to_result(summary)


class PrecertEngineHarness:
    """Certification via the in-process precert engine.

    Drives the real precert → precert-bank-sim → precertdb path, with the whole
    exchange carried as a signed A2A conversation with the bank. UPI-bound.
    """

    key = "precert"

    async def run(self, *, change_id: str, partner_id: str, role: str,
                  test_data: dict[str, Any],
                  test_data_per_case: dict[str, Any] | None = None,
                  dispatch_meta: dict[str, Any] | None = None) -> CertResult:
        from app.services.cert_orchestrator import orchestrate_cert_run_precert_engine

        summary = await orchestrate_cert_run_precert_engine(
            change_id, partner_id, role, test_data, test_data_per_case or {},
            dispatch_meta=dispatch_meta,
        )
        return _to_result(summary)


class SimPackHarness:
    """Certification via the built-in pack-driven simulator (SIM-6).

    Greenfield (decision 2026-08-31): executes the round's request variants
    in-process against `services/simulator/runtime.handle` — exactly what a
    partner's stack hits over HTTP — and grades with `cert_assertions`.
    Every result records case, variant, mode and the pack that graded it.
    Domain-neutral: scope, templates and assertions all derive from the API
    registry and the published packs, never from code-resident vocabulary.
    """

    key = "sim_pack"

    async def run(self, *, change_id: str, partner_id: str, role: str,
                  test_data: dict[str, Any],
                  test_data_per_case: dict[str, Any] | None = None,
                  dispatch_meta: dict[str, Any] | None = None) -> CertResult:
        from app.core.database import SessionLocal
        from app.services.cert_pack_run import run_round

        db = SessionLocal()
        try:
            summary = await run_round(
                db, change_id=change_id, partner_id=partner_id, role=role,
                test_data=test_data, test_data_per_case=test_data_per_case,
                dispatch_meta=dispatch_meta)
        finally:
            db.close()
        return _to_result(summary)


_BY_KEY = {
    "sim_pack": SimPackHarness,
    "precert": PrecertEngineHarness,
    "cert_agent": CertAgentHarness,
}


def harness_by_key(key: str):
    """One named harness — the per-change/per-partner choice (S-6).

    An unknown key RAISES. Falling back to the default would certify through
    a different engine than the dispatch asked for, and the run would record
    the engine that actually ran while the operator believes another did.
    """
    try:
        return _BY_KEY[key]()
    except KeyError:
        raise ValueError(
            f"unknown certification harness {key!r} — "
            f"known: {', '.join(sorted(_BY_KEY))}")


def default_harness():
    """The harness this deployment is configured for.

    `cert_harness="sim_pack"` (declared setting, SIM-6) selects the
    pack-driven simulator — the harness AXIS. Empty preserves the legacy
    selector exactly: `precert_engine_enabled` picks the engine, otherwise
    cert-agent. The choice is a named object, never a branch in the
    orchestrator (`test_certification_dispatch.py` pins that).
    """
    from app.core.config import settings

    if settings.cert_harness == "sim_pack":
        return SimPackHarness()
    if getattr(settings, "precert_engine_enabled", False):
        return PrecertEngineHarness()
    return CertAgentHarness()
