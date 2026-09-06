# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The simulator's request handler — one function, two edges.

`handle()` is the whole execute path (resolve → identify → validate →
scenario → respond); `api/sim_execute.py` translates it to HTTP for partner
stacks, and the sim_pack certification harness calls it in-process. One
implementation so the harness certifies against EXACTLY what a partner's
stack would hit.

Refusals raise `SimRefusal` carrying the HTTP status and machine-readable
payload; the §3.1 rule lives in `resolver.resolve_request` (unknown/withdrawn
→ 400 `unknown_pack`, never a fallback).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.wire.codec import CodecError
from app.core.wire.registry import codec_for
from app.services.simulator import resolver, scenario, validation

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

__all__ = ["SimRefusal", "SimReply", "handle"]

_DELAY_CAP_MS = 10_000
DEFAULT_RC = "00"


class SimRefusal(Exception):
    def __init__(self, status: int, payload: dict,
                 pack_header: str | None = None):
        super().__init__(payload.get("error", "refused"))
        self.status = status
        self.payload = payload
        self.pack_header = pack_header


@dataclass
class SimReply:
    rc: str
    content: str
    media_type: str
    pack_header: str            # "<ref> <pack_id>" | "none"
    scenario: str               # "variant_id:v-1" | "tc_id:PR_7" | "field:…" | "default"


def _identify(resolved, body: bytes | str):
    """(api_entry, codec, doc) for the body's root, asked through each
    declared wire format's codec — no format specifics here."""
    by_format: dict[str, list[dict]] = {}
    for entry in resolved.apis.values():
        by_format.setdefault(entry.get("wire_format", "xml"), []).append(entry)
    for fmt, entries in sorted(by_format.items()):
        codec = codec_for(fmt)
        try:
            doc = codec.parse(body)
        except CodecError:
            continue
        for entry in entries:
            if codec.count(doc, entry["api"]) >= 1:
                return entry, codec, doc
    return None, None, None


async def handle(db: "Session", *, body: bytes | str,
                 pack: str | None = None,
                 tc_id: str | None = None,
                 variant_id: str | None = None) -> SimReply:
    try:
        resolved = resolver.resolve_request(db, pack)
    except resolver.UnknownPackError as exc:
        raise SimRefusal(400, {"error": "unknown_pack", "detail": str(exc)})

    if resolved is None:
        # The pre-pack world, stated — not a fallback from a bad ref.
        return SimReply(rc=DEFAULT_RC, content=f'<Response rc="{DEFAULT_RC}"/>',
                        media_type="application/xml", pack_header="none",
                        scenario="default")

    pack_header = f"{resolved.pack_ref} {resolved.pack_id}"
    entry, codec, doc = _identify(resolved, body)
    if entry is None:
        raise SimRefusal(
            400, {"error": "unknown_api",
                  "detail": "no API in the resolved pack matches the request "
                            "body's root"}, pack_header)

    violations = validation.validate_request(entry, body, codec=codec)
    if violations:
        raise SimRefusal(
            422, {"error": "validation_failed", "api": entry["api"],
                  "violations": violations}, pack_header)

    chosen = scenario.choose(resolved.scenarios, variant_id=variant_id,
                             tc_id=tc_id, doc=doc, codec=codec)
    if chosen is None:
        rc, how = DEFAULT_RC, "default"
        # Deliberately LOUD (plan S-4): the pack declared no scenario for this
        # case/variant, so the response code fell back to success. Staying
        # quiet here would trade a loud gap for a silent pass — an
        # unconfigured case that answers "00" looks exactly like a case that
        # passed on purpose. Also recorded per-result as scenario="default".
        logger.warning(
            "simulator: respcode defaulted to %s — pack %s declares no "
            "scenario for api=%s tc_id=%s variant_id=%s",
            DEFAULT_RC, resolved.pack_ref, entry["api"], tc_id, variant_id)
    else:
        respond = chosen.get("respond", {})
        rc = str(respond.get("rc", DEFAULT_RC))
        when = chosen.get("when", {})
        how = next(f"{k}:{v}" for k, v in when.items() if k != "eq" and v)
        delay_ms = int(respond.get("delay_ms") or 0)
        if delay_ms > 0:
            await asyncio.sleep(min(delay_ms, _DELAY_CAP_MS) / 1000)
        if respond.get("no_response"):
            raise SimRefusal(504, {"error": "scenario_no_response",
                                   "scenario": how}, pack_header)

    template = entry.get("response_template")
    content = template.replace("{{rc}}", rc) if template \
        else f'<Response rc="{rc}"/>'
    media = "application/xml" if entry.get("wire_format", "xml") == "xml" \
        else "application/octet-stream"
    return SimReply(rc=rc, content=content, media_type=media,
                    pack_header=pack_header, scenario=how)
