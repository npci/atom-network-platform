# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Bearer JWT helpers for outbound A2A calls.

Today only the cert_engine handshake uses Bearer JWTs (see
`backend/app/services/a2a_client.py:_get_cert_engine_jwt`). Slice 5
generalises this so every partner type can advertise an `/a2a/auth`
endpoint and the platform exchanges its `api_key` for a short-lived JWT
on first use, refreshes proactively before expiry.

This module is the **client side** only: it fetches and caches tokens.
The receiving server (partner / cert-agent) owns issuance and validation.

Cache shape (post-Slice 9 of A2A security hardening):
    {
      partner_id: {
        "jwt":                str,    # access token (15-min TTL)
        "expires_at":         int,    # access expiry (epoch_seconds)
        "refresh_jwt":        str,    # refresh token (24h TTL)
        "refresh_expires_at": int,    # refresh expiry (epoch_seconds)
        "auth_url":           str,    # remembered for refresh path
      }
    }

When the access token is within `refresh_window_s` of expiry, the
helper calls /a2a/auth/refresh with the cached refresh token (cheap —
no api_key handshake). When the refresh ALSO expires, it falls back
to the api_key handshake. This makes the common path cheap (refresh
endpoint hit once every ~14 min) while still working when a worker
restart drops the cache.

Module-global so reuse survives across requests in the same process.
"""
from __future__ import annotations

import time
from typing import Optional

import httpx


_TOKEN_CACHE: dict[str, dict] = {}


async def fetch_bearer_jwt(
    partner_id: str,
    auth_url: str,
    api_key: str,
    *,
    refresh_window_s: int = 60,
    timeout: float = 15.0,
) -> Optional[str]:
    """Fetch (or reuse a cached) JWT from a partner's `/a2a/auth` endpoint.

    Args:
        partner_id:       Cache key — typically `PartnerAgent.id`.
        auth_url:         Full URL of the partner's `/a2a/auth` endpoint.
                          Slice 9 derives the refresh URL from this by
                          appending `/refresh`.
        api_key:          Static credential exchanged for a JWT.
        refresh_window_s: Refresh proactively this many seconds before
                          the cached token expires. 60s avoids races
                          where a token expires mid-request.
        timeout:          httpx timeout for the auth call itself.

    Returns:
        The JWT string, or None if the auth call fails. Callers must
        treat None as "auth not available" and decide whether to fall
        back to legacy POST or surface the failure.
    """
    now = int(time.time())
    cached = _TOKEN_CACHE.get(partner_id)

    # Hot path: cached access token still has comfortable runway.
    if cached and cached.get("expires_at", 0) - now > refresh_window_s:
        return cached["jwt"]

    # Warm path: access expired but refresh still valid → refresh-only call.
    if (
        cached
        and cached.get("refresh_jwt")
        and cached.get("refresh_expires_at", 0) - now > refresh_window_s
    ):
        token = await _refresh(cached, timeout)
        if token:
            return token
        # Refresh failed (server-side rotation race, or session revoked).
        # Fall through to the cold path below.

    # Cold path: api_key handshake. Either no cache, or refresh chain broken.
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(auth_url, json={"api_key": api_key})
            if resp.status_code != 200:
                return None
            data = resp.json()
    except Exception:
        # Network / DNS / timeout / JSON-parse — caller decides what to
        # do. We don't log here because the caller has more context
        # (partner name, endpoint url, attempt count).
        return None

    token = data.get("jwt") or data.get("access_token")
    if not token:
        return None

    _TOKEN_CACHE[partner_id] = {
        "jwt": token,
        "expires_at": now + int(data.get("expires_in", 900)),
        "refresh_jwt": data.get("refresh_token"),
        "refresh_expires_at": (
            now + int(data["refresh_expires_in"])
            if data.get("refresh_expires_in") is not None
            else 0
        ),
        "auth_url": auth_url,
    }
    return token


async def _refresh(cached: dict, timeout: float) -> Optional[str]:
    """POST the cached refresh token to /auth/refresh and update the cache.

    Returns the new access token on success, None on failure (caller
    falls back to the api_key handshake).
    """
    auth_url = cached.get("auth_url")
    refresh_jwt = cached.get("refresh_jwt")
    if not (auth_url and refresh_jwt):
        return None

    refresh_url = auth_url.rstrip("/") + "/refresh"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                refresh_url, json={"refresh_token": refresh_jwt},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
    except Exception:
        return None

    new_access = data.get("jwt") or data.get("access_token")
    if not new_access:
        return None

    now = int(time.time())
    cached["jwt"] = new_access
    cached["expires_at"] = now + int(data.get("expires_in", 900))
    if data.get("refresh_token"):
        cached["refresh_jwt"] = data["refresh_token"]
        cached["refresh_expires_at"] = (
            now + int(data["refresh_expires_in"])
            if data.get("refresh_expires_in") is not None
            else cached.get("refresh_expires_at", 0)
        )
    return new_access


def reset_cache_for_tests() -> None:
    """Clear the in-memory token cache. Used by unit tests; do not call
    from production code paths."""
    _TOKEN_CACHE.clear()
