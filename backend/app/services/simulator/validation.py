# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Ingress validation from the pack field table (SIM-3).

NOT a second implementation: the six rule kinds are `cert_assertions` — the
grader's own functions — fed with spec rows synthesized from `apis[].fields[]`.
Expressed once in the pack, executed by one engine on both sides: a bank that
fails at the simulator and a bank that fails at the grader fail for the same
documented reason, because it is literally the same code.

v1 ingress strictness: any FAIL rejects; SKIPs are the same honest non-answers
they are on the grading side (a SKIP never rejects). Per-constraint
reject-vs-warn strictness is pack data the contract does not carry yet.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from app.services.cert_assertions import assertion_failures, evaluate_specs

__all__ = ["validate_request"]

# assertion_kind -> the pack-field keys copied into `expected` — the same
# mapping cert_case_builder applies to registry rows.
_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("occurrence", ("occurrence",)),
    ("datatype", ("datatype",)),
    ("length", ("length_rule",)),
    ("mandatory", ("mandatory", "condition_text")),
    ("enum", ("enum_values",)),
    ("pattern", ("pattern_rule",)),
)


def _specs_for(api_entry: Mapping[str, Any]) -> list[Any]:
    specs = []
    for f in api_entry.get("fields", []):
        for kind, keys in _KINDS:
            payload = {k: f.get(k) for k in keys if f.get(k)}
            if payload.get(keys[0]):
                specs.append(SimpleNamespace(
                    assertion_kind=kind, expected=payload, field_path=f["path"]))
    return specs


def validate_request(api_entry: Mapping[str, Any], body: str | bytes, *,
                     codec) -> list[dict]:
    """The violations (FAILs only) of `body` against one merged pack API
    entry — each naming the failing path and the constraint that failed,
    in `cert_assertions.assertion_failures` shape."""
    outcomes = evaluate_specs(_specs_for(api_entry), request_body=body,
                              response_body=None, actual_code=None, codec=codec)
    return assertion_failures(outcomes)
