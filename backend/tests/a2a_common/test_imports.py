# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Slice 1 smoke tests.

Confirm `a2a-sdk` resolves and `a2a_common` re-exports the three public
functions. Real coverage of mount/executor/client behaviour lands in
Slices 3-5 once each backend wires up its AgentCard and Executor.

Skipped cleanly when the SDK isn't installed (CI environments without
the wheel) so this file never blocks a build before deps land.
"""
from __future__ import annotations

import pytest


def test_a2a_sdk_imports_resolve():
    """Sanity: the SDK's headline classes import and a basic AgentCard
    can be constructed. Catches install-but-broken-wheel scenarios."""
    pytest.importorskip("a2a")
    from a2a.types.a2a_pb2 import (  # noqa: WPS433  (test-only import)
        AgentCard,
        AgentCapabilities,
        AgentSkill,
    )
    from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore  # noqa: F401, WPS433
    from a2a.client import ClientConfig, ClientFactory  # noqa: F401, WPS433

    card = AgentCard(name="test-agent", description="smoke", version="0.0.1")
    assert card.name == "test-agent"
    # Capabilities + skills should round-trip without error.
    card.capabilities.CopyFrom(AgentCapabilities(streaming=False))
    card.skills.append(AgentSkill(id="ping", name="Ping", description="health"))
    assert card.skills[0].id == "ping"


def test_a2a_common_reexports_callable():
    """`from app.a2a_common import …` exposes the public surface."""
    pytest.importorskip("a2a")
    from app.a2a_common import (
        build_a2a_components,
        fetch_bearer_jwt,
        reset_cache_for_tests,
        send_a2a_message,
    )

    for fn in (build_a2a_components, send_a2a_message, fetch_bearer_jwt, reset_cache_for_tests):
        assert callable(fn), f"{fn} is not callable"
