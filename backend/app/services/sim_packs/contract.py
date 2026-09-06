# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The Capability Pack contract (SIM S-0) — pure: no I/O, no DB, no LLM.

A pack is an immutable JSON bundle describing how a simulator behaves for one
set of APIs, layered on a base pack (root: the baseline). Nothing in a pack is
authored by hand — the builder (S-1) projects it from the API Registry.

This module is HALF of a cross-language contract: the simulator (Java, in the
separate `oss-a2a-platform` repo — absent from this machine) must reproduce
`canonical_json` and `content_hash` byte-for-byte, or content addressing
breaks silently. The rules a Java implementation must match:

* **Canonical form**: JSON with keys sorted lexicographically at every level,
  separators `","` / `":"` (no whitespace), non-ASCII characters emitted as
  raw UTF-8 (never `\\uXXXX`-escaped), and **absent/None fields omitted
  entirely** (an explicit `null` never appears). Encoded as UTF-8.
* **Content hash**: `"sha256:" + hex(sha256(canonical bytes))` over the pack
  with the keys in `HASH_EXCLUDED_KEYS` removed first. Excluded: `pack_id`
  (cannot contain itself), `pack_ref`/`change_id` (human identity and scope —
  identical behaviour republished under a new revision keeps its address),
  and `provenance` (describes the build, not the behaviour; contains
  `generated_at`, which would break build-twice determinism).

Domain-neutral by construction: the template keys are `request_template` /
`response_template` with a per-API `wire_format` (first flavour "xml"), not
`request_xml` — the plan document's sketch predates the genericization
directive, and the Java record does not exist yet, so THIS schema is the
canonical one. `pack.schema.json` beside this module is the generated
cross-language snapshot; `tests/services/test_sim_pack_contract.py` pins it
to these models.

A `pack_ref` is a NON-SECRET identifier (it rides in query strings and lands
in access logs): never test data, never a credential, never a bank identity.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Collection, Mapping

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

__all__ = [
    "CONTRACT_VERSION",
    "HASH_EXCLUDED_KEYS",
    "PackValidationError",
    "SimPack",
    "canonical_json",
    "content_hash",
    "json_schema",
    "stamp",
    "validate_pack",
]

CONTRACT_VERSION = "1"

# Removed before hashing — see the module docstring for the rationale per key.
HASH_EXCLUDED_KEYS = frozenset({"pack_id", "pack_ref", "change_id", "provenance"})

# name@revision, both halves from the query-string-safe set. A pack_ref rides
# in URLs and logs, so the grammar is deliberately narrow.
_PACK_REF_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+$")


class PackValidationError(ValueError):
    """A pack that must not be published, with the reason in the message."""


class PackField(BaseModel):
    """One row of an API's field table — the same six constraint cells the
    grader asserts (`cert_assertions`), copied from the registry."""

    path: str                                   # registry grammar: "ReqTransfer/Head/@ver"
    occurrence: str | None = None               # "1..1", "0..n"
    datatype: str | None = None
    length_rule: str | None = None
    mandatory: str | None = None                # "Y" | "N" | "C"
    condition_text: str | None = None           # when mandatory == "C"
    enum_values: list[str] | None = None
    pattern_rule: str | None = None


class PackRoute(BaseModel):
    path: str                                   # e.g. "/execute"
    flow_code: str | None = None


class PackApi(BaseModel):
    api: str
    direction: str = "request"                  # "request" | "response" | "other"
    wire_format: str = "xml"                    # codec key — data, per API
    route: PackRoute | None = None
    request_template: str | None = None
    response_template: str | None = None
    fields: list[PackField] = Field(default_factory=list)


class ScenarioWhen(BaseModel):
    """Scenario identity — exactly one of: a test case, a request VARIANT
    (§3.1 — two variants of one case may expect different responses), or a
    field predicate."""

    tc_id: str | None = None
    variant_id: str | None = None
    field: str | None = None                    # registry field path
    eq: str | None = None                       # value the field must equal

    @model_validator(mode="after")
    def _exactly_one_identity(self) -> "ScenarioWhen":
        identities = [self.tc_id is not None, self.variant_id is not None,
                      self.field is not None]
        if sum(identities) != 1:
            raise ValueError(
                "scenario `when` must carry exactly one identity: "
                "tc_id | variant_id | field")
        if (self.field is None) != (self.eq is None):
            raise ValueError("`field` and `eq` come together or not at all")
        return self


class ScenarioRespond(BaseModel):
    rc: str                                     # response code the simulator returns
    delay_ms: int = 0
    error_text: str | None = None
    no_response: bool = False                   # timeout scenario


class PackScenario(BaseModel):
    when: ScenarioWhen
    respond: ScenarioRespond


class PackCoverage(BaseModel):
    """The honest half — what the pack does NOT cover, counted."""

    apis: int = 0
    fields_total: int = 0
    fields_with_constraints: int = 0
    gaps: list[str] = Field(default_factory=list)


class PackProvenance(BaseModel):
    registry_snapshot: str | None = None
    generated_at: str | None = None             # ISO-8601; OUTSIDE the hash
    coverage: PackCoverage | None = None


class SimPack(BaseModel):
    contract_version: str = CONTRACT_VERSION
    pack_ref: str                               # "CHG-4711@3" — human identity
    pack_id: str | None = None                  # "sha256:…" — the real identity
    base_pack: str                              # chain parent ref; root is the baseline
    engine_min: str = "1.0"                     # simulator capability floor
    requires: list[str] = Field(default_factory=list)
    change_id: str | None = None
    apis: list[PackApi]
    scenarios: list[PackScenario] = Field(default_factory=list)
    provenance: PackProvenance | None = None

    @field_validator("pack_ref", "base_pack")
    @classmethod
    def _ref_grammar(cls, v: str) -> str:
        if not _PACK_REF_RE.match(v):
            raise ValueError(
                f"pack ref {v!r} must be name@revision from [A-Za-z0-9._-] — "
                "it rides in query strings and access logs")
        return v

    def canonical_dict(self) -> dict:
        """The pack as canonical data: None-valued fields omitted."""
        return self.model_dump(exclude_none=True)


def canonical_json(data: Mapping[str, Any]) -> str:
    """THE canonical serialisation — the Java side must match byte-for-byte."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def content_hash(data: Mapping[str, Any]) -> str:
    """`pack_id` for a pack given as canonical data (None fields absent)."""
    hashed = {k: v for k, v in data.items() if k not in HASH_EXCLUDED_KEYS}
    digest = hashlib.sha256(canonical_json(hashed).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def stamp(pack: SimPack) -> SimPack:
    """Return a copy with `pack_id` set to the content address."""
    return pack.model_copy(update={"pack_id": content_hash(pack.canonical_dict())})


def validate_pack(
    data: Mapping[str, Any],
    *,
    capabilities: Collection[str] | None = None,
) -> SimPack:
    """Validate pack data; raise `PackValidationError` naming what is wrong.

    `capabilities` is the simulator's ADVERTISED set (`GET /api/packs/
    capabilities`) — passed in, never hardcoded here: the publish-time gate
    belongs to whoever talks to a concrete simulator. When given, a `requires`
    entry the simulator lacks fails validation with the capability named —
    at publish time, when a human is watching, not mid-certification.

    A present `pack_id` must match the content: a pack whose address disagrees
    with its bytes is corrupt or tampered with, and content addressing is the
    only integrity story a pack has.
    """
    try:
        pack = SimPack.model_validate(data)
    except ValidationError as exc:
        raise PackValidationError(f"pack does not match the contract: {exc}") from exc

    if pack.pack_id is not None:
        expected = content_hash(pack.canonical_dict())
        if pack.pack_id != expected:
            raise PackValidationError(
                f"pack_id {pack.pack_id!r} does not match the content "
                f"({expected}) — the pack is corrupt or was edited after "
                "stamping; packs are immutable")

    if capabilities is not None:
        missing = sorted(set(pack.requires) - set(capabilities))
        if missing:
            raise PackValidationError(
                f"pack requires capabilities this simulator does not "
                f"advertise: {', '.join(missing)} — a simulator release adding "
                "them is the scope of this rejection")

    return pack


def json_schema() -> dict:
    """The generated JSON Schema for the cross-language snapshot
    (`pack.schema.json`); the test pins the file to this output."""
    return SimPack.model_json_schema()
