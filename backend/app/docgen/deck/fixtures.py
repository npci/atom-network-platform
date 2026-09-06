# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Sample DeckOutline fixtures.

Used by D1 round-trip verification, D4 renderer smoke-tests, and
fast iteration on the .pptx renderer without touching the LLM.
The 12-slide fixture loosely mirrors the structure observed in
`docs/Embedded Payments final.pdf`.
"""
from __future__ import annotations

from app.docgen.deck.schema import (
    ColumnBlock,
    DeckOutline,
    DeckSlide,
    NumberedStep,
    SlideLayout,
    TableBlock,
)


def sample_embedded_payments() -> DeckOutline:
    return DeckOutline(
        title="Embedded Payments",
        subtitle="Use cases and market potential",
        feature_name="Embedded Payments",
        slides=[
            DeckSlide(
                slide_no=1, layout=SlideLayout.TITLE,
                title="Embedded Payments",
                subtitle="Use cases and market potential of NET-powered embedded devices",
                speaker_notes="Open by framing the strategic context: connected devices are growing 30% YoY in India.",
            ),
            DeckSlide(
                slide_no=2, layout=SlideLayout.SECTION,
                title="What problem are we solving?",
                speaker_notes="Pause for emphasis. The next slide gets specific.",
            ),
            DeckSlide(
                slide_no=3, layout=SlideLayout.THREE_COLUMN,
                title="Three device categories",
                columns=[
                    ColumnBlock(icon_hint="car",       heading="Cars",          body="Take me to the nearest fuel station. Recharge my FasTag wallet."),
                    ColumnBlock(icon_hint="watch",     heading="Wearables",     body="On-the-go tap and pay."),
                    ColumnBlock(icon_hint="microphone",heading="Voice",         body="Hey assistant, pay my electricity bill."),
                ],
                speaker_notes="Anchor each example to a real Indian use case the audience knows.",
            ),
            DeckSlide(
                slide_no=4, layout=SlideLayout.BULLET_LIST,
                title="Key Highlights",
                bullets=[
                    "Going from current 4-Party Model to a 5-Party Model",
                    "Primary PSP and Secondary device PSP for payer",
                    "Payment + authentication initiated from embedded device",
                    "Limit setting through the network Mobile App",
                ],
                speaker_notes="The 5th party is the device PSP. Spend time on this — it's the architectural shift.",
            ),
            DeckSlide(
                slide_no=5, layout=SlideLayout.DIAGRAM,
                title="Architecture Diagram",
                diagram_kind="graphviz",
                diagram_text="""digraph G {
                    rankdir=LR; node [shape=box, style=rounded];
                    "Primary Device PSP" -> "the Authority Network";
                    "Embedded Device PSP" -> "the Authority Network";
                    "the Authority Network" -> "Beneficiary Bank";
                    "the Authority Network" -> "Payee PSP";
                }""",
                speaker_notes="Walk left to right. Both device PSPs route through the Authority as the integrity boundary.",
            ),
            DeckSlide(
                slide_no=6, layout=SlideLayout.NUMBERED_FLOW,
                title="Embedded Device Registration Flow",
                steps=[
                    NumberedStep(n=1, label="Scan QR",         body="From embedded device"),
                    NumberedStep(n=2, label="Confirm Device",  body="In Primary the network app"),
                    NumberedStep(n=3, label="Confirm Banking", body="Account selection"),
                    NumberedStep(n=4, label="Define Limit",    body="Per-tx + daily cap"),
                    NumberedStep(n=5, label="Confirmation",    body="Embedded device live"),
                ],
                speaker_notes="The whole flow is sub-30-seconds end-to-end.",
            ),
            DeckSlide(
                slide_no=7, layout=SlideLayout.TABLE,
                title="Roles and Responsibilities",
                table=TableBlock(
                    headers=["Actor", "Responsibilities"],
                    rows=[
                        ["Embedded PSP", "Registration, payment initiation, de-registration, dispute mgmt"],
                        ["Primary PSP",  "Device binding, lifecycle mgmt, limits, dispute mgmt"],
                        ["Issuer",       "No change"],
                        ["NPCI",         "Interoperability + risk management controls"],
                    ],
                ),
                speaker_notes="The Issuer row is intentionally minimal — that's the design goal.",
            ),
            DeckSlide(
                slide_no=8, layout=SlideLayout.TWO_COLUMN,
                title="Risk Mitigation",
                columns=[
                    ColumnBlock(heading="Limits",           body="Per-tx and daily cumulative caps. Cooling period for first 24h."),
                    ColumnBlock(heading="Lifecycle Control", body="Registration and de-registration from primary app at any time."),
                ],
                speaker_notes="If asked about fraud — point to limits + cooling period as the layered defence.",
            ),
            DeckSlide(
                slide_no=9, layout=SlideLayout.BULLET_LIST,
                title="Regulatory Compliance",
                bullets=[
                    "Aligned with RBI guidelines on the network device binding",
                    "Limits configurable centrally at the Authority per use case",
                    "Audit trail captured per registered device",
                ],
                speaker_notes="Compliance posture: centrally enforced + per-device auditable.",
            ),
            DeckSlide(
                slide_no=10, layout=SlideLayout.BULLET_LIST,
                title="Performance & Scale",
                bullets=[
                    "Target volume: 10 Mn transactions in 5 years",
                    "Latency SLA: P95 < 800ms end-to-end",
                    "Availability: 99.95% inherited from the network baseline",
                ],
                speaker_notes="The latency number includes device-to-device round-trip.",
            ),
            DeckSlide(
                slide_no=11, layout=SlideLayout.SECTION,
                title="Open Questions",
                speaker_notes="Pause for Q&A.",
            ),
            DeckSlide(
                slide_no=12, layout=SlideLayout.TITLE,
                title="Thank you",
                subtitle="Questions? Reach out to the Authority Change Management team.",
                speaker_notes="End.",
            ),
        ],
    )
