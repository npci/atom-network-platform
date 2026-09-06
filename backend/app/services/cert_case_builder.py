# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Certification case-set generation from the API-Registry delta (CERT-1 + §3.1).

The scope of a certification round is DERIVED, not hand-picked: the registry
rows a change introduced (`introduced_by_change_id`) select which APIs get
certified, the test-case catalogue supplies the executable cases, and every
constrained field of each selected message becomes assertion rows. Two nouns,
two tables:

* a `CertRequestVariant` is one EXECUTABLE input combination (§3.1) — built by
  the pure generator in `cert_variants.py`;
* a `CertCaseSpec` is one ASSERTION over the payload a variant captured.

Judgements this module must keep honouring (each cost a round trip once):

* One spec row is one assertion, not one executed case — a forty-field message
  is forty-odd rows and ONE transaction.
* The field set is the WHOLE message, not only the changed fields: asserting a
  neighbouring constrained field is free and catches regressions a narrow
  scope misses. Delta selects APIs; it does not narrow fields.
* `available_cases` arrives as PLAIN DATA (the `case_details()` dicts). No
  simulator import, no psycopg2 — the builder and its tests need neither.
* `expected` and `field_path` are copied, never referenced.
* Deterministic; re-dispatch REPLACES the (cflow_id, run_number) rows.
* Only as good as the registry: a change that alters an API without touching
  the registry generates no cases, and `BuildResult.summary()` leads with what
  is NOT covered rather than burying it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field as dc_field
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from app.core.domain.contract import combination_rules_of, wire_format_of
from app.core.domain.registry import get_active_pack
from app.models.api_registry import ApiField, ApiMessage
from app.models.base import generate_uuid
from app.models.phase_c import CertCaseSpec, CertRequestVariant
from app.services.cert_agent.execution import internal_initiator
from app.services.cert_variants import generate_variants

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

__all__ = ["BuildResult", "RoundScope", "build", "delta_messages", "store",
           "derive_round_scope"]

# assertion_kind -> the ApiField columns copied into `expected`.
_FIELD_ASSERTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("occurrence", ("occurrence",)),
    ("datatype", ("datatype",)),
    ("length", ("length_rule",)),
    ("mandatory", ("mandatory", "condition_text")),
    ("enum", ("enum_values",)),
    ("pattern", ("pattern_rule",)),
)


@dataclass
class BuildResult:
    change_id: str
    cflow_id: str
    run_number: int
    fallback: bool = False
    uncovered_apis: list[str] = dc_field(default_factory=list)
    unconstrained_fields: list[str] = dc_field(default_factory=list)
    gaps: list[dict] = dc_field(default_factory=list)
    variants: list[CertRequestVariant] = dc_field(default_factory=list)
    specs: list[CertCaseSpec] = dc_field(default_factory=list)

    def summary(self) -> str:
        """Leads with what is NOT covered — the honest half is the useful half."""
        parts: list[str] = []
        if self.uncovered_apis:
            parts.append(
                f"NOT covered: {len(self.uncovered_apis)} changed API(s) with no "
                f"executable test case ({', '.join(sorted(self.uncovered_apis))})")
        if self.unconstrained_fields:
            parts.append(
                f"{len(self.unconstrained_fields)} changed field(s) carry no "
                "assertable constraint")
        if self.gaps:
            parts.append(f"{len(self.gaps)} combination-coverage gap(s) for review")
        if self.fallback:
            parts.append(
                "registry delta was EMPTY — fell back to the harness baseline "
                "scope (origin=harness_baseline), which certifies the existing "
                "contract, not this change")
        parts.append(
            f"generated {len(self.variants)} variant(s), {len(self.specs)} "
            f"assertion spec(s) for round {self.run_number}")
        return "; ".join(parts)


def delta_messages(db: "Session", change_id: str) -> dict[str, ApiMessage]:
    """lower(api_name) -> message, for every message the change touched —
    directly, or via any of its fields.

    Public on purpose: the sim-pack builder (SIM S-1) consumes THE SAME delta,
    so the simulator's behaviour and the grader's expectations are projections
    of one selection — two implementations of "what did this change touch"
    would be exactly the drift this design exists to prevent."""
    touched: dict[str, ApiMessage] = {}
    for msg in db.query(ApiMessage).filter(
            ApiMessage.introduced_by_change_id == change_id):
        touched[msg.api_name.lower()] = msg
    field_msgs = (
        db.query(ApiMessage)
        .join(ApiField, ApiField.message_id == ApiMessage.id)
        .filter(ApiField.introduced_by_change_id == change_id)
    )
    for msg in field_msgs:
        touched[msg.api_name.lower()] = msg
    return touched


def _constraints_of(field: ApiField) -> list[tuple[str, dict]]:
    """The assertion kinds this field row declares, with their COPIED payloads."""
    out: list[tuple[str, dict]] = []
    for kind, columns in _FIELD_ASSERTIONS:
        payload = {c: getattr(field, c) for c in columns if getattr(field, c)}
        if payload.get(columns[0]):
            out.append((kind, payload))
    return out


def _expected_outcome(case: Mapping[str, Any]) -> dict:
    """Neutral expected-outcome shape from a catalogue case dict."""
    status = (case.get("expected_status") or "").strip().upper() or None
    code = (case.get("authority_batch") or {}).get("expected_rc")
    return {"result": status, "code": code}


def build(
    db: "Session",
    *,
    change_id: str,
    cflow_id: str,
    run_number: int,
    available_cases: Sequence[Mapping[str, Any]],
    pack=None,
) -> BuildResult:
    """Plan the round's variants + assertion specs. Pure planning — nothing is
    written until `store()`."""
    pack = pack or get_active_pack()
    wire_fmt = wire_format_of(pack) or "xml"
    rules = combination_rules_of(pack)

    result = BuildResult(change_id=change_id, cflow_id=cflow_id,
                         run_number=run_number)

    delta = delta_messages(db, change_id)
    result.fallback = not delta

    # Which catalogue cases are in scope, and against which registry message.
    cases_by_api: dict[str, list[Mapping[str, Any]]] = {}
    for case in available_cases:
        cases_by_api.setdefault((case.get("api") or "").lower(), []).append(case)

    if result.fallback:
        selected = [(case, None) for case in available_cases]
        origin = "harness_baseline"
    else:
        selected = []
        origin = "registry_delta"
        for api_lower, msg in sorted(delta.items()):
            matched = cases_by_api.get(api_lower, [])
            if not matched:
                result.uncovered_apis.append(msg.api_name)
                continue
            selected.extend((case, msg) for case in matched)
        # Changed fields with nothing to assert — reported, not silently thin.
        for msg in delta.values():
            for f in msg.fields:
                if f.introduced_by_change_id == change_id and not _constraints_of(f):
                    result.unconstrained_fields.append(f.xpath)

    for case, msg in selected:
        case_id = case.get("case_id") or ""
        if not case_id:
            continue
        if msg is None:
            # Fallback scope may still know the message from the registry.
            msg = db.query(ApiMessage).filter(
                ApiMessage.api_name.ilike(case.get("api") or "")).first() \
                if case.get("api") else None

        authority_data = dict(case.get("authority_batch") or {})
        template = {k: str(v) for k, v in authority_data.items() if k != "expected_rc"}
        expected = _expected_outcome(case)
        api_name = (msg.api_name if msg else (case.get("api") or "")) or case_id

        # case_id scopes variant identity: two catalogue cases may carry
        # identical request inputs for the same API and are still different
        # executions (different case, different expected response).
        generation = generate_variants(api_name, case_id=case_id,
                                       template=template,
                                       expected=expected, rules=rules)
        result.gaps.extend(generation.gaps)

        for spec in generation.variants:
            variant_row = CertRequestVariant(
                # Explicit id: the column default fires at flush, and the spec
                # rows below need the FK value NOW.
                id=generate_uuid(),
                cflow_id=cflow_id, run_number=run_number, case_id=case_id,
                variant_id=spec.variant_id,
                api_message_id=msg.id if msg else None,
                initiator=internal_initiator(case.get("initiator")),
                wire_format=wire_fmt,
                input_data=dict(spec.input_data),
                expected=dict(spec.expected),
                strategy=spec.strategy,
                covered_rules=list(spec.covered_rules) or None,
                is_negative=spec.is_negative,
                fault_key=spec.fault_key,
                provenance={"change_id": change_id, "origin": origin,
                            "api_message_id": msg.id if msg else None},
            )
            result.variants.append(variant_row)

            # One response-code assertion per variant (it grades the outcome,
            # not the payload). authority_data rides here ONCE per variant —
            # §3.1 forbids copying input payloads into every assertion row.
            result.specs.append(CertCaseSpec(
                cflow_id=cflow_id, run_number=run_number, case_id=case_id,
                variant_id=variant_row.id,
                api_message_id=msg.id if msg else None,
                assertion_kind="response_code",
                expected=dict(spec.expected),
                origin=origin, wire_format=wire_fmt,
                authority_data=authority_data or None,
            ))
            # Whole-message field assertions — every constrained field, not
            # only the changed ones.
            if msg is not None:
                for f in msg.fields:
                    for kind, payload in _constraints_of(f):
                        result.specs.append(CertCaseSpec(
                            cflow_id=cflow_id, run_number=run_number,
                            case_id=case_id, variant_id=variant_row.id,
                            api_message_id=msg.id, api_field_id=f.id,
                            field_path=f.xpath, assertion_kind=kind,
                            expected=payload, origin=origin,
                            wire_format=wire_fmt,
                        ))

    return result


def store(db: "Session", result: BuildResult) -> None:
    """Persist a build, REPLACING any existing rows for this
    (cflow_id, run_number) — a re-dispatched round is a fresh snapshot, never
    an accumulation. A new round number leaves earlier rounds' immutable
    snapshots untouched."""
    db.query(CertCaseSpec).filter(
        CertCaseSpec.cflow_id == result.cflow_id,
        CertCaseSpec.run_number == result.run_number,
    ).delete(synchronize_session=False)
    db.query(CertRequestVariant).filter(
        CertRequestVariant.cflow_id == result.cflow_id,
        CertRequestVariant.run_number == result.run_number,
    ).delete(synchronize_session=False)
    db.add_all(result.variants)
    # Explicit flush: spec rows FK the variant rows, and a real Postgres
    # (enforced constraints — unlike the SQLite harness) needs the variants on
    # disk before the specs land. Found by the live migration walk, not by the
    # unit suite.
    db.flush()
    db.add_all(result.specs)
    db.commit()
    logger.info("cert_case_builder: stored %d variant(s), %d spec(s) for %s round %d — %s",
                len(result.variants), len(result.specs), result.cflow_id,
                result.run_number, result.summary())


@dataclass
class RoundScope:
    """What one dispatch actually executes and grades, derived from a build.

    Lives here rather than inline in the orchestrator because it is builder
    knowledge: which cases survived the delta, and how the generated variants
    and assertion specs index onto them.
    """

    result: "BuildResult"
    case_list: list                       # narrowed, wire-shaped case dicts
    case_ids: set[str]
    variants_by_case: dict[str, list]
    specs_by_variant: dict[str, list]

    @property
    def empty(self) -> bool:
        return not self.case_ids


def derive_round_scope(db: "Session", *, change_id: str, cflow_id: str,
                       run_number: int, available_cases: Sequence[Mapping[str, Any]],
                       pack=None) -> RoundScope:
    """Build + persist this round's variants and specs, and index them.

    The executed set narrows to what the builder generated: an API the change
    did not touch is not certified. Storage REPLACES any earlier rows for the
    same (cflow_id, run_number), so a re-dispatch is a fresh snapshot.
    """
    built = build(db, change_id=change_id, cflow_id=cflow_id,
                   run_number=run_number, available_cases=available_cases,
                   pack=pack)
    store(db, built)
    logger.info("cert_case_builder.round_scope change=%s round=%d — %s",
                change_id, run_number, built.summary())

    case_ids = {v.case_id for v in built.variants}
    variants_by_case: dict[str, list] = {}
    for variant in built.variants:
        variants_by_case.setdefault(variant.case_id, []).append(variant)
    specs_by_variant: dict[str, list] = {}
    for spec in built.specs:
        specs_by_variant.setdefault(spec.variant_id, []).append(spec)
    return RoundScope(
        result=built,
        case_list=[c for c in available_cases if c.get("case_id") in case_ids],
        case_ids=case_ids,
        variants_by_case=variants_by_case,
        specs_by_variant=specs_by_variant,
    )
