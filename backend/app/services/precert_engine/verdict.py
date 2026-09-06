# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Re-import shim — the verdict rule's canonical home is now
`app.services.cert_agent.verdict` (COMBINED_EXECUTION_PLAN item 0.2).

Pure and domain-portable (expected outcome vs observed outcome); it was only
ever housed here because the precert engine was its first caller. The shim
keeps `runner.py`/`connector.py`'s relative imports and the precert tests on
the SAME objects — identity, not copies.
"""
from app.services.cert_agent.verdict import (
    Expectation,
    ResponseOutcome,
    ResultStatus,
    Verdict,
    decide,
    normalized_code,
)

# Declares the shim's public surface, and tells linters these re-exports are
# deliberate — an explicit export list rather than a per-import suppression.
__all__ = [
    "Expectation", "ResponseOutcome", "ResultStatus", "Verdict", "decide",
    "normalized_code",
]
