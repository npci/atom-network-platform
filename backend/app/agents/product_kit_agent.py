# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Product Kit Generator agent.

Generates all 9 partner-ready documents that form the Product Kit:
  1. Product Document
  2. Product Deck (slide outline)
  3. Promo Video Script
  4. Explainer Video Script
  5. FAQ Document
  6. Certification Test Cases
  7. Circular
  8. Manifest File (YAML)
  9. Prototype Screens (HTML wireframes)
"""
import base64
import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path

from app.core.llm import stream_llm, call_llm
from app.core.json_recovery import parse_llm_json
from app.core.llm_router import pick_model_for_agent
from app.core.domain.contract import participants_of
from app.core.domain.registry import get_active_pack, prompt_block

# Supplied by the active domain pack, not by any single domain's module.
DOMAIN_HARD_RULES = prompt_block("hard_rules")
API_DESIGN_PRINCIPLES = prompt_block("api_design_principles")

# ── Pack-derived prompt fragments (resolved at import, registry pattern) ──────
# Actor lists, audience-specific FAQ sections, sandbox identifier hints and the
# manifest's per-participant effort keys are ecosystem vocabulary. Each has a
# domain-neutral default (mostly derived from the pack's declared participants)
# so a pack that supplies no block still gets honest, un-branded prose.
_PARTICIPANTS = participants_of(get_active_pack())

_JOURNEY_ACTORS = prompt_block(
    "kit_user_journey_actors",
    "\n".join(f"- {p.label}" for p in _PARTICIPANTS)
    or "- Each participant role this ecosystem defines",
)
_FAQ_AUDIENCE_3 = prompt_block(
    "kit_faq_audience_3",
    "## SECTION 3: Participant Operations FAQs\n\n"
    "Generate 8–12 Q&A pairs covering:\n"
    "- Impact on existing operational processes\n"
    "- Reconciliation changes\n"
    "- Dispute handling\n"
    "- Go-live dependencies\n"
    "- Regulatory reporting changes",
)
_TEST_IDENTIFIERS = prompt_block(
    "kit_test_identifiers",
    "- Test identifier formats to use\n- Test participant codes",
)
_BOUNDARY_EXAMPLES = prompt_block(
    "kit_boundary_examples",
    "maximum and minimum field values, special characters in identifiers",
)
# One "<Low|Medium|High>" effort line per non-authority participant role.
_INTEGRATION_EFFORT_LINES = "\n".join(
    f'  {p.key}: "<Low|Medium|High>"'
    for p in _PARTICIPANTS if not p.is_authority
) or '  partner: "<Low|Medium|High>"'
# Manifest key prefix for regulator circular references (rbi_circular_refs for
# UPI, nllc_circular_refs for NLLN, ...).
_REGULATOR_KEY = (
    prompt_block("regulatory_body", "regulator").strip().lower().replace(" ", "_")
    or "regulator"
)
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE
from app.agents.video_script_schema import (
    VideoScript, VideoSegment, segment_boundaries,
)

logger = logging.getLogger(__name__)


# ── System prompts per doc type ───────────────────────────────────────────────

_SYSTEM_PROMPTS = {
    "product_doc": f"""You are the Product Documentation Author for this change-management platform.

Generate a comprehensive **Product Document** for the feature change described in the provided context.
This document is read by internal stakeholders, partner organisations to understand the new feature.

Structure the document with these sections (## headings):

## 1. Executive Summary
One-page overview: what is changing, why, and the key benefits.

## 2. Product Overview
### 2.1 Feature Description
Detailed explanation of the new feature/change.
### 2.2 Problem Statement
The existing gap or pain point this change addresses.
### 2.3 Solution Approach
How this change solves the problem.

## 3. User Journeys
Provide step-by-step user journey narratives for each actor:
{_JOURNEY_ACTORS}

For each journey, describe the sequence of steps and system interactions.

## 4. Feature Specifications
### 4.1 Functional Specifications
Detailed functional requirements with acceptance criteria.
### 4.2 Business Rules
Key business rules, limits, caps, and constraints.
### 4.3 Edge Cases & Exceptions
How boundary and error scenarios are handled.

## 5. Integration Requirements
What changes partner organisations need to make on their side to support this feature.

## 6. Regulatory & Compliance Notes
- Regulator guidelines addressed
- Data privacy considerations
- PCI-DSS implications

## 7. Go-Live Criteria
Checklist of items that must be satisfied before this feature goes live.

## 8. Glossary
Key terms and abbreviations used in this document.

---
Rules:
- Be specific to this domain's terminology
- Use professional language suitable for regulatory and partner distribution
- Target length: 2,500–4,000 words
- When revising from feedback, emit the complete updated document

""" + ANTI_INJECTION_CLAUSE,

    "product_deck": """You are the Presentation Designer for this change-management platform.

Generate a **Product Deck** for the feature change. The deck is delivered to partners
as TWO artefacts produced from the same output:
  (A) A markdown SCRIPT (rendered to `.docx` for the speaker / audit trail).
  (B) A `.pptx` PRESENTATION (rendered from a structured JSON outline
      you also emit at the end of this response).

Slide budget: 12-16 slides. Pick the right number for the change at hand.

# Part A — Markdown Script

Write the markdown script slide-by-slide. Each slide gets:

## Slide N: <Slide Title>
- Body content (bullets preferred; sub-headings OK)

Suggested narrative arc (adapt for the feature):

  Slide 1   Title Slide          (feature name + tagline)
  Slide 2   Agenda / Section divider
  Slide 3   The Problem Today    (pain points; cite stats from context if available)
  Slide 4   Our Solution         (one punchy headline + 3-4 key points)
  Slide 5   How It Works         (step-by-step user journey)
  Slide 6   Key Features         (4-6 features with one-line descriptions)
  Slide 7   Technical Architecture (high-level block diagram)
  Slide 8   Partner Integration  (participant obligations, timeline)
  Slide 9   Regulatory Compliance (regulator / authority references)
  Slide 10  Performance & Scale  (TPS, latency, availability)
  Slide 11  Go-Live Roadmap      (milestone table)
  Slide 12  Next Steps / CTA

Rules for Part A:
- Each slide should hold 2-3 minutes of presenter narration's worth of detail.
- Bullet points over paragraphs in slide bodies.
- Boardroom tone — crisp and domain-appropriate throughout.
- When revising from feedback, emit all slides in full (not a diff).

# Part B — JSON Outline (emit AFTER the markdown, in a fenced ```json``` block)

Append exactly one fenced `json` block at the very end of your response. Inside, emit
a JSON object that mirrors the markdown above and conforms to this shape:

```
DeckOutline = {
  title:        string,                 # short — appears on slide 1
  subtitle:     string,                 # tagline — appears under the title
  feature_name: string,                 # used in the slide footer
  slides:       DeckSlide[]             # 3-16 slides, slide_no = 1..N in order
}

DeckSlide = {
  slide_no:      int (1..N),
  layout:        "title" | "section" | "bullet_list" | "two_column" |
                 "three_column" | "numbered_flow" | "diagram" | "table",
  title:         string,                # always required
  speaker_notes: string,                # always required, 1-3 sentences
  # The fields below are layout-specific. Populate ONLY the field(s) the layout requires:
  subtitle:      string?                # title layout only
  bullets:       string[]?              # bullet_list — 1-12 items
  columns:       ColumnBlock[]?         # two_column → 2 items; three_column → 3 items
  steps:         NumberedStep[]?        # numbered_flow — 2-7 steps
  table:         TableBlock?            # table layout
  diagram_text:  string?                # diagram layout — graphviz `digraph { ... }`
  diagram_kind:  "graphviz"?            # diagram layout — must be exactly "graphviz"
}

ColumnBlock  = { icon_hint?: string, heading: string, body: string }
NumberedStep = { n: int, label: string, body?: string }
TableBlock   = { headers: string[], rows: string[][] }   # rows[i].length == headers.length
```

Layout selection rules (PICK THE BEST FIT — the markdown title hints at the layout):
- "title": opening + closing slides (slide 1 MUST be `title`).
- "section": short divider headlines like "Open Questions" or "Architecture".
- "bullet_list": a list of points (the most common layout).
- "two_column" / "three_column": comparing 2-3 things side-by-side.
- "numbered_flow": a multi-step user journey or process (slide 5 / Slide "How It Works" is a strong candidate).
- "diagram": when an architecture, sequence, or block diagram makes the point. Provide `diagram_text` as VALID graphviz DOT source (e.g. `digraph G { rankdir=LR; A -> B; }`). Use real component / actor names from the feature context, not placeholders.
- "table": for milestone / role / RACI matrices. `headers` and every row must have the same column count.

Hard requirements:
- `slide_no` starts at 1 and increments by 1 — no gaps, no duplicates.
- Slide 1's `layout` MUST be `"title"`.
- Total slides between 3 and 16 inclusive.
- Speaker notes are mandatory on EVERY slide (1-3 sentences guiding the presenter).
- The JSON content MUST mirror the markdown — same titles, same number of slides, same data.
- Emit exactly ONE fenced `json` block, at the very end. No commentary after it.

""" + ANTI_INJECTION_CLAUSE,

    "promo_video": """You are the Marketing Scriptwriter for this change-management platform.

Write a **Promotional Video Script** for the new feature. This is a 60–90 second marketing video
targeted at end customers and the general public. The tone is upbeat, simple, and inspiring.

Structure the script using these sections:

## OPENING (0:00–0:10)
Scene description + Voiceover text. Hook the viewer immediately.

## PROBLEM SETUP (0:10–0:25)
Show the current pain/friction in relatable terms. On-screen visuals described.

## FEATURE REVEAL (0:25–0:50)
Introduce the new feature clearly. Show it in action (phone screen mockup descriptions).
Voiceover explains the key benefit in plain language.

## HOW IT WORKS (0:50–1:10)
3-4 simple steps shown visually. Keep it very accessible — no technical jargon.

## TESTIMONIAL / SOCIAL PROOF (1:10–1:20)
Short persona quote or stat reinforcing the value.

## CALL TO ACTION (1:20–1:30)
Clear CTA: a concrete next step for the audience / "Available from [date]".

---
Script format for each section:
[VISUAL]: What appears on screen
[VO]: Voiceover text
[TEXT ON SCREEN]: Any overlay text

---
Rules:
- Write for a general Indian audience (reference everyday Indian contexts, not technical banking terms)
- Voiceover should be translatable to Hindi easily (short sentences, no idioms)
- Tone: friendly, trustworthy, modern
- When revising from feedback, emit the complete script

""" + ANTI_INJECTION_CLAUSE,

    "explainer_video": """You are the Educational Content Writer for this change-management platform.

Write an **Explainer Video Script** for the new feature. This is a 2–3 minute step-by-step tutorial
aimed at partner teams and app developers who need to understand and implement the feature.

Structure:

## INTRO (0:00–0:15)
Brief welcome, what this video covers, who it's for.

## BACKGROUND CONTEXT (0:15–0:45)
Why this feature is being introduced. What problem it solves technically.

## KEY CONCEPTS (0:45–1:30)
Define 3–5 key terms or concepts that are essential to understand before the walkthrough.
Format:
**[Term]**: [Plain-language explanation]

## STEP-BY-STEP WALKTHROUGH (1:30–2:30)
### Step 1: [Step Title]
[VISUAL]: Screen/diagram description
[VO]: Detailed explanation
...continue for each step (aim for 4–7 steps)

## INTEGRATION CHECKLIST (2:30–2:50)
Quick recap of what partner teams need to implement:
- [ ] Checklist item 1
- [ ] Checklist item 2
...

## WHERE TO GET HELP (2:50–3:00)
Reference to the authority's developer portal, sandbox environment, support contacts.

---
Rules:
- Target audience: partner developer teams
- Use proper technical terminology (API endpoints, message formats, flow diagrams)
- Keep narration clear and paced (each sentence ~5 seconds of speech)
- When revising from feedback, emit the complete script

""" + ANTI_INJECTION_CLAUSE,

    "faq": f"""You are the Customer & Partner Success Writer for this change-management platform.

Generate a comprehensive **FAQ Document** for the new feature. Cover three audiences:

## SECTION 1: End Customer FAQs
(Simple language, non-technical, everyday user perspective)

Generate 10–15 Q&A pairs covering:
- What is this new feature and how does it benefit me?
- How do I use it?
- Is it safe and secure?
- What if something goes wrong?
- Which apps / banks support it?
- Cost / charges?
- Availability date and rollout?

## SECTION 2: Partner FAQs
(Technical and integration-focused)

Generate 10–15 Q&A pairs covering:
- Integration effort and API changes required
- Certification process
- Sandbox availability
- Backward compatibility
- Error codes and retry behavior
- SLA and performance expectations
- Compliance requirements

{_FAQ_AUDIENCE_3}

---
Format for each FAQ:
**Q: [Question]**
A: [Answer — 2–5 sentences, clear and complete]

---
Rules:
- Questions must be realistic — phrased as real stakeholders would ask them
- Answers must be accurate to this ecosystem's processes
- Cross-reference between sections where relevant ("See also: Partner FAQs #3")
- When revising from feedback, emit the complete FAQ document

""" + ANTI_INJECTION_CLAUSE,

    # ── [LEGACY — NOT IN USE under default settings] ──────────────────────
    # WHY this prompt is kept: emergency rollback. The default flow for
    # cert_test_cases is now the LangGraph pipeline at
    # `app.excel_testcase_engine` (gated by settings.excel_engine_enabled,
    # default true). The WS handler in agents.py routes cert_test_cases to
    # the engine and skips this prompt entirely. If the operator flips
    # excel_engine_enabled = false, cert_test_cases falls back here.
    # Do not edit this prompt expecting changes to surface in the live
    # workbook output — edit the engine's prompts in
    # `app/excel_testcase_engine/prompts/` instead.
    "cert_test_cases": f"""You are the Quality Engineering Lead for this change-management platform.

Generate a **Certification Test Case Document** for partner certification of the new feature.
Partner organisations must pass these test cases before going live.

Structure:

## 1. Test Scope
Brief description of what is being certified and who must complete this certification.

## 2. Test Environment
- Sandbox URL / endpoint details (placeholder)
{_TEST_IDENTIFIERS}
- Simulator availability

## 3. Prerequisites
List of items partners must have in place before starting certification:
- API credentials
- Sandbox access
- Implementation completeness checklist

## 4. Test Cases

Format each test case as:

| TC-ID | Test Case Name | Description | Precondition | Test Steps | Expected Result | Pass Criteria |
|-------|---------------|-------------|--------------|-----------|-----------------|---------------|

Generate at minimum 25–35 test cases grouped by category:

### 4.1 Happy Path Test Cases (TC-HP-001 to TC-HP-010)
Core positive flows. Include at least 10 cases.

### 4.2 Negative / Error Test Cases (TC-NEG-001 to TC-NEG-010)
Invalid inputs, missing fields, exceeded limits. At least 10 cases.

### 4.3 Boundary Test Cases (TC-BND-001 to TC-BND-005)
Edge values: {_BOUNDARY_EXAMPLES}, etc.

### 4.4 Security Test Cases (TC-SEC-001 to TC-SEC-005)
Auth failures, replay attacks, tampered payloads.

### 4.5 Performance / SLA Test Cases (TC-PERF-001 to TC-PERF-005)
Response time validation, concurrent request handling.

## 5. Certification Scoring
Table: Category | Total TCs | Must-Pass Count | Pass Threshold

## 6. Defect Reporting
How to report failures, severity levels, SLA for the authority to respond.

---
Rules:
- TC-IDs must be unique and follow the category convention
- Test steps must be concrete and executable
- Expected results must be unambiguous
- When revising from feedback, emit the complete document

""" + ANTI_INJECTION_CLAUSE,

    "circular": """You are the Regulatory Communications Officer for this change-management platform.

Draft an **Operating Circular** to notify all ecosystem participants about the new feature/change. Match the layout the issuing authority uses for its circulars; where prior circulars appear in the supplied context, follow their numbering and phrasing conventions.

Use THIS EXACT TOP BLOCK (do not skip any line):

---

**[ISSUING AUTHORITY — FULL LEGAL NAME]**

**[AUTHORITY]/[DOMAIN]/OC No. [NNN]/[YYYY-YY]**                                 **[Day Month, Year]**

To,

The Members — [ecosystem / network name]

Dear Madam / Sir,

**Subject:** [Feature Name] — [one-line action statement]

---

Immediately after the salutation, open with a one-paragraph context setter referencing the relevant prior circular (cite it in the authority's own numbering format, taken from the supplied context — never invent a circular number) and stating the reason for the present circular.

Then produce the following sections with ## headings:

## 1. Background and Objective
Reference current state, the observed issue or opportunity, and the regulatory mandate (cite the regulator's directive where applicable). Use [S#] citations when corpus evidence is provided.

## 2. Scope and Applicability
List entities bound by this circular:
List the participant roles THIS domain defines, plus the authority's own
switch/platform. Use the role names from the supplied domain knowledge — do not
import roles from another ecosystem.

## 3. Feature / Change Description
What is being introduced, modified, or deprecated. Keep concise — technical depth belongs in the TSD, not here.

## 4. Effective Date and Timeline
| Milestone | Date | Applicable To |
|-----------|------|---------------|
| Certification window opens | DD-MMM-YYYY | All participants |
| UAT cut-over | DD-MMM-YYYY | Affected participants |
| Production go-live | DD-MMM-YYYY | All participants |

Dates must come from PM clarification answers or be expressed as relative placeholders ("T+30 days from circular date", "Q3 go-live") — never invented absolute dates.

## 5. Obligations on Participants
Bullet list of MUST-DO obligations per participant role. Use imperative voice ("<Role> shall…", "<Role> must…") with this domain's role names. Ground each obligation in a [S#] citation when corpus evidence is available.

## 6. Technical Implementation Notes
High-level: API changes, new / deprecated fields, error code updates. Refer readers to the TSD for detail.

## 7. Non-Compliance
State the consequence of missing the go-live deadline (e.g. penal charges, suspension of transaction routing, mandatory remediation plan). Use the authority's standard language.

## 8. Support and Escalation
| Channel | Purpose | Contact |
|---------|---------|---------|
Use the support channels named in the supplied context. Never invent an email
address or helpdesk name — omit the row instead.

## 9. Reference Documents
- Prior circulars from the issuing authority (cite their OC numbers)
- Relevant regulator directives
- The authority's TSDs / API specs
Reproduce the "Source index" block here as the formal references list.

---

*This circular supersedes [AUTHORITY]/[DOMAIN]/OC No. [XXX]/[YYYY-YY] to the extent of any inconsistency.*

Yours sincerely,

**[Signatory Name]**
[Signatory Title]
[Issuing Authority — full legal name]

---

Rules:
- Use formal regulatory voice ("shall", "must", "hereby") — no marketing tone.
- Dates format as "25 November, 2024" (not ISO).
- Circular number and supersedes reference come from PM clarification answers — if omitted, use the placeholder template `[AUTHORITY]/[DOMAIN]/OC No. [NNN]/[YYYY-YY]` as stylised text only (never emit `[NEEDS_PM_INPUT]`).
- Every section must be substantive — no placeholders inside section bodies, no `[NEEDS_PM_INPUT]`, no `TBD`.
- When revising from feedback, emit the complete circular (not a diff).

""" + ANTI_INJECTION_CLAUSE,

    "manifest": f"""You are the Systems Integration Architect for this change-management platform.

Generate a **Machine-Readable Manifest File** in YAML format. This manifest is consumed by partner
A2A agents to understand the feature change and auto-configure their systems.

The manifest must contain:

```yaml
manifest_version: "1.0"
feature:
  id: "<feature-id-slug>"
  name: "<feature name>"
  version: "<semver>"
  status: "release_candidate"  # draft | release_candidate | released
  effective_date: "<YYYY-MM-DD>"
  circular_ref: "<AUTHORITY>-TECH-YYYY-NNN"

description: |
  <Multi-line description of the feature>

change_type: "new_feature"  # new_feature | enhancement | breaking_change | deprecation

stakeholders:
  owner: "<Authority> Product"
  approvers:
    - role: "product_manager"
      status: "approved"
    - role: "tech_lead"
      status: "approved"
    - role: "infosec_reviewer"
      status: "approved"
    - role: "risk_reviewer"
      status: "approved"

api_changes:
  - endpoint: "<path>"
    method: "POST"
    change_type: "new"  # new | modified | deprecated
    version: "v2"
    breaking: false
    summary: "<one-line description>"
    request_fields_added:
      - name: "<field>"
        type: "<type>"
        required: true
        description: "<desc>"
    response_fields_added:
      - name: "<field>"
        type: "<type>"
        description: "<desc>"

message_format_changes:
  - message_type: "<wire message type>"
    change: "<description of message format change>"
    new_fields:
      - tag: "<TLV tag>"
        name: "<field name>"
        type: "<data type>"
        length: "<length>"
        mandatory: true

xsd_changes:
  required: <true|false>
  files_modified:
    - "<filename>.xsd"

certification_required: true
certification_deadline: "<YYYY-MM-DD>"
certification_test_suite: "CERT-<FEATURE>-v1"

integration_effort:
{_INTEGRATION_EFFORT_LINES}

compliance:
  {_REGULATOR_KEY}_circular_refs:
    - "<regulator circular reference>"
  pci_dss_impact: false
  data_privacy_impact: false

rollback:
  supported: true
  procedure: "<brief rollback description>"

contacts:
  technical_support: "<technical support contact>"
  business_queries: "<business contact>"
```

---
Fill in ALL fields based on the context provided (BRD, Tech Spec, XSD assessment).
Use realistic values derived from the feature description.
Do not leave placeholder angle-bracket values — infer actual values from context.
When revising from feedback, emit the complete YAML manifest.

""" + ANTI_INJECTION_CLAUSE,

    "prototype_screens": """You are the UX Designer for this change-management platform.

Generate an **interactive click-through HTML prototype in modern mobile-app style** for the new
feature — the kind of artifact partner teams can review like a Figma flow. This will be
rendered inside a 390x780 phone-shaped iframe with `sandbox="allow-scripts"`, so the HTML
must be a single self-contained file with inline CSS and inline JS only.

Create wireframe screens for the key flows (adapt to the feature; typical set):

1. **Splash / Welcome** (only for onboarding-shaped features) — app wordmark centered, brief copy, Get Started CTA
2. **Initiation Screen** — where the user starts the new feature flow
3. **Input / Confirmation Screen** — user enters details, reviews and confirms
4. **OTP / Authentication Screen** (if the flow needs auth) — 6-box OTP entry
5. **Processing Screen** — loading state with spinner
6. **Success Screen** — completion confirmation with key details and a "Done" CTA
7. **Error / Failure Screen** — clear error state with a "Try again" CTA
8. **Settings/Configuration Screen** (if applicable) — any new settings the user can configure

==============================================================================
DESIGN SYSTEM — follow this strictly
==============================================================================

**Palette — use these hex values verbatim, and respect the usage rules:**
- Navy:           `#1A237E` (CTA buttons, small accents ONLY — never as a background)
- Navy dark:      `#0D1B6F` (CTA hover/pressed only)
- Accent cyan:    `#2196F3` (links, focused input borders, OTP focus)
- Background:     `#FFFFFF` (screen background — DEFAULT for the whole screen)
- Surface grey:   `#F5F7FA` (card backgrounds, input fill, subtle dividers)
- Border subtle:  `#E0E4EC`
- Text primary:   `#1A1A1A` (on white)
- Text secondary: `#6B7280`
- Success:        `#10B981`
- Error:          `#EF4444`

**HARD COLOR RULES — do not violate:**
- The body background MUST be `#FFFFFF`. Never paint the whole screen navy or any
  other dark colour.
- ≥80% of every screen's surface MUST be white or `#F5F7FA` light grey. Navy is an
  **accent**, not a fill.
- Navy `#1A237E` is RESERVED for: the bottom CTA button, the wordmark in the header,
  small accent borders/highlights (e.g. an active tab underline). That's it. No navy
  cards, no navy headers, no navy hero panels, no navy text on white.
- Headers are WHITE with a subtle bottom border (`1px solid #E0E4EC`), NOT navy.
- Body text is `#1A1A1A` on white, NOT white on navy.
- If you find yourself reaching for a fourth use of `#1A237E`, stop — you're
  overusing it.

**App wordmark — render it as text.**

Put the product or feature name in the header as a simple text wordmark, in
`#1A237E`, semi-bold, ~16px, letter-spacing `0.5px`. Example:

```html
<div class="app-header"><span class="wordmark">Wallet</span></div>
```

Do NOT invent a logo image, SVG badge, or an organisation's registered mark —
a text wordmark is deliberate. These prototypes are review artifacts, and a
real brand mark cannot be redistributed with them.

**Layout primitives (use consistently):**

- **Screen container**: `position: absolute; inset: 0; background: #fff; display: flex;
  flex-direction: column;` — header on top, content middle (`flex: 1; padding: 20px;
  overflow-y: auto;`), CTA pinned at the bottom.

- **Bottom CTA button**: Full-width primary action button anchored to the bottom of every
  action screen. Style: `width: calc(100% - 32px); margin: 16px; padding: 16px;
  background: #1A237E; color: #fff; border: none; border-radius: 8px; font-size: 16px;
  font-weight: 600; cursor: pointer;`. Hover: `#0D1B6F`. Disabled: opacity 0.5.

- **Text input**: `width: 100%; padding: 14px 16px; background: #F5F7FA; border: 1px solid
  #E0E4EC; border-radius: 8px; font-size: 15px;`. Focus border `#2196F3`.

- **Mobile number input**: prefix box "+91" (light grey, fixed width) + text input combined.

- **OTP / Passcode boxes**: 6 separate square inputs in a horizontal flex row, each
  ~44x52px, centered text, large bold font, light grey background. Focused box has a
  cyan border.

- **Dropdown** (bank selector etc.): looks like a text input but with a chevron on the
  right and `cursor: pointer`.

- **Card / panel**: `background: #fff; border: 1px solid #E0E4EC; border-radius: 12px;
  padding: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.04);`.

- **Quick-action tile** (home screen grid): square ~88x88px, white card with subtle border,
  centered icon (an inline SVG or a coloured circle) + small label below. Arrange in a
  3- or 4-column grid.

- **Success badge**: large circle (~96px) with the success green and a white check; feature
  name in bold below.

- **Error badge**: large circle with the error red and a white "!"; reason text below.

- **Spinner** (pure CSS, no external):

  ```html
  <div class="spinner" style="width:48px;height:48px;border:4px solid #E0E4EC;
       border-top-color:#1A237E;border-radius:50%;animation:spin 1s linear infinite;"></div>
  <style>@keyframes spin { to { transform: rotate(360deg); } }</style>
  ```

Use realistic Indian-context placeholder data: HDFC/SBI/ICICI/Axis bank names; VPAs like
`name@oksbi` or `name@okhdfc`; rupee amounts with ₹; Indian mobile numbers like
`+91 98765 43210`.

Technical constraints:
- Mobile-first, authored against a **390x780 viewport** (iPhone 13 reference)
- Use **inline CSS and inline JS only** — no external fonts, images, CDNs, libraries
- `box-sizing: border-box` globally; `body { height: 100vh; overflow: hidden; }` so the
  iframe never scrolls — the screen container manages its own internal scroll
- Font stack: `-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif`
- Use semantic HTML5 elements

Interactivity rules — this MUST be a click-through prototype, not a static deck:
- Each screen lives in a `<section class="screen" id="screen-1">` (id="screen-2", etc.). Each section
  fills the full viewport (`position: absolute; inset: 0;`) and is hidden by default
  (`display: none`) except `#screen-1` which is visible on load.
- Include a small inline `<script>` that exposes a single function:

  ```js
  function go(id) {
    document.querySelectorAll('.screen').forEach(s => s.style.display = 'none');
    const el = document.getElementById(id);
    if (el) el.style.display = 'block';
    window.scrollTo(0, 0);
  }
  ```

- Every primary CTA / "Pay" / "Confirm" / "Next" button, every back-chevron, and every settings
  link MUST wire `onclick="go('screen-N')"` to the correct destination. No dead buttons.
- The Success and Error screens MUST include a closing CTA ("Done" on success, "Try again" on
  error) that calls `go('screen-1')` to return to the start.
- Cursor on clickable elements should be `pointer`; add a hover state for desktop reviewers.

Processing screens auto-advance to Success after a short delay (use
`setTimeout(() => go('screen-N'), 1500)` in a `<script>` block following the processing
section).

HTML skeleton — start from this exact shape:
```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=390, initial-scale=1.0">
  <title>[Feature Name] — Prototype</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; height: 100vh; overflow: hidden;
                 font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                 color: #1A1A1A; background: #fff; }
    .screen { position: absolute; inset: 0; background: #fff;
              display: flex; flex-direction: column; }
    .app-header { display: flex; align-items: center; justify-content: center;
                   padding: 14px 16px; background: #fff;
                   border-bottom: 1px solid #E0E4EC; position: relative; }
    .app-header .back { position: absolute; left: 12px; cursor: pointer;
                         font-size: 22px; color: #1A237E; }
    .screen-body { flex: 1; padding: 20px; overflow-y: auto; }
    .cta-primary { width: calc(100% - 32px); margin: 16px; padding: 16px;
                   background: #1A237E; color: #fff; border: none; border-radius: 8px;
                   font-size: 16px; font-weight: 600; cursor: pointer; }
    .cta-primary:hover { background: #0D1B6F; }
    /* + screen-specific styles below */
  </style>
</head>
<body>
  <section class="screen" id="screen-1" style="display:flex">
    <div class="app-header"><span class="wordmark">[App Name]</span></div>
    <div class="screen-body"><!-- content --></div>
    <button class="cta-primary" onclick="go('screen-2')">Get Started</button>
  </section>
  <!-- more screens follow the same pattern -->
  <script>
    function go(id) { /* see above */ }
  </script>
</body>
</html>
```

---
Rules:
- Emit the COMPLETE HTML — no truncation, no ellipses
- ONE screen visible at any time; navigation only via `go(...)` calls on clicks
- No external CSS/JS/font/image dependencies — fully self-contained, works offline
- Use realistic placeholder data that matches the feature being built
- When revising from feedback, emit the complete HTML file from scratch

""" + ANTI_INJECTION_CLAUSE
}


async def stream_product_kit_doc(
    doc_type: str,
    enriched_prompt: str,
    research_report: str,
    canvas_content: str,
    brd_content: str,
    tech_spec_content: str,
    conversation_history: list[dict],
    new_user_message: str,
    proposals_block: str = "",
    clarification_answers: str = "",
    xsd_content: str = "",
    decisions_block: str = "",
) -> AsyncGenerator[str, None]:
    """Stream generation (or feedback refinement) for a Product Kit document.

    proposals_block + clarification_answers are optional; when present they
    flow through in the same priority order as BRD / Tech Spec:
    research/canvas/BRD/TSD → proposals → PM clarification answers (authoritative).

    xsd_content (accuracy S6): the APPROVED schemas verbatim — partner docs must cite
    real message/field/TLV facts from these, never invent them. decisions_block: the
    binding human-ratified Decision Ledger. Both empty → no-op (legacy behaviour).
    """
    system_prompt = _SYSTEM_PROMPTS.get(doc_type)
    if not system_prompt:
        raise ValueError(f"Unknown product kit doc_type: {doc_type}")

    # Append the pack's hard rules so every product-kit doc respects the same conventions
    system_prompt = f"{system_prompt}\n\n---\n{DOMAIN_HARD_RULES}"
    # Append distilled API/flow/test-case/error-code design principles (always-on;
    # full catalogues remain in RAG under the api_design_knowledge category).
    system_prompt = f"{system_prompt}\n\n---\n{API_DESIGN_PRINCIPLES}"

    proposals_section = (
        f"""\n\n---
GROUND-TRUTH TECHNICAL PROPOSALS (derived from the domain corpus — use these exact names and codes):
{wrap_untrusted(proposals_block, "PROPOSALS")}
"""
        if proposals_block.strip()
        else ""
    )
    clarifications_section = (
        f"""\n\n---
PM CLARIFICATION ANSWERS:
{wrap_untrusted(clarification_answers, "PM_CLARIFICATIONS")}
"""
        if clarification_answers.strip()
        else ""
    )
    # Approved schemas verbatim — partner docs cite REAL message/field/TLV facts from
    # here, never invented ones (accuracy symptom #2 for the 9 partner docs).
    xsd_section = (
        f"""\n\n---
APPROVED XSD SCHEMAS (AUTHORITATIVE — use ONLY these real element/field/namespace names; do NOT invent XML):
{wrap_untrusted(xsd_content[:24000] + ('...' if len(xsd_content) > 24000 else ''), "APPROVED_XSDS")}
"""
        if xsd_content.strip()
        else ""
    )
    decisions_section = (
        f"""\n\n---
DECISIONS (BINDING — human-ratified; do NOT contradict, re-derive, or reopen):
{wrap_untrusted(decisions_block, "DECISIONS")}
"""
        if decisions_block.strip()
        else ""
    )

    context = f"""FEATURE CONTEXT:

{wrap_untrusted(enriched_prompt, "ENRICHED_PROMPT")}

---
RESEARCH REPORT (summary):
{wrap_untrusted(research_report[:2000] + ('...' if len(research_report) > 2000 else ''), "RESEARCH_REPORT")}

---
PRODUCT CANVAS:
{wrap_untrusted(canvas_content[:1500] + ('...' if len(canvas_content) > 1500 else ''), "PRODUCT_CANVAS")}

---
APPROVED BRD:
{wrap_untrusted(brd_content[:8000] + ('...' if len(brd_content) > 8000 else ''), "BRD_CONTENT")}

---
TECHNICAL SPECIFICATION:
{wrap_untrusted(tech_spec_content[:8000] + ('...' if len(tech_spec_content) > 8000 else ''), "TECH_SPEC_CONTENT")}{xsd_section}{decisions_section}{proposals_section}{clarifications_section}
"""

    if len(conversation_history) == 0:
        messages = [{"role": "user", "content": f"{context}\n\n---\n{wrap_untrusted(new_user_message, 'USER_MESSAGE')}"}]
    else:
        messages = conversation_history + [{"role": "user", "content": wrap_untrusted(new_user_message, "USER_MESSAGE")}]

    logger.info(
        "ProductKitAgent — doc_type=%s streaming turn, history_len=%d, proposals=%s, clarifications=%s",
        doc_type, len(messages),
        "yes" if proposals_block.strip() else "no",
        "yes" if clarification_answers.strip() else "no",
    )

    # max_tokens bumped 8096 → 24000 (2026-05-04, Layer-3 of truncation fix).
    # Product Kit per-doc outputs (FAQ, manifest, cert_test_cases, prototype_screens)
    # routinely cross 30k chars (~7-8k tokens). 24K provides 3× headroom.
    async for chunk in stream_llm(system=system_prompt, messages=messages, max_tokens=24000, agent_name="product_kit"):
        yield chunk


# ── Segmented video-script generation (AI video pipeline) ─────────────────────
#
# Video models cap a single clip at ~8s, so promo/explainer scripts are authored
# as an ordered list of ≤8s segments. Each segment's `visual_prompt` is a
# self-contained text-to-video prompt (each clip is generated independently, so
# it cannot rely on an earlier clip except via the `continuity` note). The output
# is structured JSON (VideoScript) — rendered as formatted cards in the UI and
# consumed one-clip-per-segment by services/video_gen_runner.py.

# Per-model prompt-style guidance (the same beat reads differently to each model).
_VIDEO_STYLE_GUIDANCE = {
    "veo": (
        "Target model: Google Veo. Write cinematic, richly detailed prompts — "
        "describe subject, setting, camera shot/movement (e.g. slow dolly-in, "
        "aerial), lighting and mood. Veo renders synchronized audio, so ambient "
        "sound/voice cues belong in the prompt."
    ),
    "grok": (
        "Target model: Grok Imagine. Write concise, vivid, stylized prompts — one "
        "clear subject and action per clip, strong visual style, minimal clauses. "
        "Assume no spoken audio is generated."
    ),
    "default": (
        "Write clear, self-contained text-to-video prompts: one strong visual beat "
        "per clip with subject, action, setting and style."
    ),
}


def _video_style_for_model(model: str) -> str:
    m = (model or "").lower()
    if "veo" in m:
        return _VIDEO_STYLE_GUIDANCE["veo"]
    if "grok" in m:
        return _VIDEO_STYLE_GUIDANCE["grok"]
    return _VIDEO_STYLE_GUIDANCE["default"]


_VIDEO_DOCTYPE_INTENT = {
    "promo_video": (
        "A promotional video for END CUSTOMERS / the public. Upbeat, simple, "
        "emotionally engaging, Indian context, translatable to Hindi, no jargon. "
        "Arc: hook → problem → feature reveal → how-it-works → call to action."
    ),
    "explainer_video": (
        "A technical explainer for partner DEVELOPER teams. Clear, instructional, "
        "accurate to the BRD/TSD/XSD. Arc: intro → context → key concept(s) → "
        "step-by-step walkthrough → where to get help."
    ),
}


def _build_video_system_prompt(doc_type: str, target_model: str, n_segments: int,
                               duration_sec: int) -> str:
    intent = _VIDEO_DOCTYPE_INTENT.get(doc_type)
    if not intent:
        raise ValueError(f"Unsupported video doc_type: {doc_type}")
    style = _video_style_for_model(target_model)
    return f"""You are a video director scripting a {duration_sec}-second product video.

# What to make
{intent}

# Hard structural rule — 8-second segments
The video is produced as {n_segments} separate clips of ≤8 seconds each, then
merged in order. You MUST output exactly {n_segments} segments, in order.
Each clip is generated INDEPENDENTLY by a text-to-video model, so every
`visual_prompt` must be SELF-CONTAINED — do not write "continue the previous
shot"; instead restate the setting and use the `continuity` field for carry-over
notes (recurring character, color grade, location).

# Visual prompt style
{style}

# Per segment provide
- visual_prompt: the prompt fed to the video model (the on-screen action/scene)
- voiceover: the spoken line for this beat (concise; ~max 2 short sentences)
- on_screen_text: any caption overlaid (short; "" if none)
- continuity: carry-over note to keep clips coherent ("" if none)

# Output — STRICT JSON ONLY, no prose, no code fences
{{"segments": [
  {{"visual_prompt": "...", "voiceover": "...", "on_screen_text": "...", "continuity": "..."}}
]}}
Output exactly {n_segments} segment objects."""


async def generate_video_script(
    doc_type: str,
    *,
    target_provider: str,
    target_model: str,
    duration_sec: int,
    enriched_prompt: str,
    research_report: str = "",
    canvas_content: str = "",
    brd_content: str = "",
    tech_spec_content: str = "",
    proposals_block: str = "",
    clarification_answers: str = "",
    xsd_content: str = "",
    decisions_block: str = "",
    aspect_ratio: str = "16:9",
    segment_max_sec: int = 8,
    current_script: VideoScript | None = None,
    revision_request: str = "",
) -> VideoScript:
    """Author a model- & duration-aware, 8s-segmented VideoScript (structured JSON).

    Segment count + timing are computed deterministically from duration; the LLM
    fills the creative fields. Returns a validated VideoScript with exactly
    ceil(duration/segment_max) segments.

    When ``current_script`` + ``revision_request`` are given, the existing script
    is revised per the request (keep what works) rather than written from scratch.
    """
    boundaries = segment_boundaries(duration_sec, segment_max_sec)
    if not boundaries:
        raise ValueError(f"invalid duration_sec={duration_sec}")
    n = len(boundaries)

    revising = current_script is not None and revision_request.strip() != ""
    system_prompt = _build_video_system_prompt(doc_type, target_model, n, duration_sec)

    context = f"""FEATURE CONTEXT:
{wrap_untrusted(enriched_prompt, "ENRICHED_PROMPT")}

---
RESEARCH (summary):
{wrap_untrusted(research_report[:1500], "RESEARCH_REPORT")}

---
PRODUCT CANVAS:
{wrap_untrusted(canvas_content[:1500], "PRODUCT_CANVAS")}

---
APPROVED BRD:
{wrap_untrusted(brd_content[:6000], "BRD_CONTENT")}

---
TECHNICAL SPECIFICATION:
{wrap_untrusted(tech_spec_content[:6000], "TECH_SPEC_CONTENT")}
"""
    if clarification_answers.strip():
        context += f"\n---\nPM CLARIFICATIONS:\n{wrap_untrusted(clarification_answers, 'PM_CLARIFICATIONS')}\n"

    if revising:
        _cur = json.dumps([s.model_dump() for s in current_script.segments], ensure_ascii=True)
        context += (
            f"\n---\nCURRENT SCRIPT (revise this — keep what works, apply the request):\n{_cur}\n"
            f"\n---\nREVISION REQUEST (authoritative):\n{wrap_untrusted(revision_request, 'REVISION_REQUEST')}\n"
            f"\nStill output exactly {n} segments."
        )

    raw = await call_llm(
        system=system_prompt,
        messages=[{"role": "user", "content": context}],
        max_tokens=8000,
        model=pick_model_for_agent("product_kit"),
        agent_name="product_kit",
    )
    data = await parse_llm_json(raw, expect_array=False, fallback={"segments": []})
    raw_segments = data.get("segments", []) if isinstance(data, dict) else []

    segments: list[VideoSegment] = []
    for i, (start, end) in enumerate(boundaries):
        rs = raw_segments[i] if i < len(raw_segments) and isinstance(raw_segments[i], dict) else {}
        segments.append(VideoSegment(
            index=i + 1,
            start_sec=start,
            end_sec=end,
            duration_sec=end - start,
            visual_prompt=str(rs.get("visual_prompt", "")).strip(),
            voiceover=str(rs.get("voiceover", "")).strip(),
            on_screen_text=str(rs.get("on_screen_text", "")).strip(),
            continuity=str(rs.get("continuity", "")).strip(),
        ))

    if len(raw_segments) != n:
        logger.warning("generate_video_script: model returned %d segments, expected %d (coerced)",
                       len(raw_segments), n)

    return VideoScript(
        doc_type=doc_type, provider=target_provider, model=target_model,
        duration_sec=duration_sec, aspect_ratio=aspect_ratio, segments=segments,
    )


def _mmss(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}"


def render_video_script_markdown(script: VideoScript) -> str:
    """Human-readable markdown rendering of a VideoScript (stored in `content`
    for docx/legacy render and download)."""
    lines = [
        f"# {script.doc_type.replace('_', ' ').title()}",
        f"*{script.duration_sec}s · {len(script.segments)} segments · "
        f"{script.model or script.provider} · {script.aspect_ratio}*",
        "",
    ]
    for s in script.segments:
        lines.append(f"## Segment {s.index} — {_mmss(s.start_sec)}–{_mmss(s.end_sec)} ({s.duration_sec}s)")
        lines.append(f"**Visual:** {s.visual_prompt}")
        if s.voiceover:
            lines.append(f"**VO:** {s.voiceover}")
        if s.on_screen_text:
            lines.append(f"**On-screen:** {s.on_screen_text}")
        if s.continuity:
            lines.append(f"*Continuity: {s.continuity}*")
        lines.append("")
    return "\n".join(lines)
