# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""NLLN-specific hard constraints used in LLM prompts and post-gen validation.

Mirrors the shape of `app.packs.network.rules` — same categories (error codes,
functional requirements, obligation language, placeholders, limits, API/field
naming, XSD/schema) with library-loan vocabulary in place of payments
vocabulary. Illustrative, not a transcription of any real NLLC specification.
"""

NLLN_HARD_RULES = """\
# NLLN LIBRARY-LOAN DOCUMENT HARD RULES (must follow; violations fail review)

## Error Codes
- All loan-protocol error codes MUST use the NLLN alphanumeric format: L01-L99 (loan-state errors), C0-C9 (catalogue/holdings errors), RV (reservation conflict), XL (loan-limit exceeded).
- NEVER use HTTP status codes (400, 401, 403, 404, 500, 502, 503) for loan-protocol errors.
- Every error code MUST be classified as "SE" (System Error) or "PD" (Policy Decline). Never "error", "failure", or other free-form labels.
- Every error code row MUST identify the responsible entity: Participant Library / NLLC / Lending Library / Borrowing Library.

## Functional Requirements
- Every BRD/Tech Spec functional requirement MUST be numbered FR-01, FR-02, FR-03, ... (zero-padded to 2 digits).
- A BRD must have at least 6 functional requirements.
- Each FR must be a single declarative sentence beginning with "The system shall", "NLLC shall", "Participant Library must", or a similar explicit subject.

## Obligation Language
- Use active obligation verbs: "NLLC shall", "Participant Library must", "Lending Library shall", "The system shall".
- NEVER use vague phrasing: "the system will", "ideally", "should probably", "in some cases".
- Every flow step must name the responsible actor (Lending Library / NLLC / Borrowing Library / Patron).

## Placeholders
- Final output must contain ZERO placeholder text. Never emit: "TBD", "TODO", "XXX", "<placeholder>", "N/A" (unless truly not applicable and stated so), "lorem ipsum".
- If a detail is genuinely unknown, state the assumption explicitly with an "Assumption:" prefix.

## Loan Periods & Limits
- Use a structured duration (numeric value + unit) for loan periods (e.g., "14 days"), never a bare unqualified number.
- Renewal counts and reservation queue lengths are integers; state the unit explicitly.

## API & Field Naming
- Request API names use the form "ReqXxxYyy" (PascalCase after Req).
- Response API names use the form "RespXxxYyy".
- Field names use camelCase (e.g., patronId, loanPeriod), never snake_case or PascalCase.
- Every field row must specify: type, mandatory (Y/N), dLength, description.

## XSD / Schema
- XSD element names use PascalCase (e.g., <LoanPeriod>).
- XSD attributes use camelCase.
- All duration/count fields use xs:decimal or xs:integer with explicit restrictions; never xs:string for numeric values.
"""


NLLN_ERROR_CODE_EXAMPLES = """\
## Example error codes (reference only — use ones relevant to the feature)
| Code | se_pd | Entity | Description |
|------|-------|--------|-------------|
| L09  | SE    | Participant Library | Invalid patron identifier |
| L16  | SE    | NLLC   | Catalogue lookup timeout |
| C30  | PD    | Lending Library | Item not available for loan |
| RV   | PD    | NLLC   | Reservation conflict — item already held |
| XL   | PD    | Participant Library | Patron loan limit exceeded |
"""
