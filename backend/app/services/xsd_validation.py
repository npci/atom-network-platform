# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Deterministic XML-block validation for generated docs (accuracy S6).

A TSD / Product-Kit legitimately contains two kinds of ```xml block:
  - SCHEMA blocks (root ``<xs:schema>`` / ``<schema ...XMLSchema>``) — the doc must
    REPRODUCE an approved schema, never re-author it. Checked by whitespace-normalised
    substring match against the approved schema files.
  - INSTANCE blocks (sample request/response messages) — must VALIDATE against the
    approved XSD set (so the doc doesn't invent elements). Checked via ``xmlschema``.

The earlier plan's "every XML block is an excerpt of a schema" predicate was wrong:
instance documents validate AGAINST a schema, they are never substrings OF it. This
splits the two correctly.

Advisory + FAIL-OPEN: any parse/schema-build/namespace-resolution error yields NO
finding (we never block a doc because the validator couldn't construct the schema).
Returns a list of human-readable finding strings (empty == clean).
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_XML_BLOCK = re.compile(r"```xml\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_WS = re.compile(r"\s+")


def extract_xml_blocks(markdown: str) -> list[str]:
    """Every ```xml fenced block body in the markdown."""
    if not markdown:
        return []
    return [m.group(1).strip() for m in _XML_BLOCK.finditer(markdown) if m.group(1).strip()]


def _is_schema_block(xml: str) -> bool:
    head = xml.lstrip()[:400].lower()
    return ("xs:schema" in head or "xsd:schema" in head
            or ("<schema" in head and "xmlschema" in head))


def _norm(s: str) -> str:
    return _WS.sub(" ", s).strip()


def validate_xml_blocks(markdown: str, approved_schemas: list[str]) -> list[str]:
    """Validate the doc's XML blocks against the approved schemas. '' approved set
    (no XSDs yet) → no findings (nothing to check against)."""
    blocks = extract_xml_blocks(markdown)
    if not blocks or not approved_schemas:
        return []

    findings: list[str] = []
    norm_schemas = [_norm(s) for s in approved_schemas if s and s.strip()]

    # Build instance validators once (fail-open: skip any schema that won't build).
    built = []
    try:
        import xmlschema
        for s in approved_schemas:
            if not (s and s.strip()):
                continue
            try:
                built.append(xmlschema.XMLSchema(s))
            except Exception as e:  # noqa: BLE001 — unbuildable (imports/namespace) → skip
                logger.debug("xsd_validation: schema did not build (%s) — skipping", e)
    except Exception as e:  # noqa: BLE001 — xmlschema unavailable
        logger.debug("xsd_validation: xmlschema unavailable (%s)", e)

    for i, block in enumerate(blocks):
        if _is_schema_block(block):
            # Must reproduce an approved schema region (whitespace-normalised).
            nb = _norm(block)
            if nb and not any(nb in ns for ns in norm_schemas):
                findings.append(
                    f"XML block #{i + 1} is a SCHEMA that does not match any approved XSD "
                    "(reproduce the approved schema verbatim; do not author new schema)."
                )
        elif built:
            # Instance message — valid if it validates against ANY approved schema.
            ok = False
            well_formed = True
            for sch in built:
                try:
                    if sch.is_valid(block):
                        ok = True
                        break
                except Exception:  # noqa: BLE001 — not parseable against this schema
                    continue
            if not ok:
                # Only flag a well-formed instance that matched no schema (fail-open on
                # parse errors so a malformed snippet isn't double-reported here).
                try:
                    import xml.etree.ElementTree as ET
                    ET.fromstring(block)
                except Exception:  # noqa: BLE001
                    well_formed = False
                if well_formed:
                    findings.append(
                        f"XML block #{i + 1} is a sample message that does not validate against "
                        "any approved XSD (it may reference elements not in the decided schema)."
                    )
    return findings
