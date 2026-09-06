# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Precert engine — drives the real Nfinite simulators from inside the platform.

Provisions a bank into precertdb, runs each certification case against the live precert
switch (precert -> precert-bank-sim -> precertdb.upihosttxnlog), and applies a pure pass/fail
rule to the response code the bank's switch actually returned.

Named `precert_engine`, NOT `cert_engine`: this codebase already uses "cert_engine" to mean
the registered cert-engine PartnerAgent peer (`find_cert_engine_partner`, the `cert_engine_*`
settings, migration 0034). This module is a different thing — an in-process driver for the
external Nfinite precert stack. It does not replace that peer, nor the `certagent/`
cert-agent <-> bank-agent services.

Deliberately transport- and framework-agnostic: nothing here imports FastAPI, the ORM, or
a2a-sdk. Callers (cert_orchestrator, the cert handlers) call INTO this module; it never calls
back out. That keeps the certification logic identical whether it is exercised from a unit
test, a one-shot script, or a live A2A message.

precertdb is Postgres, reached with psycopg2 — already a platform dependency, so this adds no
new driver.
"""
from .connector import (
    CaseResult,
    CaseSpec,
    NfiniteConnector,
    SimulatorRejected,
    Unverifiable,
)
from .provisioning import BankConfig, NfiniteProvisioner
from .runner import run_certification
from .state_machine import (
    IllegalTransition,
    Phase,
    Trigger,
    is_terminal,
    next_phase,
)
from .verdict import (
    Expectation,
    ResponseOutcome,
    ResultStatus,
    Verdict,
    decide,
    normalized_code,
)

__all__ = [
    "Expectation", "ResponseOutcome", "ResultStatus", "Verdict", "decide", "normalized_code",
    "Phase", "Trigger", "next_phase", "is_terminal", "IllegalTransition",
    "NfiniteConnector", "CaseSpec", "CaseResult", "Unverifiable", "SimulatorRejected",
    "NfiniteProvisioner", "BankConfig",
    "run_certification",
]
