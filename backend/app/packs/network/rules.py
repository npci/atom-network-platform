# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Domain-specific hard constraints used in LLM prompts and post-gen validation.

One source of truth for:
  - The textual rules we paste into every document-generation system prompt
  - The regex patterns used to validate generated content

Keeping these together means a rule change only needs to happen in one place.
"""

# ──────────────────────────────────────────────────────────────────────────────
# Shared constraint block — inlined into agent SYSTEM_PROMPTs
# ──────────────────────────────────────────────────────────────────────────────

NETWORK_HARD_RULES = """\
# NETWORK DOCUMENT HARD RULES (must follow; violations fail review)

## Error Codes
- All transaction error codes MUST use the network alphanumeric format: U01-U99, Z0-Z9, RB, XD, XT, YA-YZ.
- NEVER use HTTP status codes (400, 401, 403, 404, 500, 502, 503) for transaction errors.
- Every error code MUST be classified as "TD" (Technical Decline) or "BD" (Business Decline). Never "error", "failure", or other free-form labels.
- Every error code row MUST identify the responsible entity: PSP / Authority / Issuer Bank / Beneficiary Bank / Remitter.

## Functional Requirements
- Every BRD/Tech Spec functional requirement MUST be numbered FR-01, FR-02, FR-03, ... (zero-padded to 2 digits).
- A BRD must have at least 6 functional requirements.
- Each FR must be a single declarative sentence beginning with "The system shall", "The Authority shall", "PSP Bank must", or a similar explicit subject.

## Obligation Language
- Use active obligation verbs: "The Authority shall", "PSP Bank must", "Issuer Bank shall", "The system shall".
- NEVER use vague phrasing: "the system will", "ideally", "should probably", "in some cases".
- Every flow step must name the responsible actor (PSP Bank / Authority / Issuer Bank / Customer / Merchant).

## Placeholders
- Final output must contain ZERO placeholder text. Never emit: "TBD", "TODO", "XXX", "<placeholder>", "N/A" (unless truly not applicable and stated so), "lorem ipsum".
- If a detail is genuinely unknown, state the assumption explicitly with an "Assumption:" prefix.

## Currency & Limits
- Use "INR" for amounts (e.g., "INR 2,000 per transaction"). Do not use "$", "Rs.", or "Rs".
- Use the "₹" symbol only in customer-facing display mockups, never in API specs or obligation text.

## API & Field Naming
- Request API names use the form "ReqXxxYyy" (PascalCase after Req).
- Response API names use the form "RespXxxYyy".
- Field names use camelCase (e.g., payerVpa, txnAmount), never snake_case or PascalCase.
- Every field row must specify: type, mandatory (Y/N), dLength, description.

## XSD / Schema
- XSD element names use PascalCase (e.g., <PayerDetails>).
- XSD attributes use camelCase.
- All amount fields use xs:decimal with total/fraction digits restrictions; never xs:string for amounts.
"""


# ──────────────────────────────────────────────────────────────────────────────
# Taxonomy-specific guidance (optional — include per agent)
# ──────────────────────────────────────────────────────────────────────────────

NETWORK_ERROR_CODE_EXAMPLES = """\
## Example error codes (reference only — use ones relevant to the feature)
| Code | td_bd | Entity | Description |
|------|-------|--------|-------------|
| U09  | TD    | PSP    | Invalid VPA format |
| U16  | TD    | Authority | Risk threshold exceeded |
| U30  | BD    | Issuer | Do not honour |
| U67  | BD    | PSP    | Collect request expired |
| Z9   | TD    | Authority | Duplicate transaction ID |
| RB   | TD    | Remitter Bank | Reversal by remitter bank |
| XD   | BD    | Authority | Debit has already been done |
"""
