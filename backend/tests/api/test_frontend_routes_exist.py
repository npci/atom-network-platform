# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Every endpoint the frontend calls must resolve to a real backend route.

This exists because the same accident has now happened three times, always the
same way: a commit whose stated purpose was something else quietly deleted code
another commit had added, nothing failed, and the loss only surfaced when a human
clicked a button.

  1a9fb6c7  added POST cert/demo-run, POST cert/demo-reset, GET cert/txns
            ("Purely additive: 116 insertions, 0 deletions")
  11e354a5  "emit round_opened / round_closed on every round transition"
            deleted all three, unmentioned in its message

  68cd0a44  wired the partner's four cert lifecycle handlers
  33dc4a9e  "handle round_opened / round_closed inbound notices" deleted them
            (see the partner platform's own tests/test_handlers.py for
            that guard — separate repository)

The cert conversation view kept rendering; "Run certification" just returned
"Not Found", the Reset button did nothing, and the transaction tab polled a 404
every 1.5 seconds. A frontend that calls an endpoint is not evidence the endpoint
exists -- this test is what makes that true.

It parses the URLs out of services/api.js rather than listing them by hand, so a
newly added call is covered automatically.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.api.router import api_router


def _find_api_js() -> Path | None:
    """Locate frontend/src/services/api.js by walking up from this file.

    Searched rather than hardcoded because the backend image copies only
    `backend/` -- inside the container there is no frontend tree, so the sweep
    below cannot run. It SKIPS there rather than passing, so a green container
    run is never mistaken for the check having happened; on a full checkout (dev
    box, CI with the repo) it runs for real.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "frontend" / "src" / "services" / "api.js"
        if candidate.is_file():
            return candidate
    return None


API_JS = _find_api_js()
_NO_FRONTEND = pytest.mark.skipif(
    API_JS is None,
    reason="frontend/ not present (backend-only image) -- run from a full checkout",
)

# api.<method>(`/path/${expr}/more`) -- template literals with interpolation.
_CALL = re.compile(r"""api\.(get|post|put|patch|delete)\(\s*`([^`]+)`""", re.I)
# Interpolations stand in for path params: `/changes/${id}/x` -> /changes/{p}/x
_INTERP = re.compile(r"\$\{[^}]*\}")


def _frontend_calls() -> set[tuple[str, str]]:
    if API_JS is None:
        return set()
    src = API_JS.read_text(encoding="utf-8")
    out: set[tuple[str, str]] = set()
    for method, raw in _CALL.findall(src):
        path = _INTERP.sub("{p}", raw).split("?")[0].rstrip("/")
        if not path.startswith("/"):
            continue
        out.add((method.upper(), path))
    return out


def _backend_routes() -> set[tuple[str, str]]:
    """Every path the app can serve, normalised for comparison.

    `api_router` alone is NOT the answer. Some routers are attached at startup
    rather than statically -- `main._register_excel_testcase_engine_once` calls
    `app.include_router(engine_router, prefix="/api")` behind an
    `excel_engine_enabled` flag -- so a check against api_router reports the five
    live product-kit/cert_test_cases endpoints as missing. That false positive is
    exactly the noise that gets a guard like this switched off, so the
    dynamically-registered routers are merged in explicitly below.

    If another router is ever attached at startup, add it here.
    """
    routers = [api_router]
    try:
        from app.excel_testcase_engine.api import router as engine_router
        routers.append(engine_router)
    except Exception:  # noqa: BLE001 — optional subsystem; absence is not a failure
        pass

    out: set[tuple[str, str]] = set()

    def _walk(router, prefix: str = "") -> None:
        """Collect (METHOD, path) pairs, descending into included routers.

        FastAPI 0.141 changed the shape of `router.routes`: an included router is
        now kept as an `_IncludedRouter` wrapper instead of being flattened into
        the parent's route list, and that wrapper has no `.path`. The previous
        version of this helper read `getattr(r, "path", "")`, so under 0.141 it
        skipped EVERY nested route silently and reported live endpoints as
        missing — a guard that fails open is worse than no guard. Recurse through
        `.original_router` when present, and keep working on the older flattened
        shape too.
        """
        for r in getattr(router, "routes", []) or []:
            inner = getattr(r, "original_router", None)
            if inner is not None:
                _walk(inner, prefix + (getattr(r, "prefix", "") or ""))
                continue
            raw = getattr(r, "path", None)
            if raw is None:
                continue
            path = re.sub(r"\{[^}]+\}", "{p}", prefix + raw).rstrip("/")
            for m in getattr(r, "methods", set()) or set():
                out.add((m.upper(), path))

    for router in routers:
        _walk(router)
    return out


@_NO_FRONTEND
def test_the_sweep_actually_found_calls_to_check():
    """Guards the guard: a regex that silently matches nothing would pass everything."""
    assert len(_frontend_calls()) > 20, "parsed suspiciously few calls out of api.js"


@_NO_FRONTEND
@pytest.mark.parametrize("call", sorted(_frontend_calls()))
def test_every_frontend_call_has_a_backend_route(call):
    """Parametrised so a failure names the exact dead endpoint, not just a count."""
    method, path = call
    routes = _backend_routes()
    if (method, path) in routes:
        return
    # A wrong-method hit is a different bug from a missing path; say which.
    same_path = sorted(m for m, p in routes if p == path)
    assert not same_path, f"{method} {path} not routed, but {same_path} is -- wrong method?"
    pytest.fail(f"{method} {path} is called by services/api.js but no backend route matches")


def test_the_three_demo_endpoints_are_present():
    """Named explicitly: these are the ones 11e354a5 removed."""
    routes = _backend_routes()
    for method, path in (
        ("POST", "/changes/{p}/partners/{p}/cert/demo-run"),
        ("POST", "/changes/{p}/partners/{p}/cert/demo-reset"),
        ("GET", "/changes/{p}/partners/{p}/cert/txns"),
    ):
        assert (method, path) in routes, f"{method} {path} missing -- restored by hand once already"
