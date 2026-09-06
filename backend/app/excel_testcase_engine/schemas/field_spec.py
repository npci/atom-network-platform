# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Field-catalog Pydantic model (Slice 2 — cert-tc-v2).

The field catalog is per-API metadata that lets the Planner emit
deterministic field-negative test cases (missing_mandatory, field_length,
invalid_format, invalid_enum, wrong_leg, unexpected_leg) instead of
improvising them from TSD prose.

Source of truth: `npci_specs/message_fields.json`. Every value is
best-effort — where a constraint is genuinely unknown, leave the field
`None` and the Planner will skip that particular field-negative shape
rather than fabricate a constraint.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


MessageLeg = Literal["Req", "Resp", "Notification", "Ack"]


class FieldLengthSpec(BaseModel):
    """Min/max character-length constraint for a field.

    None on either side means the constraint is not enforced at that end.
    Both None => length-negative cases are skipped for this field.
    """

    min: int | None = None
    max: int | None = None


class FieldSpec(BaseModel):
    """Per-field metadata used by the Planner and Validator.

    `name` is the wire-format field name (e.g. "amount", "payerVpa",
    "preAuthLimit") — case-preserving. `legs` lists which message legs
    of the primary API this field appears in.
    """

    name: str
    description: str = ""
    mandatory: bool = False
    legs: list[MessageLeg] = Field(default_factory=list)
    # ECMA-262 / Python `re`-compatible regex. When None, format-negative
    # cases are skipped (we don't fabricate patterns).
    format: str | None = None
    length: FieldLengthSpec | None = None
    # Explicit list of allowed values. When None or empty, enum-negative
    # cases are skipped. Values are compared case-sensitively.
    enum: list[str] | None = None
    # Free-text business-rule handle (e.g. "amount MUST be <= per_txn_limit").
    # Used by the Planner to emit business_rule_fail cases when set.
    business_rule_ref: str | None = None
    # Free-text provenance so a reviewer can trace where the constraint came
    # from (BRD section, XSD import, hand-authored, etc.). Not enforced.
    source: str = ""


class ApiFieldCatalog(BaseModel):
    """The catalog for one API — a dict of field-name → FieldSpec.

    Kept as a wrapper so pydantic validates each FieldSpec at load time
    and consumers can iterate `.fields.values()` cleanly.
    """

    api: str
    fields: dict[str, FieldSpec] = Field(default_factory=dict)


class FieldCatalog(BaseModel):
    """Top-level catalog — a dict of API-name → ApiFieldCatalog.

    Loaders return this shape. Consumers should use
    `npci_specs.get_field_specs(api)` rather than reading the raw dict
    so the None-when-missing contract stays consistent.
    """

    apis: dict[str, ApiFieldCatalog] = Field(default_factory=dict)
