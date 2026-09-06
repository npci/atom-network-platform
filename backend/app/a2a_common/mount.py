# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Generic A2A JSON-RPC mount.

Lifted from `certagent/cert-agent/app/a2a/mount.py` and parameterised so
any backend can supply its own AgentCard and AgentExecutor. The wiring
itself is identical across the three subsystems.

Usage (in a backend's main.py — Slice 3+):

    from app.a2a_common import build_a2a_components
    from .my_card import MY_AGENT_CARD
    from .my_executor import MyExecutor

    sub_app, card_routes = build_a2a_components(
        agent_card=MY_AGENT_CARD,
        executor=MyExecutor(),
    )
    app.mount("/a2a-rpc", sub_app)
    for r in card_routes:
        app.add_route(r.path, r.endpoint, methods=list(r.methods))

Public surface:
    build_a2a_components(agent_card, executor, task_store=None, rpc_url="/rpc")
        Returns (sub_app, card_routes).
        sub_app    — Starlette app to mount; serves POST {rpc_url}.
        card_routes — list of Starlette Route objects for
                      /.well-known/agent-card.json (and friends).
"""
from __future__ import annotations

from typing import Optional, Type

from starlette.applications import Starlette
from starlette.middleware import Middleware

from a2a.server.request_handlers.default_request_handler import LegacyRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore


def build_a2a_components(
    agent_card,
    executor,
    *,
    task_store=None,
    rpc_url: str = "/rpc",
    auth_middleware: Optional[Type] = None,
    hmac_middleware: Optional[Type] = None,
):
    """Build the JSON-RPC mount + agent-card routes for a backend.

    Args:
        agent_card:       `a2a.types.a2a_pb2.AgentCard` describing this agent's
                          name / capabilities / skills / authentication schemes.
        executor:         `a2a.server.agent_execution.AgentExecutor` subclass
                          that handles incoming Tasks. Each backend writes its
                          own — see Slice 3/4 of the SDK refactor.
        task_store:       Optional `TaskStore`. Defaults to `InMemoryTaskStore()`.
                          Slice 2 of the SDK refactor swaps in a SQLAlchemy-
                          backed store for durability.
        rpc_url:          Path under the mount where JSON-RPC is served.
                          Default `/rpc` matches cert-agent's existing
                          `/a2a-rpc/rpc` URL.
        auth_middleware:  Optional Starlette middleware CLASS (not instance)
                          to wrap the sub-app with. Slice 2 of the security
                          hardening passes `SdkAuthMiddleware` here for the
                          the Authority side; cert-agent (Slice 2.5) and partner
                          (Slice 3) pass their own. None = no auth (current
                          behaviour for any caller that hasn't migrated yet).
        hmac_middleware:  Optional ASGI middleware CLASS that enforces the
                          X-NPCI-Signature envelope (Slice 5). Wrapped
                          OUTSIDE of `auth_middleware` so the body buffer
                          and HMAC check happen before JWT decode — see
                          `sdk_hmac_middleware.SdkHmacMiddleware` for the
                          ordering rationale.

    Returns:
        (sub_app, card_routes) — see module docstring.
    """
    if task_store is None:
        task_store = InMemoryTaskStore()
    handler = LegacyRequestHandler(
        agent_executor=executor,
        task_store=task_store,
        agent_card=agent_card,
    )
    rpc_routes = create_jsonrpc_routes(handler, rpc_url=rpc_url)
    card_routes = create_agent_card_routes(agent_card)
    # Order: HMAC outermost (reads body once, replays), then JWT auth
    # (header-only, no body read), then the SDK app. Starlette applies
    # middleware in list order top-down → first item is the outermost.
    middleware: list[Middleware] = []
    if hmac_middleware is not None:
        middleware.append(Middleware(hmac_middleware))
    if auth_middleware is not None:
        middleware.append(Middleware(auth_middleware))
    sub_app = Starlette(routes=rpc_routes, middleware=middleware)
    return sub_app, card_routes
