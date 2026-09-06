# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Re-import shim — the state machine's canonical home is now
`app.services.cert_agent.state_machine` (COMBINED_EXECUTION_PLAN item 0.2).

The lifecycle was never precert-specific despite this package's name; it is
the certification lifecycle every harness drives, and C-0 persists it as
`cert_flow_states`. The shim keeps every existing import path — including this
package's `__init__` re-exports and the precert tests — pointing at the SAME
objects (identity, not copies), so `Phase` comparisons and `TRANSITIONS`
lookups cannot fork.
"""
from app.services.cert_agent.state_machine import (
    TERMINAL,
    TRANSITIONS,
    IllegalTransition,
    Phase,
    Trigger,
    is_terminal,
    next_phase,
)

# Declares the shim's public surface, and tells linters these re-exports are
# deliberate — an explicit export list rather than a per-import suppression.
__all__ = [
    "TERMINAL", "TRANSITIONS", "IllegalTransition", "Phase", "Trigger",
    "is_terminal", "next_phase",
]
