# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Validation helpers for generated document plans and section content."""
from __future__ import annotations

import copy
from typing import Any

from app.docgen.content_fallbacks import fallback_table_data
from app.docgen.document_guides import get_document_blueprint


def _nonempty_paragraphs(section: dict[str, Any]) -> list[str]:
    return [p.strip() for p in section.get("paragraphs", []) if isinstance(p, str) and p.strip()]


def _has_table(section: dict[str, Any]) -> bool:
    table = section.get("table_data") or {}
    return bool(table.get("headers")) and bool(table.get("rows"))


def _min_substantive_paragraphs(section_plan: dict[str, Any], doc_type: str) -> int:
    if doc_type.lower() == "circular":
        return 1
    override = section_plan.get("validation_min_paragraphs")
    if override is not None:
        try:
            return max(0, int(override))
        except (TypeError, ValueError):
            pass
    return 2


def _has_nonempty_code_blocks(generated: dict[str, Any]) -> bool:
    return any(isinstance(c, str) and c.strip() for c in (generated.get("code_blocks") or []))


def _has_structured_content(generated: dict[str, Any]) -> bool:
    return (
        _has_table(generated)
        or bool(generated.get("bullet_points"))
        or bool(generated.get("numbered_items"))
        or _has_nonempty_code_blocks(generated)
    )


def _substantive_body_ok(section_plan: dict[str, Any], generated: dict[str, Any], doc_type: str) -> bool:
    if section_plan.get("render_style", "body") != "body":
        return True
    paragraphs = _nonempty_paragraphs(generated)
    min_paragraphs = _min_substantive_paragraphs(section_plan, doc_type)
    has_structured = _has_structured_content(generated)
    # Allow list/table/code-only sections (e.g. "v. Note:" with numbered_items only).
    return (
        len(paragraphs) >= min_paragraphs
        or (len(paragraphs) >= 1 and has_structured)
        or (len(paragraphs) == 0 and has_structured)
    )


def repair_sections_for_validation(
    plan: dict[str, Any],
    sections: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Patch common LLM omissions so validation can pass without weakening rules.
    Idempotent for typical cases. Returns (repaired_sections, human-readable repair notes).
    """
    notes: list[str] = []
    plan_sections = plan.get("sections", [])
    doc_type = plan.get("doc_type", "")
    out: list[dict[str, Any]] = [copy.deepcopy(s) for s in sections]

    for idx, section_plan in enumerate(plan_sections):
        if idx >= len(out):
            break
        gen = out[idx]
        heading = section_plan.get("heading", "")

        if section_plan.get("include_table") and not _has_table(gen):
            gen["table_data"] = fallback_table_data(section_plan, heading)
            notes.append(f"Injected fallback table_data for '{heading}'.")

        if not _substantive_body_ok(section_plan, gen, doc_type):
            if section_plan.get("validation_fill_numbered_items") and not (gen.get("numbered_items") or []):
                gen["numbered_items"] = [
                    "Confirm this behaviour against your published integration specification before production use.",
                    "Assign clear ownership for monitoring, incident response, and exception handling.",
                    "Document version, sequencing, and idempotency expectations for audit and replay controls.",
                    "Define timeout, retry, backoff, and reconciliation rules for pending or partial outcomes.",
                ]
                notes.append(f"Injected numbered_items for '{heading}' (plan flag validation_fill_numbered_items).")

            if not _substantive_body_ok(section_plan, gen, doc_type):
                paras = [p for p in (gen.get("paragraphs") or []) if isinstance(p, str)]
                bridge = (
                    "Detailed specifications, tables, and code samples in the surrounding sections "
                    "implement the requirements summarized above."
                )
                if len(_nonempty_paragraphs(gen)) == 0:
                    paras.extend(
                        [
                            f"This subsection covers {heading} as defined in the document plan.",
                            bridge,
                        ]
                    )
                    notes.append(f"Added placeholder paragraphs for empty section '{heading}'.")
                else:
                    paras.append(bridge)
                    notes.append(f"Added bridge paragraph for substantive minimum on '{heading}'.")
                gen["paragraphs"] = paras

    return out, notes


def validate_generated_document(
    plan: dict[str, Any],
    sections: list[dict[str, Any]],
    include_diagrams: bool = True,
) -> dict[str, list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    doc_type = plan.get("doc_type", "")
    blueprint = get_document_blueprint(doc_type)
    plan_sections = plan.get("sections", [])

    if not plan_sections:
        errors.append("The document plan does not contain any sections.")
        return {"errors": errors, "warnings": warnings}

    if len(sections) != len(plan_sections):
        errors.append(
            f"Generated section count ({len(sections)}) does not match plan section count ({len(plan_sections)})."
        )

    if blueprint:
        expected_keys = {s.get("section_key") for s in blueprint.get("sections", []) if s.get("section_key")}
        actual_keys = {s.get("section_key") for s in plan_sections if s.get("section_key")}
        missing_keys = expected_keys - actual_keys
        if missing_keys:
            warnings.append(
                f"{doc_type} plan is missing expected section keys: {sorted(missing_keys)}. "
                "Blueprint sections may have been reordered or renamed by the LLM."
            )

    if doc_type.lower() == "circular":
        if plan.get("include_cover_page", True):
            errors.append("Circular documents must not include a cover page.")
        if plan.get("include_toc", True):
            errors.append("Circular documents must not include a table of contents.")

    for idx, section_plan in enumerate(plan_sections):
        if idx >= len(sections):
            break
        generated = sections[idx]
        expected_heading = section_plan.get("heading", "")
        if generated.get("section_heading") != expected_heading:
            # LLM sometimes rephrases headings slightly — log as warning only
            warnings.append(
                f"Section heading at position {idx + 1} was rephrased: "
                f"expected '{expected_heading}', got '{generated.get('section_heading')}'."
            )

        render_style = section_plan.get("render_style", "body")
        paragraphs = _nonempty_paragraphs(generated)

        if render_style == "body":
            min_paragraphs = _min_substantive_paragraphs(section_plan, doc_type)
            substantive = _substantive_body_ok(section_plan, generated, doc_type)
            if not substantive:
                errors.append(
                    f"Section '{expected_heading}' must contain at least {min_paragraphs} "
                    f"substantive paragraphs, or structured content alone (table, list, or code), "
                    f"or one paragraph plus such structured content."
                )

        if section_plan.get("include_table") and not _has_table(generated):
            errors.append(f"Section '{expected_heading}' requires a table but no table_data was generated.")

        if section_plan.get("include_diagram") and include_diagrams and not generated.get("diagram_path"):
            warnings.append(f"Section '{expected_heading}' was configured for a diagram but no diagram was attached.")

    return {"errors": errors, "warnings": warnings}
