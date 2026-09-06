# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Re-import shim — the harness machinery moved to `app/services/cert_harnesses`.

This module used to define the three harnesses and their selector. That was
re-examined on the genericisation ruling (2026-09-02): the registry and
`SimPackHarness` are platform machinery with no domain term in them, and
housing them in this domain's pack made the only domain-neutral certification
engine unreachable for every other domain — the registry no longer loads
Python pack classes, so nothing could reach this module at runtime.

`NetworkPack.certification()` and the existing tests import from here; the shim
keeps both working, following the same pattern as the `precert_engine` →
`cert_agent` relocation. The two genuinely domain-bound harnesses (cert_agent,
precert) keep their identity — only their home moved, to sit beside
`cert_orchestrator`, where their real work has always lived.
"""
from app.services.cert_harnesses import (  # noqa: F401
    CertAgentHarness,
    PrecertEngineHarness,
    SimPackHarness,
    _to_result,
    default_harness,
    harness_by_key,
)

__all__ = [
    "CertAgentHarness", "PrecertEngineHarness", "SimPackHarness",
    "harness_by_key", "default_harness", "_to_result",
]
