# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Pack builder from the API-Registry delta (SIM S-1).

Projects the SAME delta that builds `cert_case_specs`/`cert_request_variants`
(`cert_case_builder.delta_messages` — one function, not a reimplementation)
into a Capability Pack layered on the active baseline. Neither the simulator's
behaviour nor the grader's expectations is authored; both are projections of
the same registry rows, so they cannot drift apart.

Judgements this module must keep honouring:

* **An empty delta produces NO pack at all — never an empty one.** An empty
  pack published over a baseline is indistinguishable from "nothing changed"
  and hides a broken delta. `build_pack` returns None; the caller decides
  what that means.
* Deterministic: same rows in → identical `pack_id` out. Nothing volatile is
  hashed (`generated_at` lives in provenance, outside the hash) and every
  list is deterministically ordered.
* Routes arrive as PLAIN DATA (no simulator import): an API without a
  declared route ships without one and the gap is COUNTED, not invented.
* Scenarios come from the round's request variants — a §3.1 variant is the
  scenario identity (`when.variant_id`), because two variants of one case may
  expect different responses. A variant with no expected response code is a
  gap, not a guessed `00`.
* Field tables are the WHOLE message, copied — same rule and reason as
  `cert_case_specs.expected`.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from app.core.domain.contract import wire_format_of
from app.core.domain.registry import get_active_pack
from app.services.cert_case_builder import delta_messages
from app.services.sim_packs.contract import (
    PackApi,
    PackCoverage,
    PackField,
    PackProvenance,
    PackRoute,
    PackScenario,
    ScenarioRespond,
    ScenarioWhen,
    SimPack,
    stamp,
    validate_pack,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

__all__ = ["build_pack", "build_baseline_pack"]

# The six assertable cells — a field carrying none of them is counted as
# unconstrained in coverage (the honest half).
_CONSTRAINT_COLUMNS = ("occurrence", "datatype", "length_rule", "mandatory",
                      "enum_values", "pattern_rule")


def _pack_field(f) -> PackField:
    return PackField(
        path=f.xpath,
        occurrence=f.occurrence or None,
        datatype=f.datatype or None,
        length_rule=f.length_rule or None,
        mandatory=f.mandatory or None,
        condition_text=f.condition_text or None,
        enum_values=list(f.enum_values) if f.enum_values else None,
        pattern_rule=f.pattern_rule or None,
    )


def _scenarios_from_variants(variants: Sequence[Any],
                             gaps: list[str]) -> list[PackScenario]:
    """One scenario per variant that declares an expected response code,
    ordered by variant_id. Duck-typed attribute access — takes the
    `CertRequestVariant` rows a round built, or any stand-in with the same
    attributes."""
    out: list[PackScenario] = []
    for v in sorted(variants, key=lambda v: v.variant_id):
        expected = dict(v.expected or {})
        rc = expected.get("code")
        if not rc:
            gaps.append(
                f"variant {v.variant_id} (case {v.case_id}) declares no "
                "expected response code — no scenario generated, the "
                "simulator falls back to its base pack for it")
            continue
        out.append(PackScenario(
            when=ScenarioWhen(variant_id=v.variant_id),
            respond=ScenarioRespond(rc=str(rc)),
        ))
    return out


def build_pack(
    db: "Session",
    *,
    change_id: str,
    pack_ref: str,
    base_pack_ref: str,
    variants: Sequence[Any] = (),
    routes: Mapping[str, Mapping[str, Any]] | None = None,
    engine_min: str = "1.0",
    requires: Sequence[str] = (),
    registry_snapshot: str | None = None,
    generated_at: str | None = None,
    pack=None,
) -> SimPack | None:
    """Build the pack for one change, layered on `base_pack_ref`.

    Returns the stamped, self-validated pack — or **None when the registry
    delta is empty**. `routes` is plain data keyed by lower-cased api name;
    `variants` is the round's generated request variants (scenario source);
    `generated_at` is caller-supplied so the build stays deterministic.
    """
    domain = pack or get_active_pack()
    wire_fmt = wire_format_of(domain) or "xml"
    route_map = {k.lower(): dict(v) for k, v in (routes or {}).items()}

    delta = delta_messages(db, change_id)
    if not delta:
        logger.info("sim_packs.builder: empty registry delta for change %s — "
                    "building NO pack (an empty pack would hide a broken delta)",
                    change_id)
        return None

    gaps: list[str] = []
    apis: list[PackApi] = []
    fields_total = 0
    fields_with_constraints = 0

    for api_lower, msg in sorted(delta.items()):
        route_data = route_map.get(api_lower)
        if route_data is None:
            gaps.append(
                f"no route declared for {msg.api_name} — the simulator cannot "
                "dispatch it until one is registered")
        fields = [_pack_field(f) for f in msg.fields]
        fields_total += len(fields)
        fields_with_constraints += sum(
            1 for f in msg.fields
            if any(getattr(f, col) for col in _CONSTRAINT_COLUMNS))

        template = msg.sample_xml or None
        apis.append(PackApi(
            api=msg.api_name,
            direction=msg.direction or "other",
            wire_format=wire_fmt,
            route=PackRoute(**route_data) if route_data else None,
            request_template=template if msg.direction == "request" else None,
            response_template=template if msg.direction == "response" else None,
            fields=fields,
        ))

    scenarios = _scenarios_from_variants(variants, gaps)

    built = SimPack(
        pack_ref=pack_ref,
        base_pack=base_pack_ref,
        engine_min=engine_min,
        requires=sorted(requires),
        change_id=change_id,
        apis=apis,
        scenarios=scenarios,
        provenance=PackProvenance(
            registry_snapshot=registry_snapshot,
            generated_at=generated_at,
            coverage=PackCoverage(
                apis=len(apis),
                fields_total=fields_total,
                fields_with_constraints=fields_with_constraints,
                gaps=gaps,
            ),
        ),
    )
    stamped = stamp(built)
    # Self-check against the published contract before anyone ships it.
    validate_pack(stamped.canonical_dict())
    logger.info("sim_packs.builder: %s -> %s (%d api(s), %d scenario(s), "
                "%d gap(s))", pack_ref, stamped.pack_id, len(apis),
                len(scenarios), len(gaps))
    return stamped


def build_baseline_pack(
    db: "Session",
    *,
    pack_ref: str,
    available_cases: Sequence[Mapping[str, Any]] = (),
    routes: Mapping[str, Mapping[str, Any]] | None = None,
    engine_min: str = "1.0",
    generated_at: str | None = None,
    pack=None,
) -> SimPack:
    """The chain ROOT: the contract as the registry stands — every active
    message, whole field tables — plus per-case scenarios (`when.tc_id` →
    the catalogue's expected rc) from `available_cases`, plain data like
    everywhere else. Declares itself its own base (the root marker).

    Refuses an EMPTY registry: a baseline built from nothing would resolve
    every absent-`?pack=` call against a contract that says nothing — that is
    a seeding failure (`scripts/seed_kb_baseline_xsds.py`), not a baseline.
    """
    from app.models.api_registry import ApiMessage

    domain = pack or get_active_pack()
    wire_fmt = wire_format_of(domain) or "xml"
    route_map = {k.lower(): dict(v) for k, v in (routes or {}).items()}

    messages = (db.query(ApiMessage).filter(ApiMessage.status == "active")
                .order_by(ApiMessage.api_name).all())
    if not messages:
        raise ValueError(
            "the API Registry is empty — a baseline built from nothing is a "
            "seeding failure (scripts/seed_kb_baseline_xsds.py), not a baseline")

    gaps: list[str] = []
    apis: list[PackApi] = []
    fields_total = 0
    fields_with_constraints = 0
    for msg in messages:
        route_data = route_map.get(msg.api_name.lower())
        if route_data is None:
            gaps.append(
                f"no route declared for {msg.api_name} — the simulator cannot "
                "dispatch it until one is registered")
        fields = [_pack_field(f) for f in msg.fields]
        fields_total += len(fields)
        fields_with_constraints += sum(
            1 for f in msg.fields
            if any(getattr(f, col) for col in _CONSTRAINT_COLUMNS))
        template = msg.sample_xml or None
        apis.append(PackApi(
            api=msg.api_name,
            direction=msg.direction or "other",
            wire_format=wire_fmt,
            route=PackRoute(**route_data) if route_data else None,
            request_template=template if msg.direction == "request" else None,
            response_template=template if msg.direction == "response" else None,
            fields=fields,
        ))

    scenarios: list[PackScenario] = []
    for case in sorted(available_cases, key=lambda c: str(c.get("case_id") or "")):
        case_id = case.get("case_id")
        rc = (case.get("authority_batch") or {}).get("expected_rc")
        if not case_id:
            continue
        if not rc:
            gaps.append(f"case {case_id} declares no expected response code — "
                        "no scenario generated")
            continue
        scenarios.append(PackScenario(when=ScenarioWhen(tc_id=str(case_id)),
                                      respond=ScenarioRespond(rc=str(rc))))

    built = SimPack(
        pack_ref=pack_ref,
        base_pack=pack_ref,                    # the root marker
        engine_min=engine_min,
        apis=apis,
        scenarios=scenarios,
        provenance=PackProvenance(
            generated_at=generated_at,
            coverage=PackCoverage(apis=len(apis), fields_total=fields_total,
                                  fields_with_constraints=fields_with_constraints,
                                  gaps=gaps),
        ),
    )
    stamped = stamp(built)
    validate_pack(stamped.canonical_dict())
    logger.info("sim_packs.builder: baseline %s -> %s (%d api(s), %d scenario(s))",
                pack_ref, stamped.pack_id, len(apis), len(scenarios))
    return stamped
