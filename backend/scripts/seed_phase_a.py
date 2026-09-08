# Copyright 2026 The ATOM Authors
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


# The two seeded changes are DEMO CONTENT, not fixtures: nothing parses them.
# (Verified — the only reader of these CR ids in the tree is this file, and no
# code matches on the response codes, section headings or wire names below.)
#
# They are deliberately written in the platform's own domain-general vocabulary
# — members, the Authority, requests, entitlements — rather than in any one
# industry's. This is the first artifact an evaluator of the platform reads, so
# a payments-flavoured demo would assert a domain the product does not have.
# Keep the SHAPE when editing (4 numbered sections, FR-nn / NFR-nn, numbered
# acceptance criteria, trailing status line): the Phase B/C code paths that
# consume these rows are exercised by the shape, not by the words.
TARGETS = {
    "5242c33d-194b-41a2-97e1-bb1f387a3453": {  # Reserve-then-commit on the network
        "title": "Network Reserve and Commit",
        "enhanced_prompt": (
            "Introduce a 'Reserve and Commit' capability on the network that lets an "
            "initiating member earmark a requested quantity at the time the order is "
            "placed, with the actual commitment deferred until the fulfilling member "
            "confirms readiness. Targets order abandonment and reversal friction."
        ),
        "brd_md": """# BRD — Network Reserve and Commit

## 1. Business Context
Fulfilling members today receive an immediate, irrevocable network commitment at the time an order is placed. If fulfilment is delayed, cancelled, or only partly delivered, the reversal cycle takes 24-72h, eroding the initiating member's trust in the rail. Reserve and Commit introduces an earmark-then-commit flow on the network.

## 2. Functional Requirements
- **FR-01** Initiating member raises a Reserve request with `intent=reserve` and a hold window (max 7 days).
- **FR-02** The initiating member's operator places a soft hold; the ledger reflects a reduction in available entitlement but no committed draw.
- **FR-03** The fulfilling member's operator issues `commit` within the hold window to convert the hold into a real draw.
- **FR-04** If the hold expires, the system auto-releases; no draw, no reversal needed.
- **FR-05** Partial commits (staged fulfilment) supported up to the reserved quantity.

## 3. Non-Functional Requirements
- **NFR-01** Reserve API p99 latency < 800ms.
- **NFR-02** Hold ledger entries durable for 90 days post-release for audit.
- **NFR-03** Reconciliation file extended with `reserve_id`, `commit_id` keys.

## 4. Acceptance Criteria
1. Reserve → Commit → Settled within the hold window: success.
2. Reserve → expiry: auto-release with nothing drawn.
3. Reserve → over-commit: rejected with `RP-VAL-04`.

Status: APPROVED — Phase A complete.
""",
    },
    "30dfbbbd-74de-448f-9bd9-647788685655": {  # Staged draw on an entitlement line
        "title": "Staged Draw on a Pre-Approved Entitlement",
        "enhanced_prompt": (
            "Enable large single-shot network requests to be fulfilled as a scheduled "
            "series of smaller draws against a pre-approved entitlement line, settled "
            "on the network. Targets fulfilling members carrying high-value periodic "
            "obligations."
        ),
        "brd_md": """# BRD — Staged Draw on a Pre-Approved Entitlement

## 1. Business Context
The network today supports single-shot request settlement only. Several request categories carry obligation values large enough that splitting them into a scheduled series improves the initiating member's capacity planning and shortens the fulfilling member's outstanding cycle. Reusing the Entitlement Line already live on the network, we add a schedule-selection step on the confirmation screen.

## 2. Functional Requirements
- **FR-01** Eligibility check at request-fetch: the initiating member's pre-approved entitlement line plus the fulfilling member's acceptance flag.
- **FR-02** Schedule selection (3/6/9/12 period tenors) shown on the confirmation screen.
- **FR-03** First draw executed at confirmation; subsequent draws executed automatically under a mandate.
- **FR-04** Fulfilling member is credited the full quantity at T+0; the staged repayment is between the initiating member and the entitlement provider.
- **FR-05** Mandate revocation requires the entitlement provider to close the line; a standalone revoke by the initiating member is disallowed.

## 3. Non-Functional Requirements
- **NFR-01** Confirmation screen render budget < 1.5s including the eligibility call.
- **NFR-02** Audit trail on the schedule selected, retained 7 years per the Authority's record-retention norms.

## 4. Acceptance Criteria
1. Eligible member + participating fulfilling member: full schedule picker shown, mandate created on confirmation.
2. Eligible member + non-participating fulfilling member: standard one-shot flow only.
3. Ineligible member: no schedule picker shown.

Status: APPROVED — Phase A complete.
""",
    },
}

KIT_DOCS = [
    (ProductKitDocType.PRODUCT_DOC, "Product Doc",
     "## Product Document\n\nDetailed product behavior, screen flows, and edge cases. {feature_blurb}\n\n### Wire-level message contracts\n- ReqTransfer → with intent=reserve\n- RespTransfer → with reserve_id\n- ReqCommit → with reserve_id, commit_quantity\n- RespCommit → with commit_id\n\n### State machine\nINITIATED → RESERVED → (COMMITTED | EXPIRED | RELEASED)\n"),
    (ProductKitDocType.FAQ, "Frequently Asked Questions",
     "## FAQ\n\n**Q1:** What happens if the fulfilling member never commits?\n**A:** The hold auto-releases at the end of the configured window. Nothing is drawn.\n\n**Q2:** Is the hold visible to the initiating member?\n**A:** Yes — under a 'pending reserve' line, separate from settled draws.\n\n**Q3:** Can a Reserve be cancelled by the initiating member?\n**A:** Yes, before any commit. After a partial commit, only the uncommitted remainder can be cancelled.\n\n**Q4:** Settlement timing for the fulfilling member?\n**A:** T+0 on commit; standard network fulfilment cycles apply.\n"),
    (ProductKitDocType.CIRCULAR, "the Authority Circular",
     "## AUTH/NET/CIRC/{year}/{seq} — {feature_title}\n\n**Effective date:** {effective_date}\n**Applicability:** All network members, their operators, and participant applications.\n\n### 1. Background\n{feature_blurb}\n\n### 2. Mandate\nMembers SHALL implement the wire-level changes described in the attached Product Doc by the effective date. Non-compliance attracts the standard graded penalty under Annexure-3.\n\n### 3. Certification\nCertification kit available via the standard partner certification engine. Self-certification window: 30 days from this circular.\n"),
    (ProductKitDocType.MANIFEST, "Change Manifest",
     "## Manifest — {feature_title}\n\n```yaml\nchange_id: CHG-{short_id}\nversion: 1.0\nschema_version: 1.0\nartifacts:\n  - product_doc.md\n  - faq.md\n  - circular.md\n  - cert_test_cases.md\n  - product_note.md\nmandatory: true\nrollout_window: 90d\ncertification_required: true\nbreaking_change: false\n```\n"),
    (ProductKitDocType.CERT_TEST_CASES, "Certification Test Cases",
     # RC=00 is the network's success code and is real (see cert_pack_run's
     # `expected_rc`). Every RC=RP-* below is INVENTED for this demo — do not
     # treat them as a real code set.
     "## Certification Test Cases\n\n| TC ID | Scenario | Expected |\n|---|---|---|\n| TC-01 | Happy path: reserve → commit full quantity within window | RC=00, commit_id returned |\n| TC-02 | Reserve → expiry without commit | Auto-release, nothing drawn |\n| TC-03 | Reserve → partial commit (50%) → expiry | 50% drawn, 50% released |\n| TC-04 | Reserve → over-commit attempt | RC=RP-VAL-04 |\n| TC-05 | Reserve against insufficient entitlement | RC=RP-VAL-11 |\n| TC-06 | Commit after window expiry | RC=RP-VAL-09 |\n| TC-07 | Concurrent commit x2 (idempotency) | Single draw, second returns same commit_id |\n| TC-08 | Reserve cancelled by initiating member pre-commit | Released, nothing drawn |\n"),
    (ProductKitDocType.PRODUCT_NOTE, "Product Note",
     "## Product Note (internal)\n\nKey decisions and trade-offs the rollout team should know:\n\n1. **Hold ledger entries** are kept by the initiating member's operator, not centrally — keeps reconciliation lean but means each operator needs durable hold storage.\n2. **No new wire field** is mandatory: `intent` is OPTIONAL on the existing request message with default=`transfer`. Backward-compatible.\n3. **Mandate-on-Reserve** is explicitly out-of-scope for v1; v2 will revisit if member demand exists.\n"),
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
