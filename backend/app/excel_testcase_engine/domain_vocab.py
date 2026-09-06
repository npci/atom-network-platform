# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Domain vocabulary for the test-case engine, resolved from the active pack.

One module so the engine's defaults (feature name, role sheets, default APIs,
acronym stop-lists, linter phrases) derive from ONE source instead of the
half-dozen hardcoded UPI copies they used to be. Every function degrades
honestly when the pack declares nothing: empty lists / empty strings, never
another domain's vocabulary.

Wire values are NOT here on purpose: `workbook_plan.TestCaseStub.
txn_initiated_by` stays "Bank"/"NPCI" (cert-agent matches those exactly);
these helpers cover display and scaffolding only.
"""
from __future__ import annotations

from app.core.domain.contract import cert_vocabulary_of, domain_acronyms_of, participants_of
from app.core.domain.registry import get_active_pack, prompt_block

# Structural tokens every error-code-shaped scan must skip regardless of
# domain — platform vocabulary, not pack content.
_STRUCTURAL_STOPWORDS = frozenset({"API", "TSD", "BRD", "XSD", "REQ", "RESP", "NOTE"})


def _csv_block(name: str) -> list[str]:
    raw = prompt_block(name, "")
    return [p.strip() for p in raw.split(",") if p.strip()] if raw else []


def feature_name_default() -> str:
    """Workbook/feature-name fallback (UPI: "UPI Test Cases")."""
    return f"{prompt_block('domain_name', 'Feature')} Test Cases"


def default_apis() -> list[str]:
    """The domain's canonical request/response pair for placeholders and
    engine defaults (UPI: ReqTransfer, RespTransfer). Empty when undeclared."""
    return _csv_block("default_apis")


def default_response_api() -> str:
    """The response half of `default_apis` (UPI: RespTransfer); "" when none."""
    return next((a for a in default_apis() if a.startswith("Resp")), "")


def party_labels() -> list[str]:
    """Display labels of the pack's cert parties, in declaration order."""
    return [lbl for _k, lbl in cert_vocabulary_of(get_active_pack()).parties()]


def role_sheet_names() -> list[str]:
    """Role-sheet names for the workbook: the pack's `workbook_role_sheets`
    block when declared (UPI's official packs use short forms — "Remitter",
    not "Remitter Bank"), else the cert-party labels."""
    return _csv_block("workbook_role_sheets") or party_labels()


def summary_role_headers() -> list[str]:
    """Per-role column headers on the summary sheet (UPI: Remitter,
    Beneficiary, Payer, Payee), falling back to the cert-party labels."""
    return _csv_block("summary_role_headers") or party_labels()


def scope_titles() -> list[str]:
    """Distinct certification scopes, Title-cased for display
    (UPI: Acquirer, Issuer)."""
    out: list[str] = []
    for scopes in cert_vocabulary_of(get_active_pack()).role_scopes.values():
        for sc in scopes:
            t = sc.title()
            if t not in out:
                out.append(t)
    return out


def error_code_stopwords() -> frozenset[str]:
    """Tokens an error-code-shaped scan must not mistake for codes: the
    platform's structural tokens plus the pack's `domain_acronyms`."""
    return _STRUCTURAL_STOPWORDS | set(domain_acronyms_of(get_active_pack()))


def scope_sheet_code_examples() -> list[tuple[str, str]]:
    """(code, description) example rows for the scope sheet's response-code
    table, from the `scope_sheet_code_examples` block ("CODE|desc" per line).
    Empty when the pack declares none — only the universal success row shows."""
    raw = prompt_block("scope_sheet_code_examples", "")
    out: list[tuple[str, str]] = []
    for line in raw.splitlines():
        code, _, desc = line.partition("|")
        if code.strip() and desc.strip():
            out.append((code.strip(), desc.strip()))
    return out


def suspect_role_phrases() -> list[str]:
    """Role-like phrases the step linter watches for in step prose: the
    pack's `step_linter_role_phrases` block ("|"-separated; UPI keeps its
    historical superset incl. Sponsor/Issuer variants), else cert-party labels
    plus participant labels."""
    raw = prompt_block("step_linter_role_phrases", "")
    if raw:
        return [p.strip() for p in raw.split("|") if p.strip()]
    seen: list[str] = []
    for lbl in party_labels() + [p.label for p in participants_of(get_active_pack())]:
        if lbl and lbl not in seen:
            seen.append(lbl)
    return seen


def exempt_role_phrase() -> str:
    """A phrase allowed in steps without a DETAILS entity entry (UPI:
    "UPI / NPCI" — the rail itself); "" when the pack declares none."""
    return prompt_block("step_linter_exempt_phrase", "")


def cred_subtypes() -> str:
    """The domain's credential subtypes for auth steps (UPI: "UPIPIN / MPIN /
    OTP"); "" when the domain has no credential-block concept, which disables
    the cred-consistency lint rather than borrowing UPI's."""
    return prompt_block("cred_subtypes", "")


def authority_label() -> str:
    return prompt_block("authority", "the certifying authority")
