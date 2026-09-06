# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Shared A2A SDK plumbing for the Authority ↔ Partner ↔ Cert-agent triangle.

Wraps `a2a-sdk` (Google's official A2A SDK) so each backend mounts a
JSON-RPC endpoint, advertises an AgentCard, and dispatches Tasks via a
common AgentExecutor pattern. Cert-agent is the reference implementation
(see `certagent/cert-agent/app/a2a/`); this package generalises that
plumbing so the platform backend and the partner platform backend can
adopt it without re-implementing the SDK glue.

Slices landed:
    * mount.py        — generic `build_a2a_components(card, executor)`
    * client.py       — generic outbound `send_a2a_message(...)`
    * auth.py         — Bearer JWT fetch + per-partner cache
    * task_store_db   — `get_task_store(database_url)` factory wrapping
                        the SDK's `DatabaseTaskStore` (Slice 2)
    * authority_card       — `AUTHORITY_AGENT_CARD` describing the platform's
                        receivable task types (Slice 3)
    * authority_executor   — `AuthorityAgentExecutor` dispatching to existing
                        legacy handler functions (Slice 3)
    * executor_base   — placeholder; concrete subclass for the partner
                        backend lands in Slice 4

The Authority backend mounts the JSON-RPC endpoint in `app.main` at
`/a2a-rpc/rpc` alongside the legacy `POST /api/a2a/tasks/send` router.
Both wires are alive until Slice 6 flips registered partners over via
`PartnerAgent.protocol_version`.

The partner backend mount + executor land in Slice 4.
"""
from .mount import build_a2a_components
from .client import send_a2a_message
from .auth import fetch_bearer_jwt, reset_cache_for_tests
from .task_store_db import get_task_store, reset_engine_for_tests

__all__ = [
    "build_a2a_components",
    "send_a2a_message",
    "fetch_bearer_jwt",
    "reset_cache_for_tests",
    "get_task_store",
    "reset_engine_for_tests",
]
