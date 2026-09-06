# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Seed Phase A artifacts on two clean CRs so Phase C work has inputs.

Populates: BRD (approved) + ProductKitDocument rows (product_doc, faq,
circular, manifest, cert_test_cases, product_note). Sets status to
PRODUCT_KIT. No Phase B artifacts (tech_spec, xsd) are written.

Idempotent — re-running updates content without duplicating rows.
"""
from app.core.database import SessionLocal
from app.models.change_request import ChangeRequest, ChangeStatus
from app.models.brd import BRD, BRDStatus
from app.models.product_kit import ProductKitDocument, ProductKitDocType
from app.models.research import ArtifactStatus
from app.models.phase_c import (
    PartnerAgent, ChangePartnerAssignment, AssignmentStatus,
)
from sqlalchemy import select


TARGETS = {
    "5242c33d-194b-41a2-97e1-bb1f387a3453": {  # The network Reserve Pay
        "title": "the network Reserve Pay",
        "enhanced_prompt": (
            "Introduce a 'Reserve Pay' feature on the network that lets payers earmark "
            "an amount on the payer account at the time of placing the order, "
            "with actual debit deferred until the merchant confirms shipment. "
            "Targets e-commerce checkout abandonment and refund friction."
        ),
        "brd_md": """# BRD — the network Reserve Pay

## 1. Business Context
E-commerce merchants today receive an immediate the network debit at the time of order placement. If shipment is delayed, cancelled, or partially fulfilled, refund cycles take 24-72h, eroding payer trust. Reserve Pay introduces an earmark-then-capture flow on the network rails.

## 2. Functional Requirements
- **FR-01** Payer initiates a Reserve transaction with `intent=reserve` and a hold window (max 7 days).
- **FR-02** Payer PSP places a soft hold; ledger reflects available-balance reduction but not actual debit.
- **FR-03** Merchant PSP issues `capture` within hold window to convert to a real debit.
- **FR-04** If hold expires, system auto-releases; no debit, no refund needed.
- **FR-05** Partial captures (multiple shipments) supported up to the reserved amount.

## 3. Non-Functional Requirements
- **NFR-01** Reserve API p99 latency < 800ms.
- **NFR-02** Hold ledger entries durable for 90 days post-release for audit.
- **NFR-03** Reconciliation file extended with `reserve_id`, `capture_id` keys.

## 4. Acceptance Criteria
1. Reserve → Capture → Settled within hold window: success.
2. Reserve → expiry: auto-release with no debit booked.
3. Reserve → over-capture: rejected with `RP-VAL-04`.

Status: APPROVED — Phase A complete.
""",
    },
    "30dfbbbd-74de-448f-9bd9-647788685655": {  # EMI Bill payments
        "title": "EMI Bill Payments on the network",
        "enhanced_prompt": (
            "Enable BBPS bills (electricity, telecom, education) to be paid in "
            "EMIs sourced from a pre-approved credit line, settled on the network rails. "
            "Targets billers with high-value-monthly invoices."
        ),
        "brd_md": """# BRD — EMI Bill Payments

## 1. Business Context
BBPS today supports lump-sum bill settlement only. Several biller categories (electricity arrears, term-fee education, post-paid telecom) have invoice values where EMI conversion improves payer affordability and reduces biller DSO. Reusing the Credit Line on the network rails (already-launched) we add an EMI plan selection step on the BBPS confirm screen.

## 2. Functional Requirements
- **FR-01** Eligibility check at biller-fetch: payer's pre-approved credit line + biller acceptance flag.
- **FR-02** EMI plan selection (3/6/9/12 month tenors) shown on confirm screen.
- **FR-03** First instalment debited at confirm; subsequent instalments auto-debited via mandate.
- **FR-04** Biller credited full amount at T+0; EMI repayment is between payer and lender.
- **FR-05** Mandate revocation requires lender-side closure of the line; standalone payer revoke disallowed.

## 3. Non-Functional Requirements
- **NFR-01** Confirm screen render budget < 1.5s including eligibility call.
- **NFR-02** Audit trail on EMI plan selected, retained 7 years (RBI lending norms).

## 4. Acceptance Criteria
1. Eligible payer + EMI biller: full plan picker shown, mandate created on confirm.
2. Eligible payer + non-EMI biller: standard one-time pay flow only.
3. Ineligible payer: no plan picker shown.

Status: APPROVED — Phase A complete.
""",
    },
}

KIT_DOCS = [
    (ProductKitDocType.PRODUCT_DOC, "Product Doc",
     "## Product Document\n\nDetailed product behavior, screen flows, and edge cases. {feature_blurb}\n\n### Wire-level message contracts\n- ReqTransfer → with intent=reserve\n- RespTransfer → with reserve_id\n- ReqCapture → with reserve_id, capture_amount\n- RespCapture → with capture_id\n\n### State machine\nINITIATED → RESERVED → (CAPTURED | EXPIRED | RELEASED)\n"),
    (ProductKitDocType.FAQ, "Frequently Asked Questions",
     "## FAQ\n\n**Q1:** What happens if the merchant never captures?\n**A:** The hold auto-releases at the end of the configured window. No debit is booked.\n\n**Q2:** Is the hold visible on the payer's statement?\n**A:** Yes — under a 'pending reserve' line, separate from settled debits.\n\n**Q3:** Can a Reserve be cancelled by the payer?\n**A:** Yes, before any capture. After partial capture, only the unreserved balance can be cancelled.\n\n**Q4:** Settlement timing for the merchant?\n**A:** T+0 on capture; standard the network settlement cycles apply.\n"),
    (ProductKitDocType.CIRCULAR, "the Authority Circular",
     "## The Authority/the network/CIRC/{year}/{seq} — {feature_title}\n\n**Effective date:** {effective_date}\n**Applicability:** All the network member banks, PSPs, TPAPs.\n\n### 1. Background\n{feature_blurb}\n\n### 2. Mandate\nMember banks SHALL implement the wire-level changes described in the attached Product Doc by the effective date. Non-compliance attracts the standard graded penalty under Annexure-3.\n\n### 3. Certification\nCertification kit available via the standard partner certification engine. Self-certification window: 30 days from this circular.\n"),
    (ProductKitDocType.MANIFEST, "Change Manifest",
     "## Manifest — {feature_title}\n\n```yaml\nchange_id: CHG-{short_id}\nversion: 1.0\nschema_version: 1.0\nartifacts:\n  - product_doc.md\n  - faq.md\n  - circular.md\n  - cert_test_cases.md\n  - product_note.md\nmandatory: true\nrollout_window: 90d\ncertification_required: true\nbreaking_change: false\n```\n"),
    (ProductKitDocType.CERT_TEST_CASES, "Certification Test Cases",
     "## Certification Test Cases\n\n| TC ID | Scenario | Expected |\n|---|---|---|\n| TC-01 | Happy path: reserve → capture full amount within window | RC=00, capture_id returned |\n| TC-02 | Reserve → expiry without capture | Auto-release, no debit |\n| TC-03 | Reserve → partial capture (50%) → expiry | 50% debited, 50% released |\n| TC-04 | Reserve → over-capture attempt | RC=RP-VAL-04 |\n| TC-05 | Reserve on insufficient balance | RC=ZA |\n| TC-06 | Capture after window expiry | RC=RP-VAL-09 |\n| TC-07 | Concurrent capture x2 (idempotency) | Single debit, second returns same capture_id |\n| TC-08 | Reserve cancelled by payer pre-capture | Released, no debit |\n"),
    (ProductKitDocType.PRODUCT_NOTE, "Product Note",
     "## Product Note (internal)\n\nKey decisions and trade-offs the rollout team should know:\n\n1. **Hold ledger entries** are kept on the payer-PSP side, not central — keeps reconciliation lean but means each PSP needs durable hold storage.\n2. **No new wire field** is mandatory: `intent` is OPTIONAL on existing PayReq with default=`pay`. Backward-compatible.\n3. **Mandate-on-Reserve** is explicitly out-of-scope for v1; v2 will revisit if merchant demand exists.\n"),
]


def main():
    db = SessionLocal()
    try:
        for cr_id, meta in TARGETS.items():
            cr = db.get(ChangeRequest, cr_id)
            if not cr:
                print(f"  ✗ CR {cr_id} not found, skipping")
                continue

            cr.title = meta["title"]
            cr.enhanced_prompt = meta["enhanced_prompt"]
            cr.status = ChangeStatus.PRODUCT_KIT

            # BRD — upsert a single approved row at version=1
            existing_brd = db.scalar(
                select(BRD).where(BRD.change_request_id == cr_id, BRD.version == 1)
            )
            if existing_brd:
                existing_brd.content = meta["brd_md"]
                existing_brd.status = BRDStatus.APPROVED
            else:
                db.add(BRD(
                    change_request_id=cr_id,
                    content=meta["brd_md"],
                    version=1,
                    status=BRDStatus.APPROVED,
                ))

            # ProductKitDocument — upsert one row per doc_type at version=1
            short_id = cr_id.split("-")[0]
            template_vars = {
                "feature_title": meta["title"],
                "feature_blurb": meta["enhanced_prompt"],
                "year": "2026",
                "seq": "042",
                "effective_date": "2026-08-01",
                "short_id": short_id,
            }
            for doc_type, _label, body_template in KIT_DOCS:
                body = body_template.format(**template_vars)
                existing = db.scalar(
                    select(ProductKitDocument).where(
                        ProductKitDocument.change_request_id == cr_id,
                        ProductKitDocument.doc_type == doc_type,
                        ProductKitDocument.version == 1,
                    )
                )
                if existing:
                    existing.content = body
                    existing.status = ArtifactStatus.APPROVED
                else:
                    db.add(ProductKitDocument(
                        change_request_id=cr_id,
                        doc_type=doc_type,
                        content=body,
                        version=1,
                        status=ArtifactStatus.APPROVED,
                    ))

            # Assign all active partners in ASSIGNED state — leaves Phase C
            # ready to dispatch (Change Communication trigger lives in the UI).
            partners = db.scalars(select(PartnerAgent)).all()
            assigned_count = 0
            for p in partners:
                existing_assn = db.scalar(
                    select(ChangePartnerAssignment).where(
                        ChangePartnerAssignment.change_request_id == cr_id,
                        ChangePartnerAssignment.partner_id == p.id,
                    )
                )
                if not existing_assn:
                    db.add(ChangePartnerAssignment(
                        change_request_id=cr_id,
                        partner_id=p.id,
                        status=AssignmentStatus.ASSIGNED,
                    ))
                    assigned_count += 1

            print(f"  ✓ {meta['title']} ({cr_id[:8]}) — BRD + 6 kit docs, status=PRODUCT_KIT, +{assigned_count} new partner assignments")

        db.commit()
        print("\nSeed complete.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
