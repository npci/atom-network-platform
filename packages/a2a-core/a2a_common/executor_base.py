# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Shared `AgentExecutor` base class — placeholder, not yet implemented.

Each backend will subclass `a2a.server.agent_execution.AgentExecutor` to
dispatch incoming Tasks to its existing handler functions:

    Authority backend — _process_status_update,
                        _process_readiness_declaration,
                        _process_change_acknowledgement,
                        process_cert_test_response, …
    Partner backend   — _handle_change_communication,
                        _handle_clarification_response, …

The shared base will provide:
    * Logging hooks (correlation id, latency, partner identification)
    * DB-row creation pattern (writes the matching audit row before
      returning, so the legacy and SDK paths produce identical state)
    * Error mapping (Python exceptions → A2A error parts)

This module currently ships only this docstring placeholder.
"""

__all__: list[str] = []
