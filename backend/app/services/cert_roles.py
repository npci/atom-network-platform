# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""The tokens naming each side of a certification exchange.

These were repository constants (`"NPCI"` / `"BANK"`) until it became clear that
made a wire value reachable by a vocabulary sweep. They are now pack data
(`cert_roles:`), REQUIRED of any pack that declares `certification_harness:` —
`config_pack` refuses to load one that omits them, so a deployment learns at
boot rather than mid-run.

Why they cannot be renamed freely, in one place so nobody has to rediscover it:

* they are written to `initiated_by` and therefore sit in rows already stored;
* they are the KEYS of the per-side counts in the push summary, which the SPA
  renders — so changing one is an API change, not a rename;
* the certification agent compares them against its own test-case catalogue.

Resolved per call rather than cached at import: the active pack is chosen by an
environment variable read at call time, and several tests swap packs mid-process.
"""
from __future__ import annotations

from app.core.domain.contract import cert_roles_of
from app.core.domain.registry import get_active_pack


def _roles() -> dict[str, str]:
    return dict(cert_roles_of(get_active_pack()))


def authority_role() -> str:
    """The token for the side that operates the network."""
    return _roles().get("authority", "")


def partner_role() -> str:
    """The token for the side being certified against it."""
    return _roles().get("partner", "")


def authority_wire() -> str:
    """The authority token as the certification WORKBOOK spells it.

    Distinct from `authority_role()` because `txn_initiated_by` has always
    carried a differently-cased spelling that cert-agent matches exactly —
    `"Bank"`, not `"BANK"`. Defaults to the canonical token when the pack
    declares no override.
    """
    return _roles().get("authority_wire") or authority_role()


def partner_wire() -> str:
    """The partner token as the certification workbook spells it — see
    `authority_wire`."""
    return _roles().get("partner_wire") or partner_role()


def both_roles() -> tuple[str, str]:
    """`(authority, partner)` — for the many call sites that need the pair."""
    r = _roles()
    return r.get("authority", ""), r.get("partner", "")


def zero_counts() -> dict[str, int]:
    """A fresh `{authority: 0, partner: 0}` counts map.

    The push summary reports per-side totals under these keys and the SPA reads
    them back, so building the map in one place keeps the emitted shape and the
    rendered shape from drifting apart.
    """
    a, p = both_roles()
    return {a: 0, p: 0}


def pick_counts(src) -> dict[str, int]:
    """Re-key a per-side counts map onto the configured tokens.

    Cert-agent reports these counts under its own role tokens and the SPA reads
    them back under the same ones, so this is a passthrough of a wire shape,
    not a translation. Missing or non-numeric entries become 0 rather than
    raising — a progress summary must never be the thing that fails a push.
    """
    src = src or {}
    out: dict[str, int] = {}
    for token in both_roles():
        try:
            out[token] = int(src.get(token, 0) or 0)
        except (TypeError, ValueError):
            out[token] = 0
    return out


def normalize_role(raw: str | None) -> str | None:
    """Map an inbound spelling to a canonical role token, or None.

    Accepts the token itself, its first letter, and the generic
    `authority`/`partner` words, all case-insensitively. The single-letter forms
    exist because uploaded case sheets have always carried `N`/`B` in the
    initiator column; they are matched against the CONFIGURED tokens rather than
    a hardcoded pair, so a deployment whose roles are (say) `AUTH`/`PART` gets
    `A`/`P` for free instead of silently still answering to the old letters.
    """
    if raw is None:
        return None
    v = str(raw).strip().upper()
    if not v:
        return None
    authority, partner = both_roles()

    # Full spellings — canonical AND wire. Every caller reads `txn_initiated_by`,
    # which carries the WIRE spelling, so matching only the canonical token made
    # a pack whose wire form is not a case-variant of its canonical form (say
    # `partner: PARTICIPANT` with `partner_wire: Member`) normalise to None on
    # every row. Callers treat None as "unknown" and leave `initiated_by` empty,
    # so the whole run would group under the counterparty's upload default with
    # nothing raised and nothing logged.
    for token, generic, wire in ((authority, "AUTHORITY", authority_wire()),
                                 (partner, "PARTNER", partner_wire())):
        if not token:
            continue
        if v in {token.upper(), generic, (wire or "").upper()}:
            return token

    # Single letter, accepted ONLY when it is unambiguous. Uploaded case sheets
    # have always carried initials in the initiator column, but two tokens
    # sharing one (`ACQUIRER`/`AGENT`) makes the second unreachable — silently,
    # and always in favour of whichever is tested first. Refusing an ambiguous
    # initial yields None, which callers already handle, instead of a confident
    # wrong answer.
    tokens = [t for t in (authority, partner) if t]
    if len(v) == 1 and len({t[:1].upper() for t in tokens}) == len(tokens):
        for token in tokens:
            if v == token[:1].upper():
                return token
    return None
