# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Auxiliary exporters: Markdown and DOCX views of a generated WorkbookPlan.

These run from the in-memory plan, so they're 100% deterministic and never need
to re-open the .xlsx. They give the frontend the same content in three formats.
"""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path

from app.excel_testcase_engine import domain_vocab as _domain_vocab
from app.excel_testcase_engine.schemas.workbook_plan import WorkbookPlan

_LOG = logging.getLogger("excel_engine.exporters")


def to_markdown(plan: WorkbookPlan) -> str:
    """Render a plan to a Markdown brief (per-sheet sections, per-case headings)."""

    lines: list[str] = []
    feature = plan.global_conventions.get("feature_name", plan.filename)
    lines.append(f"# {feature}")
    lines.append("")
    lines.append(f"_Archetype {plan.archetype} | {sum(len(s.test_cases) for s in plan.sheets)} test cases | {len(plan.sheets)} sheets_")
    lines.append("")
    lines.append("## Coverage audit")
    lines.append("")
    if plan.coverage_audit:
        lines.append("| API | Tag | Count |")
        lines.append("|-----|-----|------:|")
        for api, tags in sorted(plan.coverage_audit.items()):
            for tag, count in sorted(tags.items()):
                lines.append(f"| `{api}` | {tag} | {count} |")
    else:
        lines.append("(no test cases)")
    lines.append("")

    for sheet in plan.sheets:
        if not sheet.test_cases:
            continue
        lines.append(f"## {sheet.name} ({sheet.layout})")
        lines.append("")
        for tc in sheet.test_cases:
            lines.append(f"### {tc.test_id} — {tc.expected_status}{' (highlighted)' if tc.highlight else ''}")
            lines.append("")
            lines.append("**DETAILS**")
            lines.append("```")
            lines.append((tc.rendered.details_block if tc.rendered else "").rstrip())
            lines.append("```")
            lines.append("**DESCRIPTION**")
            lines.append("")
            lines.append((tc.rendered.description_block if tc.rendered else "").rstrip())
            lines.append("")
            lines.append("**TEST STEPS**")
            lines.append("")
            lines.append("```")
            lines.append((tc.rendered.steps_block if tc.rendered else "").rstrip())
            lines.append("```")
            if tc.response_code and tc.response_code != "00":
                lines.append(f"_Response code: `{tc.response_code}`_")
            lines.append("")
    return "\n".join(lines)


def to_docx_bytes(plan: WorkbookPlan) -> bytes:
    """Render a plan to a minimal DOCX (no python-docx dependency).

    We emit a tiny office-open-xml package with one document that mirrors the
    Markdown structure. Word, Pages, and Google Docs all open it. This avoids a
    new heavy dependency for a frontend convenience export.
    """

    import zipfile
    from xml.sax.saxutils import escape

    paragraphs: list[str] = []

    def heading(text: str, level: int) -> None:
        paragraphs.append(
            f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr>'
            f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'
        )

    def body(text: str, *, mono: bool = False) -> None:
        for line in (text or "").split("\n"):
            run_props = '<w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/></w:rPr>' if mono else ""
            paragraphs.append(
                f'<w:p><w:r>{run_props}<w:t xml:space="preserve">{escape(line)}</w:t></w:r></w:p>'
            )

    feature = plan.global_conventions.get("feature_name", plan.filename)
    heading(feature, 1)
    body(f"Archetype {plan.archetype}, {sum(len(s.test_cases) for s in plan.sheets)} test cases.")

    for sheet in plan.sheets:
        if not sheet.test_cases:
            continue
        heading(sheet.name, 2)
        for tc in sheet.test_cases:
            heading(f"{tc.test_id} — {tc.expected_status}", 3)
            body("DETAILS")
            body(tc.rendered.details_block.rstrip() if tc.rendered else "", mono=True)
            body("DESCRIPTION")
            body(tc.rendered.description_block.rstrip() if tc.rendered else "")
            body("TEST STEPS")
            body(tc.rendered.steps_block.rstrip() if tc.rendered else "", mono=True)

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body>' + "".join(paragraphs) + '</w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '</Relationships>'
    )

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


_STATUS_CANON = {
    "success": "Success",
    "failure": "Failure",
    "deemed":  "Deemed",
    "partial": "Partial",
}


def _canon_status(value: str) -> str:
    """Force expected_status back to Title Case for the simulator contract.

    The post_processor mutates `expected_status` to archetype-specific casing
    (UPPERCASE for A/B, Title Case for C). The simulator contract format
    (matched against `cert_simulator_contract/examples/*.json`) wants Title
    Case unconditionally, so we coerce here regardless of archetype casing.
    """

    if not isinstance(value, str):
        return value
    return _STATUS_CANON.get(value.lower().strip(), value)


def to_simulator_contract(
    plan: WorkbookPlan,
    *,
    change_request_id: str = "",
    feature_name: str | None = None,
) -> dict:
    """Project a WorkbookPlan into the cert-simulator JSON contract.

    Target shape (see knowledge_base examples
    `cert_simulator_contract/examples/{existing_api_only,
    new_api_low_confidence, new_api_with_flow_def}.json`):

        {
          "feature_name": str,
          "change_request_id": str,
          "test_cases": [
            {
              "test_id", "apis", "api_type", "entities",
              "approval_type", "payer_handle", "payee_handle",
              "scenario_summary", "expected_status",
              "response_code"?,                # omitted when empty
              "coverage_tag", "scope",
              "txn_initiated_by", "psp_as",
              "rendered": {test_id, details_block, description_block, steps_block}
            }, ...
          ]
        }

    NOT mirrored from the internal WorkbookPlan: archetype, filename, sheet
    grouping, coverage_audit, global_conventions, pair_id, highlight. These
    are engine-internal scaffolding the cert simulator does not consume.

    `flow_definitions` is intentionally omitted today — the planner does not
    yet emit new-API flow metadata. When it does, surface it under the
    optional top-level `flow_definitions` key.
    """

    feature = (
        feature_name
        or plan.global_conventions.get("feature_name")
        or plan.filename
        or _domain_vocab.feature_name_default()
    )

    cases: list[dict] = []
    for sheet in (plan.sheets or []):
        for tc in (sheet.test_cases or []):
            try:
                entry: dict = {
                    "test_id": str(getattr(tc, "test_id", "") or ""),
                    "apis": list(getattr(tc, "apis", []) or []),
                    "api_type": str(getattr(tc, "api_type", "") or ""),
                    "entities": list(getattr(tc, "entities", []) or []),
                    "approval_type": str(getattr(tc, "approval_type", "") or ""),
                    "payer_handle": str(getattr(tc, "payer_handle", "") or ""),
                    "payee_handle": str(getattr(tc, "payee_handle", "") or ""),
                    "scenario_summary": str(getattr(tc, "scenario_summary", "") or ""),
                    "expected_status": _canon_status(
                        str(getattr(tc, "expected_status", "") or "Success")
                    ),
                    # Extra key (not required by the cert-simulator contract
                    # validator, only checks required-key presence): records
                    # which role sheet this case came from so downstream
                    # consumers (UI table view, cert-agent bucketing) can
                    # group cases by sheet without re-parsing the .xlsx.
                    "sheet": str(getattr(sheet, "name", "") or ""),
                }
                # Only include response_code when set — the example fixtures
                # omit the key entirely on Deemed cases (e.g. PR_99).
                response_code = str(getattr(tc, "response_code", "") or "")
                if response_code:
                    entry["response_code"] = response_code
                entry["coverage_tag"] = str(getattr(tc, "coverage_tag", "happy_path") or "happy_path")
                # Falls back to the ACTIVE PACK's scope label, not a hardcoded
                # payments version — this dict is the cert artifact, so a wrong
                # label ships downstream as fact.
                from app.core.domain.registry import prompt_block as _pb
                entry["scope"] = str(getattr(tc, "scope", "") or "") or _pb("test_case_scope", "")
                entry["txn_initiated_by"] = str(getattr(tc, "txn_initiated_by", "Bank") or "Bank")
                entry["psp_as"] = str(getattr(tc, "psp_as", "") or "")

                # `rendered` is the only nested object — synthesise empty
                # blocks if the writer never attached one (e.g. placeholder
                # case from a failed batch). Better to ship the test case
                # with empty prose than to drop it from the contract.
                rendered = getattr(tc, "rendered", None)
                if rendered is not None:
                    entry["rendered"] = {
                        "test_id": str(getattr(rendered, "test_id", entry["test_id"]) or entry["test_id"]),
                        "details_block": str(getattr(rendered, "details_block", "") or ""),
                        "description_block": str(getattr(rendered, "description_block", "") or ""),
                        "steps_block": str(getattr(rendered, "steps_block", "") or ""),
                    }
                else:
                    entry["rendered"] = {
                        "test_id": entry["test_id"],
                        "details_block": "",
                        "description_block": "",
                        "steps_block": "",
                    }
                cases.append(entry)
            except Exception as exc:  # noqa: BLE001
                # Per-case isolation: a malformed test case must not lose
                # the rest of the contract. Log and skip — the .xlsx is
                # the source of truth, the JSON is a downstream contract.
                _LOG.warning(
                    "exporter.skip_malformed_case test_id=%s error=%r",
                    getattr(tc, "test_id", "?"), exc,
                )

    return {
        "feature_name": str(feature),
        "change_request_id": str(change_request_id or ""),
        "test_cases": cases,
    }


_REQUIRED_TOP_KEYS = ("feature_name", "change_request_id", "test_cases")
_REQUIRED_CASE_KEYS = (
    "test_id", "apis", "api_type", "entities", "approval_type",
    "payer_handle", "payee_handle", "scenario_summary",
    "expected_status", "coverage_tag", "scope",
    "txn_initiated_by", "psp_as", "rendered",
)
_REQUIRED_RENDERED_KEYS = ("test_id", "details_block", "description_block", "steps_block")


def _validate_simulator_contract(payload: dict) -> None:
    """Raise if the payload does not match the cert-simulator contract.

    Run before every JSON write so a shape regression fails fast and the
    retry path can attempt to re-derive the payload (rather than silently
    shipping a malformed companion to downstream simulator tooling).
    """

    if not isinstance(payload, dict):
        raise ValueError(f"payload must be a dict, got {type(payload).__name__}")
    for k in _REQUIRED_TOP_KEYS:
        if k not in payload:
            raise ValueError(f"missing top-level key: {k!r}")
    cases = payload["test_cases"]
    if not isinstance(cases, list):
        raise ValueError("test_cases must be a list")
    for idx, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"test_cases[{idx}] must be a dict")
        for k in _REQUIRED_CASE_KEYS:
            if k not in case:
                raise ValueError(f"test_cases[{idx}] missing key: {k!r}")
        rendered = case["rendered"]
        if not isinstance(rendered, dict):
            raise ValueError(f"test_cases[{idx}].rendered must be a dict")
        for k in _REQUIRED_RENDERED_KEYS:
            if k not in rendered:
                raise ValueError(f"test_cases[{idx}].rendered missing key: {k!r}")


def write_companions(
    plan: WorkbookPlan,
    xlsx_path: Path,
    *,
    change_request_id: str = "",
    feature_name: str | None = None,
) -> dict[str, Path]:
    """Write `<base>.md`, `<base>.docx`, and `<base>.json` next to the xlsx.

    Each companion is written INDEPENDENTLY — a failure on one (e.g. JSON
    schema mismatch, disk error on the docx) must not lose the others.
    The xlsx is the source of truth for the user-facing test cases sheet
    and is rendered upstream of this call; this function only adds
    convenience views.

    The .json companion follows the cert-simulator contract format — a flat
    `test_cases` list keyed by `feature_name` and `change_request_id`,
    matching the three reference fixtures under
    `knowledge_base/cert_simulator_contract/examples/`. JSON write is
    retried up to 3 times (with shape validation between attempts) so a
    transient I/O hiccup or a recoverable shape glitch can self-heal
    without dropping the contract.

    Returns a dict mapping artifact key → Path for whichever companions
    succeeded. Callers MUST handle missing keys gracefully.
    """

    import json as _json
    import time

    base = xlsx_path.with_suffix("")
    md_path = base.with_suffix(".md")
    docx_path = base.with_suffix(".docx")
    json_path = base.with_suffix(".json")

    out: dict[str, Path] = {}

    # ---- Markdown companion (independent) ---------------------------------
    try:
        md_path.write_text(to_markdown(plan), encoding="utf-8")
        out["md"] = md_path
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("companion.md_failed path=%s error=%r", md_path, exc)

    # ---- DOCX companion (independent) -------------------------------------
    try:
        docx_path.write_bytes(to_docx_bytes(plan))
        out["docx"] = docx_path
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("companion.docx_failed path=%s error=%r", docx_path, exc)

    # ---- JSON simulator-contract companion (with retry) -------------------
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            payload = to_simulator_contract(
                plan,
                change_request_id=change_request_id,
                feature_name=feature_name,
            )
            _validate_simulator_contract(payload)
            json_path.write_text(
                _json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            out["json"] = json_path
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            _LOG.warning(
                "companion.json_failed attempt=%d path=%s error=%r",
                attempt, json_path, exc,
            )
            # Brief backoff between attempts (50ms, 150ms) — covers
            # transient FS contention; deterministic shape errors will
            # exhaust the budget and fall through to the warning below.
            time.sleep(0.05 * (attempt + 1) ** 2)
    else:
        _LOG.warning(
            "companion.json_giving_up path=%s last_error=%r",
            json_path, last_err,
        )

    return out
