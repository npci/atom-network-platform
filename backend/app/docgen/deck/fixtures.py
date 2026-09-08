# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Sample DeckOutline fixture.

Used by D1 round-trip verification, D4 renderer smoke-tests, and
fast iteration on the .pptx renderer without touching the LLM.

WHAT THIS IS FOR, and why the content is deliberately dull: the fixture's job
is to put every `SlideLayout` member in front of the renderer once, with cells
long enough to exercise wrapping. It is NOT sample content for any domain.

It used to be a complete product deck for one ecosystem, naming real products,
that ecosystem's participant roles and its regulator by name.
That was harmless as long as the platform served one ecosystem. It is not
harmless now: this is the fixture a developer opens to learn what a generated
deck looks like, and `build_master.py` renders it into the `deck_master.pptx`
that designers treat as the visual spec. Both taught one domain's vocabulary as
the platform's. Domain content belongs in a pack; a renderer fixture is
platform scaffolding and gets placeholder prose instead.
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


def sample_deck_outline() -> DeckOutline:
    """A 12-slide outline covering all eight layouts, with no domain content."""
    return DeckOutline(
        title="Sample Feature",
        subtitle="Layout reference deck",
        feature_name="Sample Feature",
        slides=[
            DeckSlide(
                slide_no=1, layout=SlideLayout.TITLE,
                title="Sample Feature",
                subtitle="A reference outline exercising every slide layout",
                speaker_notes="Open by framing the strategic context for the change.",
            ),
            DeckSlide(
                slide_no=2, layout=SlideLayout.SECTION,
                title="What problem are we solving?",
                speaker_notes="Pause for emphasis. The next slide gets specific.",
            ),
            DeckSlide(
                slide_no=3, layout=SlideLayout.THREE_COLUMN,
                title="Three usage categories",
                columns=[
                    ColumnBlock(icon_hint="globe",      heading="Category A", body="The first situation the feature is intended to serve."),
                    ColumnBlock(icon_hint="shield",     heading="Category B", body="The second situation, with a different risk profile."),
                    ColumnBlock(icon_hint="microphone", heading="Category C", body="The third situation, initiated through another channel."),
                ],
                speaker_notes="Anchor each column to a concrete example the audience knows.",
            ),
            DeckSlide(
                slide_no=4, layout=SlideLayout.BULLET_LIST,
                title="Key Highlights",
                bullets=[
                    "Moves from the current participant model to an extended one",
                    "Adds a secondary initiating participant alongside the primary",
                    "Request and authorisation originate from the same endpoint",
                    "Limits configured through the primary participant's application",
                ],
                speaker_notes="The added participant is the architectural shift — spend time here.",
            ),
            DeckSlide(
                slide_no=5, layout=SlideLayout.DIAGRAM,
                title="Architecture Diagram",
                diagram_kind="graphviz",
                diagram_text="""digraph G {
                    rankdir=LR; node [shape=box, style=rounded];
                    "Primary Participant" -> "Authority Platform";
                    "Secondary Participant" -> "Authority Platform";
                    "Authority Platform" -> "Servicing Participant";
                    "Authority Platform" -> "Receiving Participant";
                }""",
                speaker_notes="Walk left to right. Both initiators route through the authority as the integrity boundary.",
            ),
            DeckSlide(
                slide_no=6, layout=SlideLayout.NUMBERED_FLOW,
                title="Onboarding Flow",
                steps=[
                    NumberedStep(n=1, label="Initiate",     body="From the secondary endpoint"),
                    NumberedStep(n=2, label="Confirm",      body="In the primary application"),
                    NumberedStep(n=3, label="Select",       body="Choose the account or entitlement"),
                    NumberedStep(n=4, label="Define Limit", body="Per-request + daily cap"),
                    NumberedStep(n=5, label="Confirmation", body="Endpoint is live"),
                ],
                speaker_notes="The whole flow is sub-30-seconds end-to-end.",
            ),
            DeckSlide(
                slide_no=7, layout=SlideLayout.TABLE,
                title="Roles and Responsibilities",
                table=TableBlock(
                    headers=["Actor", "Responsibilities"],
                    rows=[
                        ["Secondary Participant", "Onboarding, request initiation, de-registration, dispute handling"],
                        ["Primary Participant",   "Endpoint binding, lifecycle mgmt, limits, dispute handling"],
                        ["Servicing Participant", "No change"],
                        ["Authority",             "Interoperability + risk management controls"],
                    ],
                ),
                speaker_notes="The servicing row is intentionally minimal — that's the design goal.",
            ),
            DeckSlide(
                slide_no=8, layout=SlideLayout.TWO_COLUMN,
                title="Risk Mitigation",
                columns=[
                    ColumnBlock(heading="Limits",            body="Per-request and daily cumulative caps. Cooling period for first 24h."),
                    ColumnBlock(heading="Lifecycle Control", body="Registration and de-registration from the primary app at any time."),
                ],
                speaker_notes="If asked about abuse — point to limits + cooling period as the layered defence.",
            ),
            DeckSlide(
                slide_no=9, layout=SlideLayout.BULLET_LIST,
                title="Regulatory Compliance",
                bullets=[
                    "Aligned with the applicable regulator's guidance on endpoint binding",
                    "Limits configurable centrally at the Authority per use case",
                    "Audit trail captured per registered endpoint",
                ],
                speaker_notes="Compliance posture: centrally enforced + per-endpoint auditable.",
            ),
            DeckSlide(
                slide_no=10, layout=SlideLayout.BULLET_LIST,
                title="Performance & Scale",
                bullets=[
                    "Target volume: 10 Mn requests in 5 years",
                    "Latency SLA: P95 < 800ms end-to-end",
                    "Availability: 99.95% inherited from the platform baseline",
                ],
                speaker_notes="The latency number includes the full round-trip.",
            ),
            DeckSlide(
                slide_no=11, layout=SlideLayout.SECTION,
                title="Open Questions",
                speaker_notes="Pause for Q&A.",
            ),
            DeckSlide(
                slide_no=12, layout=SlideLayout.TITLE,
                title="Thank you",
                subtitle="Questions? Reach out to the Change Management team.",
                speaker_notes="End.",
            ),
        ],
    )
