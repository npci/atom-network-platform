# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Deterministic post-processor that runs after Writer, before Render.

Pure-Python pass that enforces invariants the LLM cannot reliably keep:

1. **Canonical API names** — substitute exact-cased canonical for any near-miss.
2. **Canonical error codes** — drop or substitute non-canonical codes.
3. **Pair-id integrity** — copy DETAILS+DESCRIPTION from canonical sibling.
4. **Step numbering** — `1. ... 2. ... 3. ...` with consistent newline separators.
5. **Status casing** — match the workbook's archetype convention.
6. **Failure terminus** — every Failure row's last step must end with the error
   code. If missing, append a canonical clause derived from the stub.
7. **Highlight rule** — auto-highlight rows whose response code is a "rare" code
   (one that appears <5% of the time in the corpus).
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

from app.excel_testcase_engine.observability import get_logger
from app.excel_testcase_engine.schemas.workbook_plan import (
    RenderedTestCase,
    SheetSpec,
    TestCaseStub,
    WorkbookPlan,
)

LOGGER = get_logger("network.agent.post")

_NEAR_MISS_RE = re.compile(r"\b(Req|Resp)([A-Za-z0-9]+)\b")


def _canonicalise_apis(text: str, api_allow: set[str]) -> str:
    """Fix casing-only near-misses against the provided API allow-list.

    BRD/TSD-only: `api_allow` is derived from the plan's declared APIs
    (TSD interface_spec + stubs' own `apis`). Non-listed tokens pass
    through unchanged — we don't second-guess what the BRD/TSD spelled.
    """
    lookup = {name.lower(): name for name in api_allow}

    def repl(match: re.Match[str]) -> str:
        token = match.group(0)
        canonical_name = lookup.get(token.lower())
        return canonical_name or token

    return _NEAR_MISS_RE.sub(repl, text)


def _normalise_steps(steps: str) -> str:
    """Coerce step lines to `1. ... 2. ...` with `\n` separators and trailing periods."""

    if not steps:
        return steps
    # Split on numbered list markers OR newlines.
    parts = re.split(r"\s*\n\s*", steps.strip())
    normalised: list[str] = []
    counter = 1
    for raw in parts:
        if not raw.strip():
            continue
        # Strip any leading "1.", "1)", "(1)", etc.
        body = re.sub(r"^\s*\(?\d+\)?[.)]\s*", "", raw).strip()
        if not body:
            continue
        if not body.endswith((".", "?", "!", '"', ")")):
            body += "."
        normalised.append(f"{counter}. {body}")
        counter += 1
    return "\n".join(normalised)


def _ensure_failure_terminus(steps: str, response_code: str, response_api: str) -> str:
    """If a Failure row's last step doesn't carry an error-code clause, append one.

    BRD/TSD-only: no error-code description lookup — the BRD is the sole
    source of code semantics. Response API defaults to the pack's declared
    response message for back-compat but callers pass the stub's actual
    response API when known.
    """
    if not response_code:
        return steps
    if f'"{response_code}"' in steps:
        return steps
    next_index = len(re.findall(r"^\d+\.", steps, flags=re.MULTILINE)) + 1
    appended = f'{next_index}. {response_api} is returned with error code "{response_code}".'
    return steps.rstrip() + "\n" + appended


def _adjust_status_casing(value: str, casing: str) -> str:
    if not value:
        return value
    if casing == "upper":
        return value.upper()
    return value.title()


def _canonicalise_status_word(steps: str, casing: str) -> str:
    target = "result-SUCCESS" if casing == "upper" else "result - SUCCESS"
    return re.sub(r"result\s*-\s*SUCCESS", target, steps, flags=re.IGNORECASE)


def _post_process_one(
    stub: TestCaseStub,
    api_allow: set[str],
    casing: str,
) -> RenderedTestCase:
    rendered = stub.rendered or RenderedTestCase(
        test_id=stub.test_id,
        details_block=f"API Involved: {stub.apis[0]}\nType : {stub.api_type}\nEntity Involved: {', '.join(stub.entities)}\n",
        description_block=f"To verify {stub.scenario_summary}.",
        steps_block="1. Initiate request.\n2. Receive response.",
    )
    details = _canonicalise_apis(rendered.details_block, api_allow)
    description = _canonicalise_apis(rendered.description_block, api_allow)
    steps = _canonicalise_apis(rendered.steps_block, api_allow)

    # BRD/TSD-only: response_code passes through verbatim. Empty is valid.
    response_code = stub.response_code or ""

    if stub.expected_status == "Failure":
        # Prefer the stub's declared Resp*, else the pack's default response
        # message (UPI: RespTransfer), else a neutral phrase.
        from app.excel_testcase_engine import domain_vocab
        response_api = next((a for a in stub.apis if a.startswith("Resp")),
                            domain_vocab.default_response_api() or "The response")
        steps = _ensure_failure_terminus(steps, response_code, response_api)
    if stub.expected_status == "Success":
        steps = _canonicalise_status_word(steps, casing)

    steps = _normalise_steps(steps)
    return RenderedTestCase(
        test_id=stub.test_id,
        details_block=details,
        description_block=description,
        steps_block=steps,
    )


def _enforce_pair_integrity(sheet: SheetSpec) -> int:
    """Copy DETAILS+DESCRIPTION from the canonical pair member to siblings."""

    drift_count = 0
    canonical: dict[str, RenderedTestCase] = {}
    for tc in sheet.test_cases:
        if not tc.pair_id or tc.rendered is None:
            continue
        if tc.pair_id not in canonical:
            canonical[tc.pair_id] = tc.rendered
        else:
            canon = canonical[tc.pair_id]
            if (
                tc.rendered.details_block != canon.details_block
                or tc.rendered.description_block != canon.description_block
            ):
                drift_count += 1
                tc.rendered = tc.rendered.model_copy(
                    update={
                        "details_block": canon.details_block,
                        "description_block": canon.description_block,
                    }
                )
    return drift_count


def _highlight_rare(plan: WorkbookPlan, threshold: float = 0.05) -> int:
    counts: dict[str, int] = defaultdict(int)
    total = 0
    for sheet in plan.sheets:
        for tc in sheet.test_cases:
            if tc.response_code:
                counts[tc.response_code] += 1
                total += 1
    if total == 0:
        return 0
    rare = {code for code, n in counts.items() if (n / total) < threshold and code not in {"00", ""}}
    highlighted = 0
    for sheet in plan.sheets:
        for tc in sheet.test_cases:
            if tc.response_code in rare and not tc.highlight:
                tc.highlight = True
                highlighted += 1
    return highlighted


def _ensure_unique_test_ids(plan: WorkbookPlan) -> int:
    """Renumber duplicate IDs within each sheet."""

    fixed = 0
    for sheet in plan.sheets:
        seen: set[str] = set()
        suffix_pattern = re.compile(r"^(.*?)(\d+)$")
        used_suffixes: dict[str, set[int]] = defaultdict(set)
        for tc in sheet.test_cases:
            match = suffix_pattern.match(tc.test_id)
            if match:
                used_suffixes[match.group(1)].add(int(match.group(2)))
        for tc in sheet.test_cases:
            if tc.test_id not in seen:
                seen.add(tc.test_id)
                continue
            match = suffix_pattern.match(tc.test_id)
            if not match:
                continue
            prefix, suffix = match.group(1), int(match.group(2))
            new_suffix = suffix + 1
            while new_suffix in used_suffixes[prefix]:
                new_suffix += 1
            new_id = f"{prefix}{new_suffix:0{len(match.group(2))}d}"
            tc.test_id = new_id
            seen.add(new_id)
            used_suffixes[prefix].add(new_suffix)
            fixed += 1
            if tc.rendered:
                tc.rendered = tc.rendered.model_copy(update={"test_id": new_id})
    return fixed


def post_process(plan: WorkbookPlan) -> WorkbookPlan:
    """Apply every deterministic quality-fix to the plan in place.

    BRD/TSD-only: no canonical UPI catalog. The API allow-list is
    reconstructed from what the plan itself declares (TSD-driven).
    """
    # Allow-list = every API named on any stub. Used only to fix casing
    # near-misses (`reqtransfer` → `ReqTransfer`); unknown tokens pass through.
    api_allow: set[str] = set()
    for sheet in plan.sheets:
        for tc in sheet.test_cases:
            api_allow.update(tc.apis)

    casing = (plan.global_conventions.get("status_casing") or "upper").lower()

    for sheet in plan.sheets:
        for tc in sheet.test_cases:
            tc.rendered = _post_process_one(tc, api_allow, casing)
            tc.expected_status = _adjust_status_casing(tc.expected_status, casing)  # type: ignore[assignment]

    drift_fixed = sum(_enforce_pair_integrity(sheet) for sheet in plan.sheets)
    duplicates_fixed = _ensure_unique_test_ids(plan)
    highlighted = _highlight_rare(plan)

    # Recompute coverage_audit so it always matches actual rendered counts.
    audit: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for sheet in plan.sheets:
        for tc in sheet.test_cases:
            for api in tc.apis:
                if api.startswith("Req"):
                    audit[api][tc.coverage_tag] += 1
    plan.coverage_audit = {api: dict(counts) for api, counts in audit.items()}

    LOGGER.info(
        "postprocessor.done",
        pair_drift_fixed=drift_fixed,
        duplicates_fixed=duplicates_fixed,
        highlighted=highlighted,
    )
    return plan
