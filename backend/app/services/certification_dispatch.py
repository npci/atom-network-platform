# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Run a certification through whatever harness the active domain pack declares.

Counterpart to `partner_dispatch.notify_partner`. Callers ask for a
certification; this decides who performs it.

Replaces an `if settings.precert_engine_enabled:` that lived at the top of
`cert_orchestrator.orchestrate_cert_run`. That branch worked, but it meant the
only way to certify differently was to edit that function — and it could not
move into the adapter, because the adapter calls it.
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.domain.contract import (
    CertResult,
    certification_harness_of,
    certification_of,
)
from app.core.domain.registry import get_active_pack

logger = logging.getLogger(__name__)


def _resolve_harness(key: str | None):
    """The harness for this dispatch, or None when the domain declares none.

    Resolution order, most specific first:

    1. A `dispatch_meta["harness"]` key — the per-change/per-partner choice.
       A pack that declares `certification()` gets to honour (or refuse) the
       key itself; otherwise the platform registry resolves it. Unknown keys
       RAISE (ValueError) in both paths — certifying through a different
       engine than the dispatch asked for is the silent failure this refuses.
    2. A Python pack's own harness: `certification_of(pack)`.
    3. A config pack's NAMED harness: `certification_harness_of(pack)` keys
       into the platform registry (`services/cert_harnesses`). The YAML names
       platform-registered behaviour; the platform supplies it.
    4. Nothing declared → None. Omission still means the domain has no
       certification body, and the caller skips — an internal deprecation
       has nothing to certify against, and that stays a true statement.
    """
    pack = get_active_pack()
    if key:
        if callable(getattr(pack, "certification", None)):
            return certification_of(pack, key)
        from app.services.cert_harnesses import harness_by_key

        return harness_by_key(key)

    harness = certification_of(pack)
    if harness is not None:
        return harness

    name = certification_harness_of(pack)
    if name:
        from app.services.cert_harnesses import harness_by_key

        return harness_by_key(name)
    return None


async def run_certification(
    change_id: str,
    partner_id: str,
    role: str,
    test_data: dict[str, Any],
    test_data_per_case: dict[str, Any] | None = None,
    dispatch_meta: dict[str, Any] | None = None,
) -> CertResult | None:
    """Certify one partner for one change. None when the domain has no certifier.

    None is a real answer, not a failure. An internal API deprecation has no
    certification body — nothing to submit to and no verdict to wait for — so a
    lifecycle that treats a missing harness as an error would deadlock on the
    first domain that has none.

    Never raises, matching the orchestrators it wraps: a certification failure
    is a business outcome that surfaces through status and logs, not an
    exception that aborts the caller's background task.
    """
    # S-6: the harness is a per-dispatch choice — and a dispatch IS one
    # (change, partner) pair, so naming it here is the per-change/per-partner
    # selection. Absent, the domain's own declaration stands — a Python
    # pack's harness object, or a config pack's named platform harness
    # (see `_resolve_harness` for the full order).
    _key = (dispatch_meta or {}).get("harness")
    try:
        harness = _resolve_harness(_key)
    except ValueError:
        logger.exception(
            "certification refused: harness %r cannot be supplied for the "
            "active domain (change=%s partner=%s)",
            _key, change_id, partner_id)
        return CertResult(passed=None,
                          details={"error": "unknown_harness", "harness": _key})
    if harness is None:
        logger.info(
            "certification skipped: active domain declares no certification "
            "harness (change=%s partner=%s)", change_id, partner_id,
        )
        return None

    try:
        result = await harness.run(
            change_id=change_id,
            partner_id=partner_id,
            role=role,
            test_data=test_data or {},
            test_data_per_case=test_data_per_case or {},
            # C-6 round audit: who dispatched (operator|auto) and the chain to
            # the previous round / triggering fix notification. Optional and
            # None on every pre-loop call path.
            dispatch_meta=dispatch_meta,
        )
    except Exception:  # noqa: BLE001 — see docstring: fire-and-forget contract
        logger.exception(
            "certification run raised via harness=%s change=%s partner=%s",
            getattr(harness, "key", "?"), change_id, partner_id,
        )
        return CertResult(passed=None, details={"error": "harness raised"})

    logger.info(
        "certification run complete: harness=%s change=%s partner=%s passed=%s",
        getattr(harness, "key", "?"), change_id, partner_id, result.passed,
    )
    return result
