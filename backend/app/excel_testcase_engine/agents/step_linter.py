# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Per-test-case TEST STEPS linter.

Catches the failure modes that hurt step accuracy most:

1.  Steps reference an API not present in DETAILS (or not in canonical list).
2.  Failure row's last step missing the `error code "<CODE>" (...).` clause.
3.  Success row's last step missing `result-SUCCESS` / `with result - SUCCESS`.
4.  Step numbering not strictly sequential ``1. 2. 3.``.
5.  Cred-block / UMN / UUID references that don't match the API type.
6.  Steps that mention an entity not listed in DETAILS' Entity Involved.

The orchestrator runs ``lint_plan`` after the post-processor; any stub with a
non-empty issues list is re-prompted by the writer with the issues attached.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.excel_testcase_engine.observability import get_logger
from app.excel_testcase_engine.schemas.workbook_plan import TestCaseStub, WorkbookPlan

LOGGER = get_logger("network.agent.step_linter")

_API_REF_RE = re.compile(r"\b(?:Req|Resp)[A-Za-z0-9]+\b")
_CODE_REF_RE = re.compile(r'error code\s+"([^"]+)"')
_STEP_PREFIX_RE = re.compile(r"^\s*(\d+)\.\s")
# DETAILS uses both `API Involved:` and `API Involved :` and the rare `API Invovled :`
# typo retained from legacy packs. Be tolerant.
_DETAILS_API_RE = re.compile(
    r"^API\s+Inv[ao]?lved\s*:\s*(.+?)(?:\nType\s*:|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
_DETAILS_ENTITY_RE = re.compile(
    r"^Entity\s+Involved\s*:\s*(.+?)$",
    re.MULTILINE | re.IGNORECASE,
)


@dataclass
class StepIssue:
    """One linter finding for a single test case."""

    test_id: str
    code: str            # short tag, e.g. "missing_failure_code"
    message: str         # human-readable description
    fix_hint: str        # short suggestion for the writer


@dataclass
class StepLintReport:
    """Aggregated linter report for a plan."""

    issues_by_test: dict[str, list[StepIssue]] = field(default_factory=dict)

    @property
    def is_clean(self) -> bool:
        return not self.issues_by_test

    def for_stub(self, test_id: str) -> list[StepIssue]:
        return self.issues_by_test.get(test_id, [])


def _details_apis(details: str) -> list[str]:
    match = _DETAILS_API_RE.search(details)
    if not match:
        return []
    blob = match.group(1)
    return [tok for tok in _API_REF_RE.findall(blob)]


def _details_entities(details: str) -> list[str]:
    match = _DETAILS_ENTITY_RE.search(details)
    if not match:
        return []
    return [e.strip() for e in match.group(1).split(",") if e.strip()]


def _check_step_numbering(steps: str, issues: list[StepIssue], test_id: str) -> None:
    expected = 1
    for line in steps.splitlines():
        match = _STEP_PREFIX_RE.match(line)
        if not match:
            continue
        actual = int(match.group(1))
        if actual != expected:
            issues.append(StepIssue(
                test_id=test_id,
                code="step_numbering",
                message=f"Step numbering jumps to {actual} (expected {expected}).",
                fix_hint=f"Renumber steps strictly 1, 2, 3, ... starting at 1.",
            ))
            return
        expected += 1


def _check_apis_in_steps(steps: str, details_apis: list[str], api_allow: set[str], issues: list[StepIssue], test_id: str) -> None:
    """Steps may reference any API in the caller-supplied allow-list.

    BRD/TSD-only: `api_allow` is the union of the TSD interface_spec APIs
    and every API declared on any stub in the plan. Underscore-suffixed
    sub-forms (`ReqTransfer_Debit`) are allowed when the base (`ReqTransfer`) is in
    the allow-list. Tokens outside the allow-list are flagged.
    """
    seen = set(_API_REF_RE.findall(steps))
    sub_tokens = set(re.findall(r"\b(?:Req|Resp)[A-Za-z0-9]+_[A-Za-z]+\b", steps))
    for sub in sub_tokens:
        base = sub.split("_", 1)[0]
        if base in api_allow:
            seen.discard(sub)
            continue
        issues.append(StepIssue(
            test_id=test_id,
            code="invalid_api_in_steps",
            message=f"Steps reference API `{sub}` whose base `{base}` isn't declared by the TSD or on any stub.",
            fix_hint=f"Use an API declared in the TSD Interface Specification (allow-list: {sorted(api_allow)[:8]}...).",
        ))

    for api in seen:
        if api not in api_allow:
            issues.append(StepIssue(
                test_id=test_id,
                code="invalid_api_in_steps",
                message=f"Steps reference API `{api}` which isn't in the TSD interface_spec or on any stub.",
                fix_hint=f"Use one of the DETAILS APIs ({', '.join(sorted(details_apis)) or 'none listed'}) or an API from the TSD Interface Specification.",
            ))
            continue
        if details_apis and api not in details_apis:
            issues.append(StepIssue(
                test_id=test_id,
                code="api_not_in_details",
                message=f"Steps reference `{api}` (not listed in DETAILS' API Involved).",
                fix_hint=f"Either add `{api}` to DETAILS or drop it from steps.",
            ))


def _check_failure_terminus(stub: TestCaseStub, steps: str, code_allow: set[str], issues: list[StepIssue]) -> None:
    if (stub.expected_status or "").lower() != "failure":
        return
    if not stub.response_code:
        return
    matches = _CODE_REF_RE.findall(steps)
    if not matches:
        issues.append(StepIssue(
            test_id=stub.test_id,
            code="missing_failure_code",
            message="Failure row final step lacks an `error code \"<CODE>\"` clause.",
            fix_hint=f"Append `with error code \"{stub.response_code}\".` to the final step.",
        ))
        return
    if stub.response_code not in matches:
        issues.append(StepIssue(
            test_id=stub.test_id,
            code="failure_code_mismatch",
            message=f"Steps reference {matches} but stub.response_code is `{stub.response_code}`.",
            fix_hint=f"Use `{stub.response_code}` consistently in the terminus clause.",
        ))
    for code in matches:
        # BRD/TSD-only: code_allow comes from the TSD/BRD extraction plus the
        # stubs' own response_code values. A code cited in steps that isn't
        # in that set is a warning, not a critical — the writer may have
        # picked up a BRD-adjacent code the extractor missed.
        if code and code not in code_allow:
            issues.append(StepIssue(
                test_id=stub.test_id,
                code="invalid_error_code",
                message=f"Steps reference error code `{code}` which isn't in the BRD/TSD extraction.",
                fix_hint="Confirm the code appears in the BRD's error-handling text, or replace it with a code that does.",
            ))


def _check_success_terminus(stub: TestCaseStub, steps: str, issues: list[StepIssue]) -> None:
    if (stub.expected_status or "").lower() != "success":
        return
    last_line = next((line for line in reversed(steps.splitlines()) if line.strip()), "")
    if "result-success" not in last_line.lower() and "result - success" not in last_line.lower():
        issues.append(StepIssue(
            test_id=stub.test_id,
            code="success_terminus_missing",
            message="Success row final step does not end with `result-SUCCESS`.",
            fix_hint="Rewrite the final step to terminate with `result-SUCCESS` (or `with result - SUCCESS`).",
        ))


def _check_entity_consistency(stub: TestCaseStub, details: str, steps: str, issues: list[StepIssue]) -> None:
    entities = _details_entities(details)
    if not entities:
        return
    # Detect any role-like phrase from steps that's NOT in DETAILS' entity list.
    from app.excel_testcase_engine import domain_vocab
    suspect_phrases = domain_vocab.suspect_role_phrases()
    exempt = domain_vocab.exempt_role_phrase()
    referenced = [phrase for phrase in suspect_phrases if phrase in steps]
    missing = [phrase for phrase in referenced if phrase not in entities and phrase != exempt]
    if missing:
        issues.append(StepIssue(
            test_id=stub.test_id,
            code="entity_not_in_details",
            message=f"Steps mention {missing} but DETAILS Entity Involved is `{', '.join(entities)}`.",
            fix_hint=f"Add the entities {missing} to DETAILS or rephrase steps to use only listed entities.",
        ))


def _check_cred_block_consistency(stub: TestCaseStub, steps: str, issues: list[StepIssue]) -> None:
    """Steps that reference cred blocks must match the stub's API type."""

    from app.excel_testcase_engine import domain_vocab
    subtypes = domain_vocab.cred_subtypes()
    if not subtypes:
        # The active domain declares no credential-block concept — nothing to lint.
        return
    text = steps.lower()
    if (stub.api_type or "").upper() in {"PIN", "AUTH"}:
        # Should reference a cred subtype.
        tokens = [t.strip().lower() for t in subtypes.split("/") if t.strip()] + ["cred block"]
        if not any(token in text for token in tokens):
            issues.append(StepIssue(
                test_id=stub.test_id,
                code="cred_block_missing",
                message=f"Type `{stub.api_type}` typically references a cred block, but steps don't.",
                fix_hint=f"Mention the cred subtype ({subtypes}) in the auth step.",
            ))


def lint_stub(
    stub: TestCaseStub, api_allow: set[str], code_allow: set[str],
) -> list[StepIssue]:
    """Lint one stub's rendered blocks.

    `api_allow` and `code_allow` are the full allow-lists for this run. In
    BRD/TSD-only mode callers derive them from the TSD Interface Spec
    (APIs) + BRD error-handling text (codes) + whatever the stubs declare.
    """
    if stub.rendered is None:
        return [StepIssue(
            test_id=stub.test_id,
            code="not_rendered",
            message="Stub has no rendered content.",
            fix_hint="Writer must produce DETAILS / DESCRIPTION / TEST STEPS for this stub.",
        )]
    issues: list[StepIssue] = []
    details = stub.rendered.details_block or ""
    steps = stub.rendered.steps_block or ""
    details_apis = _details_apis(details)

    _check_step_numbering(steps, issues, stub.test_id)
    _check_apis_in_steps(steps, details_apis, api_allow, issues, stub.test_id)
    _check_failure_terminus(stub, steps, code_allow, issues)
    _check_success_terminus(stub, steps, issues)
    _check_entity_consistency(stub, details, steps, issues)
    _check_cred_block_consistency(stub, steps, issues)

    return issues


def lint_plan(
    plan: WorkbookPlan,
    extra_apis: frozenset[str] | set[str] = frozenset(),
    extra_codes: frozenset[str] | set[str] = frozenset(),
) -> StepLintReport:
    """Lint every rendered stub in the plan.

    BRD/TSD-only: `extra_apis` and `extra_codes` ARE the allow-lists (no
    canonical UPI union). Callers derive them from the TSD interface_spec
    and BRD error-handling text.
    """
    api_allow = set(extra_apis)
    code_allow = set(extra_codes)
    # Backfill from what the plan itself declares — a stub's own response_code
    # and apis should never be lint-flagged as "unknown".
    for sheet in plan.sheets:
        for tc in sheet.test_cases:
            api_allow.update(tc.apis)
            if tc.response_code:
                code_allow.add(tc.response_code)
    report = StepLintReport()
    for sheet in plan.sheets:
        for tc in sheet.test_cases:
            issues = lint_stub(tc, api_allow, code_allow)
            if issues:
                report.issues_by_test[tc.test_id] = issues
    if report.issues_by_test:
        LOGGER.info(
            "step_linter.issues",
            stubs=len(report.issues_by_test),
            total=sum(len(v) for v in report.issues_by_test.values()),
        )
    return report
