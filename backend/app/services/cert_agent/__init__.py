# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Spec-conformant A2A certification-lifecycle layer (protocol Part B).

Payload builders (``tasks.py``), the lifecycle driver (``flow.py`` +
``flow_store.py``), and — since COMBINED_EXECUTION_PLAN item 0.2 — the
canonical homes of the certification STATE MACHINE (``state_machine.py``) and
the pass/fail VERDICT rule (``verdict.py``). Both moved here from
``precert_engine/``, whose modules are now re-import shims: the lifecycle was
never precert-specific, C-0 persists it as ``cert_flow_states``, and the
precert package (retired at SIM-8) must depend on this one, not the reverse.
``tests/services/test_cert_agent_tasks.py`` pins the enum↔``overall_state``
relationship; ``test_precert_engine_shims.py`` pins shim identity.

IMPORT DISCIPLINE (circular-import guard): this ``__init__`` may import
``tasks`` only — never ``flow``, ``setup``, ``flow_store``, ``state_machine``
or ``verdict``. ``cert_agent/setup.py`` reaches ``precert_engine.provisioning``,
whose package ``__init__`` imports ``connector`` → the ``.verdict`` shim →
``cert_agent.verdict`` → THIS module; growing imports here closes that loop.
"""
from app.services.cert_agent.tasks import (  # noqa: F401
    ALL_CERT_TASKS,
    PARTNER_TO_AUTHORITY,
    EITHER,
    AUTHORITY_TO_PARTNER,
    OVERALL_STATES,
)
