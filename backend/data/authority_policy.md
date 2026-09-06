---
maintainer: Authority Policy Team
profile_version: 0.1-research-draft
generated_by: claude-opus-deepresearch
generated_at: 2026-05-27
confidence_overall: low
sources_consulted: 31
---

# Authority Policy — Change-Management Resolver Brief

> Draft researched from public sources. The Authority does not publish its internal
> change-management negotiation playbook; most of the patterns below are
> inferred from observable rollout behaviour or from RBI/Authority documents
> that constrain the playbook without describing it. Sections marked
> `<TO CONFIRM WITH AUTHORITY POLICY TEAM>` must be reviewed and authored
> by the Authority policy team before this file is trusted by the resolver.
> Where this file proposes a default tolerance or threshold, it is tagged
> `[PROPOSED DEFAULT]` and should be treated as a starting point only.

---

## Quick reference

A TL;DR for downstream LLMs reading this file as context.

- **the Authority sits below RBI.** RBI Master Directions (DPSC 2021, KYC, PSS
  Act 2007) set the floor; the Authority OCs and the network Procedural Guidelines
  operate within it. RBI-MD-mandated items are non-negotiable.
  [src: RBI DPSC MD, 18 Feb 2021]
- **License-bound role assignments are not negotiable.** TPAP can't
  become PSP; only RBI-authorised entities can hold license-tier roles.
  [src: The Authority R&R doc, perennial; the Authority RuPay-CC circular, 4 Oct 2022]
- **P2P limits are a hard ceiling; P2M limits are category-based and
  movable.** P2P stays at ₹1 lakh/day. P2M is ₹5L per txn / ₹10L per day
  for verified categories (capital markets, insurance, GeM, CC bills)
  from 15 Sep 2025. [src: Outlook Money / NewsOnAir, Sep 2025]
- **the Authority launches everything in cohort/pilot mode first.** The network itself: 21
  banks (Apr 2016). v2.0 of the network: ~11 banks. PayNow-the network: 6 → 19 banks.
  RuPay-CC-on-network: a handful of issuers at pilot. [src: The Authority product
  page; MAS Feb 2023; Inc42 Oct 2022]
- **the Authority prefers deadline extensions over scope cuts.** 30% TPAP cap:
  Dec-2022→Dec-2024→Dec-2026 rather than repeal. [src: Inc42, Head and
  Tale, Dec 2024 / Jan 2025]
- **Systemic readiness gates production cutover.** SEBI activated ₹5L
  IPO ASBA on 1 May 2022 only after the Authority confirmed >80% SCSB/Sponsor
  Bank/the network App readiness on 30 Mar 2022. [src: SEBI circular 8 Mar 2022]
- **Compliance dates can shift via fresh circular when readiness lags.**
  Settlement-cycle segregation cutover moved 3 Nov 2025 → 15 Dec 2025
  via addendum. [src: The Authority/2025-26/the network/222A, 29 Oct 2025]
- **Outage RCAs become spec changes.** After 12 Apr 2025: Check
  Transaction Status restrictions, response-time SLA tightening for
  Pay/Status/Reversal/ValidateAddress. Spec was hardened from "bank
  should implement" to "the Authority will enforce". [src: The Authority RCA via
  Kapronasia, adityakulkarni.substack.com, Apr–Jun 2025]
- **the network Steering Committee is the formal forum** for systemic decisions,
  meeting at least quarterly. Penalties and termination recommendations
  sit there. [src: The network Procedural Guidelines; Steering Committee minutes
  on the Authority's website]
- **Cross-border features sit with NIPL, not domestic the network.** PayNow-the network,
  the network-UAE, the network-Malaysia all NIPL-led; domestic precedent doesn't auto-
  apply. [src: MAS Feb 2023; PRNewswire Jul 2025]
- **Round-window timing is not a published the Authority concept.** The 2-round
  cycle is the *platform's* design. Defaults below are `[PROPOSED
  DEFAULT]`. `<TO CONFIRM WITH AUTHORITY POLICY TEAM>`
- **Escalation default**: PM → PM+Compliance/Architecture → PM+Legal →
  SVP/Executive. Triggers inferred (see §4).
- **the Authority consolidates contested asks rather than 1:1 reply.** Multiple
  partners asking the same thing → FAQ/addendum/fresh OC, not
  individual responses. [inferred from rollout pattern]
- **Versioning**: addendum suffix ("OC 151A", "OC 163A", "OC 222A").
  Parent circular stays in force; addendum modifies. [src: observed
  the Authority numbering 2022–2025]

---

## 1. Non-negotiable principles

The resolver should *never* recommend conceding on the items below,
regardless of partner framing.

### 1.1 RBI-floor controls

Mandated by RBI Master Directions; the Authority operates within them. A
partner ask that violates these gets escalated, not negotiated.

- **Digital Payment Security Controls (RBI DPSC 2021).** Board-approved
  digital-payments policy, UAT-before-rollout, fraud-risk management,
  AFA/2FA, mobile/internet banking security controls, grievance-redressal
  mechanism. [src: RBI DPSC MD, 18 Feb 2021]
- **KYC Master Direction.** The network onboarding rests on bank-account-grade
  KYC; the Authority cannot relax via a feature design. [src: RBI KYC MD]
- **PSS Act 2007 Sections 10/10A.** Zero-MDR regime for the reference app-the network and
  RuPay debit (since Jan 2020) flows from Section 10A PSS Act and
  Section 269SU IT Act 1961. The Authority cannot reintroduce MDR on standard
  the network P2M via negotiation. [src: PIB PRID 2114335, 24 Mar 2025]
- **Data localisation.** All the network payment/transaction data stored in
  India by PSPs and TPAPs. Flows from RBI 6 Apr 2018 directive.
  [src: The Authority TPAP R&R doc]
- **Authentication (AFA).** Cannot be removed except via narrow
  exemptions already in the Authority/RBI circulars (recurring RuPay-CC
  ≤₹15,000; the Lite wallet under RBI Offline Framework Jan 2022 as amended).
  [src: The Authority RuPay-CC-on-network 4 Oct 2022; RBI Offline Framework]

### 1.2 License-bound role assignments

Cert-role assignments map 1:1 to license tier and cannot be negotiated
across tiers:

- **PSP role**: only RBI-authorised PSP bank. A TPAP asking the Authority to
  let it become a Payer PSP is asking for something the Authority cannot grant
  alone. [src: The Authority R&R doc]
- **TPAP role**: The Authority approves; PSP onboards; TPAPs cannot directly
  access the network switch.
- **Issuer Bank**: RBI-authorised banks only.
- **PPI Issuer**: RBI-authorised PPI issuers only; the 1.1% PPI-on-the network
  interchange (>₹2,000, from 1 Apr 2023) is set by the Authority circular, not
  bilateral. [src: Business Standard 29 Mar 2023]
- **Sponsor Bank (ASBA/IPO)**: SEBI-registered Banker-to-the-Issue.
  the Authority cannot reassign via network-side negotiation. [src: SEBI Operational
  Circular 8 Mar 2022]

### 1.3 Schema and interoperability minimums

- **Interoperability features for the reference app/the network apps.** Send/receive by VPA,
  BharatQR/the network QR, intent-call response — mandatory since 16 Apr 2018.
  the Authority/PSPs may decline txns from non-compliant apps. [src: The Authority
  Interoperability Guidelines, 16 Mar 2018]
- **Multi-bank model rules.** Customer-payment-sensitive data only in
  PSP-bank systems; PSP bank board approval; board resolution to the Authority
  at onboarding. [src: The network OC No. 32, 15 Sep 2017]
- **Settlement cycle structure.** 10 cycles per day via RTGS. From
  15 Dec 2025, AUTH and Dispute settle only in DC1 (11th) and DC2
  (12th). Structure is not negotiable. [src: The Authority/2025-26/the network/222A,
  29 Oct 2025]

### 1.4 P2P limit

- **₹1 lakh/day.** Has held through multiple P2M revisions — deliberate
  consumer-protection ceiling. P2M moves with merchant category; P2P
  does not. [src: Outlook Money, Paytm blog, Cleartax, Sep 2025]

---

## 2. Standard tolerances per category

The partner-side analyser enumerates 6 negotiable categories. Observed
historical tolerance documented per category; where no public source
exists, `[PROPOSED DEFAULT]` is given for the Authority Policy to refine.

### 2.1 Production deadline

- **Observed pattern.** Two-year extensions for ecosystem-wide compliance
  where 80%+ readiness isn't achieved (30% TPAP cap:
  Dec-2022→Dec-2024→Dec-2026). For narrower technical cutovers, the Authority
  extends ~6 weeks via addendum when member feedback flags readiness
  gaps (settlement-cycle segregation: 3 Nov→15 Dec 2025). For SEBI-
  coordinated the network changes, the Authority waits for 80%+ readiness before
  greenlighting (IPO ASBA ₹5L: The Authority Dec 2021, SEBI activated 1 May 2022
  after readiness confirmed 30 Mar 2022).
- **PM-level**: `[PROPOSED DEFAULT]` ±14 days. Accept silently.
- **Compliance/architecture**: `[PROPOSED DEFAULT]` 15–60 days; issue
  addendum if cluster threshold hit (§6).
- **SVP escalation**: >60 days; OR any ask where the date is regulator-
  coordinated; OR slipping breaches RBI MD timeline.

### 2.2 Scope (flows / channels / segments)

- **Observed pattern.** The Authority launches with reduced cohort and broadens
  later. The network: 21 banks. v2.0 of the network: ~11. PayNow-the network: 6→19. RuPay-CC-on-network:
  public-sector issuers + Axis at pilot. Does *not* drop mandatory
  regulatory flows to shrink scope.
- **PM-level**: Accept smaller participant cohort, MCC list, or fewer
  channels (e.g. defer 123Pay IVR while shipping app-based), provided
  mandatory regulatory flows intact.
- **Compliance escalation**: scope cut removing AFA, audit, AML, or
  grievance-redressal.
- **Architecture escalation**: scope cut that creates schema asymmetry
  between live-in-prod and still-in-cert participants.

### 2.3 Limits and thresholds

- **Observed pattern.** Limits move via circular, often after RBI MPC
  signal. Path: RBI announces → the Authority OC → banks implement. the Lite wallet:
  ₹200/₹2k → ₹500/₹2k → ₹1k/₹5k (Dec 2023 amendment of Jan 2022 Offline
  Framework). AutoPay AFA: ₹5k → ₹15k (OC 151). IPO ASBA: ₹2L → ₹5L
  (OC 127). P2M verified categories: ₹5L/₹10L (15 Sep 2025).
- **PM-level**: Banks setting *lower* internal caps within the Authority ceiling
  → accept; partner's prerogative.
- **PM + Architecture**: Raising above ceiling, or new category with
  custom ceiling → fresh OC, not 1:1 reply.
- **SVP / Legal**: Lowering a regulatory floor (P2P ₹1L ceiling,
  recurring-CC AFA threshold) → refuse.

### 2.4 Technical spec / API contract

- **Observed pattern.** Spec lives in BRD/TSD/Manifest chain; enforced
  via cert TCs. Post-incident reviews drive sharper spec, not partner
  negotiation: after 12 Apr 2025 outage the Authority tightened response times
  for Pay/Status/Reversal/ValidateAddress (deadline 16 Jun 2025) and
  restricted Check Transaction Status (max 3 calls, 90s gap), shifting
  the limiter from "bank should implement" to "the Authority will enforce".
- **PM-level**: Wording clarification, example payload correction,
  field-length clarification — accept and publish as BRD addendum or
  FAQ. `[PROPOSED DEFAULT]` SLA for clarifying changes: 7–14 days.
- **PM + Architecture**: Behaviour clarification that changes observable
  interaction; new error code; timeout semantics change.
- **Architecture refusal**: Schema-version compatibility break inside a
  published manifest version. The Authority versions the manifest forward instead
  of mutating; this minimum is non-negotiable.

### 2.5 Upstream dependency timelines

- **Observed pattern.** Where a network feature depends on SEBI/RBI/IRDAI
  upstream, the Authority coordinates cutover (IPO ASBA; SEBI @validbankpsp
  handle Jun 2025; the Lite wallet e-mandate auto-replenishment from Jun 2024
  RBI MPC). Partner-side vendor slips handled via coordinated extension
  without penalty *if* signaled early and bounded.
- **PM-level**: Coordinated extension for single named vendor blocker,
  ≤30 days, no penalty, conditional on written remediation plan.
- **PM + Compliance**: Security-control vendor (HSM, KMS, fraud engine)
  blockers — RBI DPSC compliance signoff needed before extension.
- **SVP**: Slip on a regulator-driven date.

### 2.6 Certification role assignments

- **Observed pattern.** Cert roles map 1:1 to license type. TPAP cannot
  take Payer-PSP cert. Sponsor Bank cert role is SEBI-bound, not Authority-
  reassignable.
- **PM-level**: Within a license tier, accept partner swapping which
  of their *own* certified entities runs a given cert (e.g. multi-bank
  PSP rotating which PSP-bank fronts a TPAP) — internal to partner.
- **Refusal**: Any ask to take a cert role for which the partner does
  not hold the matching license.

---

## 3. Standard response patterns by query type

Load-bearing for the resolver. For each common partner ask: (a) typical
The Authority response, (b) precedent, (c) recommended resolver disposition.

### 3.1 "We need missing spec X"

- **Typical response**: BRD/TSD addendum or FAQ. The Authority consolidates
  rather than 1:1.
- **Precedent**: OC 151A (the network AutoPay), OC 163A (the network logo positioning),
  OC 222A (settlement-cycle segregation) — all use the "A-suffix"
  pattern.
- **Resolver disposition**: Re-open BRD clarification stage *if* >1
  partner has asked the same thing OR the gap is material. If trivial
  and isolated, PM replies directly; log for consolidated addendum
  at round close.

### 3.2 "Push deadline by N days"

- **Typical response**: Accept silently for small slips; addendum for
  larger slips; multi-year extension for systemic non-readiness.
- **Precedent**: 30% TPAP cap (Dec 2022 → Dec 2024 → Dec 2026);
  OC 222A (3 Nov → 15 Dec 2025); IPO ASBA ₹5L coordinated extension
  to wait for 80%+ readiness.
- **Resolver disposition** `[PROPOSED DEFAULT]`:
  - N ≤ 14 days → PM accepts directly.
  - N = 15–60 days → placeholder response, consolidate at round close,
    issue addendum if cluster threshold hit (§6).
  - N > 60 days OR regulator-coordinated date → SVP + Compliance.

### 3.3 "Reduce scope to exclude flow F"

- **Typical response**: Accept if (a) mandatory regulatory flows remain
  intact, (b) cohort still achieves meaningful launch, (c) deferred
  flow picked up in follow-on cohort. Refuse if dropped flow is
  regulatory-floor OR creates asymmetry with live participants.
- **Precedent**: The network itself: 21-bank pilot. v2.0 of the network: subset of major
  banks. PayNow-the network: 6→19. The network 123Pay: Gupshup+Airtel PB and Jio PB,
  not every bank supporting every channel.
- **Resolver disposition**:
  - Mandatory regulatory flow → refuse, Compliance escalation.
  - Optional flow → accept, document as deferred-to-cohort-2.
  - Asymmetry-creating → Architecture escalation.

### 3.4 "Modify cap (per-txn / daily / fee)"

- **Typical response**: Within Authority-prescribed ceiling, banks set lower
  caps without coordination. Raising above ceiling needs a fresh OC.
  Fees are PSS Act / Section 269SU bound — bilateral fee negotiation
  out of scope.
- **Precedent**: The Authority P2M ₹5L/₹10L (15 Sep 2025) via circular, not
  bilateral. 1.1% PPI-on-the network interchange (>₹2,000) from the Authority 24 Mar
  2023 circular.
- **Resolver disposition**:
  - Lower internal cap → accept.
  - Above ceiling → Architecture + Compliance; flag for Steering
    Committee.
  - Fee structure change → Legal + Compliance; never PM-alone.

### 3.5 "Change cert role"

- **Typical response**: Refused if outside partner's license tier;
  accepted if internal re-assignment within partner's certified
  entities.
- **Precedent**: TPAP/PSP separation consistently enforced. Sponsor
  Bank role is SEBI-bound.
- **Resolver disposition**:
  - License-bound swap → refuse; Legal supplies refusal language.
  - Within-tier swap → PM accepts.

### 3.6 "Vendor readiness blocking timeline"

- **Typical response**: Coordinated extension, no penalty, if partner
  files early with remediation plan. The network-PG penalties under Steering
  Committee, historically not used punitively against good-faith
  vendor delays.
- **Resolver disposition**:
  - Early notification + ≤30-day slip → PM accepts.
  - Late notification (after deadline) → Compliance flag for audit
    trail before extension.
  - Security-control vendor (HSM, fraud, KMS) → PM + Compliance
    (RBI DPSC review).

### 3.7 "Add a new flow / channel / MCC mid-rollout"

- **Typical response**: Deferred to next release unless addition is
  small/MCC-only and spec already supports it. Mid-rollout *additions*
  are riskier than scope reductions because they perturb cert TCs
  already passed.
- **Precedent**: New flows arrive as fresh circular (Rent Repayment
  MCC 6513 initiation mode 03, Apr 2025; SEBI-intermediary
  @validbankpsp handle, Jun 2025).
- **Resolver disposition**: Placeholder response, consolidate at round
  close, feed into next release cycle rather than amending current BRD.

### 3.8 "We want to negotiate the schema-version of the manifest"

- **Typical response**: Refused. Manifest versioning is forward-only.
- **Resolver disposition**: Refuse with reference to schema-version
  compatibility minimums (§1.3). Escalate to Architecture only if
  partner appeals.

### 3.9 "Allow our app to operate without [interoperability feature]"

- **Typical response**: Refused since 16 Apr 2018; the Authority reserves right
  to decline txns from non-compliant apps.
- **Resolver disposition**: Refuse; escalate to Architecture only if
  ask is a time-bounded exception for a specific cohort.

---

## 4. Escalation matrix

The Authority publishes a network Steering Committee that handles systemic decisions
quarterly. Below the Steering Committee, day-to-day decisions sit with
the Product Manager and adjacent teams. The mapping below is *inferred*
from observable rollout behaviour and from the Authority's published governance
structure; the exact internal RACI is not public.
`<TO CONFIRM WITH AUTHORITY POLICY TEAM>`

### 4.1 PM alone

The PM should be able to dispose of these without escalation:

- Spec clarifications, FAQ items, BRD wording fixes that don't change
  observable behaviour.
- Deadline pushes ≤ 14 days where partner has filed early and ecosystem
  is otherwise on track. `[PROPOSED DEFAULT]`
- Scope reductions that drop optional flows from a partner's launch
  cohort, with the dropped flow rolling into a future cohort.
- Within-license-tier cert-role swaps.
- Bank-internal cap reductions below the Authority ceiling.
- Documentation-only artefact updates (BRD typos, broken links, version
  references).

### 4.2 PM + Compliance

Compliance gets pulled in when:

- The ask touches an RBI Master Direction (DPSC, KYC, Outsourcing of
  IT Services, PSS Act).
- The ask touches AML, fraud-risk, grievance-redressal mechanisms.
- The vendor in question is a security-control vendor (HSM, KMS,
  fraud engine).
- The slip is being requested *after* a published deadline (audit
  trail needed before granting extension).
- The ask touches data-localisation or audit-access requirements
  (the Authority/RBI right to audit TPAP/PSP).

### 4.3 PM + Architecture

Architecture gets pulled in when:

- The ask would require a schema-version mutation rather than a
  forward version bump.
- The ask would create asymmetry between live participants in
  production and partners still in cert.
- The ask is about API timeouts, retry semantics, idempotency, or
  any other behavioural contract.
- The ask requires raising a per-txn or daily cap above the existing
  the Authority ceiling (joint with Compliance).
- The ask follows a post-incident RCA item (e.g. throttling, API call
  limiters) — Architecture owns the post-RCA spec hardening.

### 4.4 PM + Legal

Legal gets pulled in when:

- The ask touches license-bound role assignments (TPAP↔PSP, Sponsor
  Bank, Issuer Bank, PPI Issuer) — refusal language must be defensible.
- The ask touches MDR/interchange/fee structure, since these are
  PSS-Act-bound.
- The ask involves a contractual matter between PSP and TPAP (the Authority's
  R&R doc explicitly requires PSP↔TPAP agreement covering data
  breach, fraud, IT Act, PSS Act, the network-PG, IPR).
- The partner has signalled they may escalate to RBI or to the
  Steering Committee themselves.
- IP / trademark items (the reference app, the network, the network AutoPay mnemonics are the Authority
  copyrighted/trademarked).

### 4.5 SVP / Executive

Executive escalation (typically COO or MD&CEO) triggers when:

- The slip is on a regulator-coordinated date (RBI MPC announcement,
  SEBI circular alignment).
- The ask is from a Tier-1 partner (top-5 PSP by volume or top-3 TPAP
  by volume) on a high-visibility feature.
- The ask would breach an RBI MD or PSS Act provision.
- The Steering Committee should ratify it before it goes back out.
- A material public-facing communication is needed (outage RCA,
  ecosystem-wide circular).
- `[PROPOSED DEFAULT]` Any single ask whose monetary impact exceeds
  a defined materiality threshold (e.g. interchange/MDR-affecting,
  cross-border-affecting).

> `<TO CONFIRM WITH AUTHORITY POLICY TEAM>` Exact role names of the
> escalation tiers (e.g. whether "Compliance" includes Risk and
> Fraud, whether "Architecture" includes the CTO office, whether
> SVP-tier maps to a specific designation), as these are internal
> to the Authority and not published.

---

## 5. Round-window timing policy

The 2-round structure is the change-management *platform's* design
choice, not an Authority-published process. The Authority's published artefacts
(the network-PG, OCs) operate on a different cadence — circular issuance,
addenda, Steering Committee quarterly meetings. The defaults below
should be read as the resolver's starting hypothesis for how to fit
the platform's 2-round structure into the Authority's observable behaviour.
`<TO CONFIRM WITH AUTHORITY POLICY TEAM>`

### 5.1 Standard round-1 window

`[PROPOSED DEFAULT]` 21 calendar days from circular/BRD publication
to partner counter-proposal cutoff. Rationale: The Authority's observable
ecosystem-readiness reviews (e.g. the 30 Mar 2022 readiness check
that gated the 1 May 2022 IPO-ASBA cutover) typically allow ~4 weeks
between publication and the gate.

### 5.2 Standard round-2 window

`[PROPOSED DEFAULT]` 14 calendar days from round-1 close to round-2
close. Round 2 is for consolidated the Authority response (FAQ, addendum,
revised BRD) and partner final position.

### 5.3 Silent acceptance

`[PROPOSED DEFAULT]` If a partner does not respond within the round
window, the Authority treats silence as acceptance of the published BRD/circular
as drafted. This aligns with the Authority's observed practice of publishing
binding circulars with a single comment window.

### 5.4 the Authority counter-back vs partner counter accept

The Authority tends to *not* counter-back individual partner asks 1:1; it
consolidates and issues an addendum. The resolver should bias toward:

- Round 1: log partner asks, do not respond bilaterally except for
  trivial clarifications.
- Round close: produce consolidated addendum (FAQ + spec addendum
  + revised manifest version if needed).
- Round 2: ask partners to ratify against the consolidated artefact.

This matches the OC-then-OC-A pattern visible in the Authority's circular
numbering.

---

## 6. Cross-partner considerations

### 6.1 Cluster fan-out thresholds

When multiple partners ask the same thing, the question stops being
bilateral and becomes systemic. The Authority's observable behaviour suggests
the threshold is low — clusters of 3–5 similar asks are typically
enough to trigger an addendum rather than 1:1 responses.

`[PROPOSED DEFAULT]`:

- **≥ 3 partners with substantively the same ask** → recommend
  consolidated addendum at round close.
- **≥ 5 partners** → recommend the addendum + targeted FAQ
  publication on the Authority's website.
- **≥ 10 partners OR ≥ 1 Tier-1 partner + ≥ 2 others** → recommend
  Steering Committee placement at the next quarterly meeting.

### 6.2 Broadcast vs 1:1

- **Broadcast (circular/addendum/FAQ)**: anything that affects spec,
  timing, or scope for the whole cohort.
- **1:1**: partner-specific cohort assignment, partner-specific
  vendor-driven extension, partner-specific cert TC clarifications.
- **Mixed**: post-incident actions — sometimes 1:1 in the moment
  (e.g. the directive to suspend Check Transaction Status on a
  specific PSP bank during the 12 Apr 2025 outage) plus broadcast
  afterward (OC tightening Check Transaction Status semantics for
  everyone). [src: The Authority RCA reported by Mobile ID World Apr 2025]

### 6.3 Tier-1 partner weighting

The Authority does not publicly disclose any explicit "Tier-1 weighting" in
governance, but partner concentration is observable: top three TPAPs
process ~91% of the network by volume (PhonePe 46–48%, GPay 36–37%, Paytm
~7% as of mid-2025). [src: The Authority data via Business Standard, Feb–Jul
2025]

`[PROPOSED DEFAULT]` — Tier-1 partner asks should be flagged for
elevated review, not because their ask is privileged but because:
(a) the systemic impact of mis-handling is larger; (b) their volume
data is also the Authority's leading indicator of ecosystem behaviour; and
(c) a Tier-1-only-fix risks accusations of preferential treatment,
so resolution paths typically broaden to the rest of the ecosystem.

### 6.4 Conflicting cluster asks

When two clusters of partners want incompatible things (e.g. one
cluster of large banks wants a lower API rate-limit for fairness,
another cluster of TPAPs wants higher rate-limit for performance):

- **Resolver disposition**: Recommend Architecture + Steering
  Committee. The Authority's observable pattern is to err toward systemic
  stability over individual-partner performance (e.g. the
  post-12-Apr-2025 RCA action was to *restrict* Check Transaction
  Status API behaviour, accepting that high-throughput partners
  would be inconvenienced).

---

## 7. Historical patterns by major the network rollout

Precedent backbone for the resolver. For each named rollout: what
partners commonly asked, what the Authority conceded, what it held firm on,
timeline shape.

### 7.1 the Lite wallet (Sep 2022)

- **Launched**: Global Fintech Fest Sep 2022, alongside RuPay-CC-on-network
  and BBP Cross-Border. [src: Paytm Payments Bank PR Sep 2022]
- **Initial cohort**: 8 banks at launch; ~11 by Aug 2023. [src: Fisdom]
- **Initial limits**: ₹200/txn, ₹2,000 wallet → ₹500/txn (Aug 2023)
  → ₹1,000/txn + ₹5,000 wallet (RBI MPC Oct 2023; RBI circular Dec
  2023). [src: Deccan Herald Dec 2023]
- **Partners asked for** (inferred): larger initial cohort, higher
  limits, credit-account support.
- **the Authority conceded**: limit raises over time; e-mandate auto-replenishment
  (Jun 2024 RBI MPC).
- **the Authority held firm on**: on-device wallet design; no-AFA-for-small-value
  as a deliberate offline-framework feature.

### 7.2 the network AutoPay (Jul 2020 onwards)

- **Role assignment**: PSP/Issuer bank is mandate-bearing; TPAPs
  initiate collect-request flows; the Authority publishes mnemonic + brand
  guidelines centrally. [src: The Authority Auto Pay Brand Guidelines]
- **AFA threshold**: ₹5,000 → ₹15,000 (OC 151, 23 Jun 2022; OC 151A).
- **Partners asked**: higher AFA-free threshold, mid-mandate amount
  changes.
- **the Authority conceded**: AFA threshold raise; e-mandate-style semantics
  for select categories.
- **the Authority held firm on**: AFA above threshold; customer revocation
  right at any time.

### 7.3 RuPay Credit Card on the network (Jun 2022 onwards)

- **Pilot cohort**: SBI Card, PNB Cards, Union Bank, Axis. [src:
  VARIndia Jul 2022; Inc42 Oct 2022]
- **Interchange/MDR**: 2% MDR (1.5% issuer, 0.5% acquirer + RuPay
  split) set by the Authority + banks, not bilateral. Zero MDR retained
  ≤₹2,000 for small offline merchants (Section 269SU floor). Above
  ₹2,000, acquirer reimburses 8 bps each to PSP and app provider.
  [src: The Authority circular 4 Oct 2022 via Inc42, Business Standard]
- **Partners asked**: higher issuer share; zero MDR above ₹2,000.
- **the Authority conceded**: tiered zero-MDR for ≤₹2,000 small merchants;
  RuPay-CC parity with other CC schemes above ₹2,000.
- **the Authority held firm on**: only authorised CC issuers; AFA above ₹15k
  recurring.

### 7.4 the Lite wallet X (Sep 2023)

- **Launched at**: GFF Sep 2023; NFC offline tap-and-pay.
- **Limits**: ₹500 per txn (later ₹1,000), ₹2,000 wallet, 1 debit +
  10 credit offline txns/day. [src: Razorpay; NewsBytes Oct 2023]
- **Constraint**: Android-only at launch (Apple NFC restrictions).
- **Partners asked**: iOS support, larger offline limits.
- **the Authority conceded**: limits raised under Offline Framework as RBI
  permitted.
- **the Authority held firm on**: iOS support is Apple-platform-bound, not
  the Authority's gift.

### 7.5 the network International / PayNow-the network (2023 onwards)

- **Sequence**: PayNow-the network launched 21 Feb 2023 with 6 Indian banks
  (Axis, DBS India, ICICI, Indian Bank, IOB, SBI) + 2 SG (DBS SG,
  Liquid Group). Indian-side roster expanded to 19 by Jul 2025. The network
  in UAE via Network International, Jul 2024. The network-PayNet (Malaysia)
  phased.
- **Governance**: NIPL-led, not domestic the network. Domestic precedent
  doesn't auto-apply on cross-border features.
- **Limits**: SGD200/txn, SGD500/day at launch; SGD1,000 for all
  DBS customers from 31 Mar 2023. [src: MAS Feb 2023]
- **Partners asked**: more corridors, higher limits, lower FX margin,
  faster NRE/NRO onboarding.
- **the Authority conceded**: 6→19 banks in 2.5 years; non-resident the network
  access from select corridors.
- **the Authority held firm on**: FEMA/RBI corridor restrictions; bank-account-
  grade KYC retained.

### 7.6 the network 123Pay (Mar 2022)

- **Cohort**: Four channels (IVR, missed call, OEM/app, sound-based);
  partnerships with Gupshup + Airtel Payments Bank, JioPay + Jio
  Payments Bank.
- **Subset support**: Not every bank supports every channel — explicit
  cohort scope cut accepted at launch.
- **Partners asked**: channel-specific delays.
- **the Authority conceded**: optional-channel cohort design.
- **the Authority held firm on**: The network-PIN auth in feature-phone flow; bank-
  account linkage retained.

### 7.7 the network IPO ASBA ₹5 lakh (2022)

- **Sequence**: The Authority OC 127 (9 Dec 2021) raised the network per-txn for
  ASBA-IPO ₹2L → ₹5L. The Authority confirmed 30 Mar 2022 that >80% of
  SCSBs/Sponsor Banks/the network Apps had completed system changes. SEBI
  mandated the network for all individual IPO bids up to ₹5L from 1 May 2022
  via SEBI/HO/DDHS/P/CIR/2022/0028.
- **Partners asked**: more time for SCSB systems; Sponsor Bank role
  clarification.
- **the Authority conceded**: readiness-gated rollout (SEBI activation held
  until >80% threshold); Sponsor Bank role definition refined.
- **the Authority held firm on**: ₹5L ceiling; Sponsor Bank remained SEBI-
  registered.

### 7.8 v2.0 of the network (Aug 2018)

- **Launched with**: SBI, HDFC, Axis, ICICI, IDBI, RBL, YES, Kotak,
  IndusInd, Federal, HSBC.
- **the Authority conceded**: launched without full auto-recurring-payment
  support (partner ask deferred to AutoPay 2020).
- **the Authority held firm on**: one-time mandate-block functionality as
  product backbone.

---

## 8. The Authority artefact lifecycle

### 8.1 Document flow

A new the network feature typically flows through:

1. **RBI policy signal** (often via Monetary Policy Statement) — common
   for limit/AFA/interchange changes; not always present.
2. **the Authority Operating Circular** (authority/network/OC No. NN/YYYY-YY) — binding
   document for partners. Sequential within FY (OC 127/21-22,
   OC 151/22-23, OC 163/22-23, OC 222/25-26).
3. **BRD / TSD / Manifest** — technical spec; referenced in circulars.
4. **Certification Test Cases** — partners certify before going live.
5. **Addendum** — "OC NNA" naming when clarification or modification
   needed after initial circular (OC 151A, 163A, 222A).
6. **Steering Committee minutes** — abridged on the Authority's website; used to
   ratify systemic decisions and impose fines.

### 8.2 Clarification stage re-opening vs handled inline

Inferred pattern:
- **Inline (PM)**: single-partner ask, trivial wording, no behaviour
  change, no spec change.
- **Re-open clarification stage**: ≥3 partners with overlapping asks,
  OR any single ask that changes observable behaviour or cert TCs.

`<TO CONFIRM>` Exact gate is internal to the Authority PMs.

### 8.3 Versioning conventions

- **Circulars**: The Authority/the network/OC No. NN/YYYY-YY; addenda use "A" suffix.
- **the network-PG**: revised in full when accumulated amendments warrant; else
  amended by circular.
- **Manifest versions**: forward-only (inferred from §1.3).

### 8.4 Sunset / supersession

- Older circulars superseded explicitly when a new circular says so;
  otherwise remain in force.
- Addenda *modify* the parent; do not supersede.
- `<TO CONFIRM>` Explicit sunset clauses observed examples are rare;
  the Authority generally supersedes via fresh circular rather than expiring
  by date.

---

## 9. Confirmation needed

Consolidated list of items the resolver must treat as `<TO CONFIRM
WITH the Authority POLICY TEAM>` or `[PROPOSED DEFAULT]` before relying on them.

**§1**: Grounded in published sources. Suggestion: The Authority Policy team
confirm there are no *additional* internal non-negotiables not captured
(e.g. crypto-adjacent flows, stablecoin on-ramps via network).

**§2 (tolerances)** — all `[PROPOSED DEFAULT]`:
- §2.1 deadline bands: ±14d PM / 15–60d Compliance / >60d SVP.
- §2.4 BRD-addendum publication SLA: 7–14 days.
- §2.5 vendor-blocker default extension: 30 days.

**§3 (response patterns)**:
- §3.2 N≤14 / 15–60 / >60 day bands — `[PROPOSED DEFAULT]`.
- §3.7 defer-to-next-release for mid-rollout scope additions —
  `<TO CONFIRM>` whether PM bench has any standing Tier-1 exception.

**§4 (escalation matrix)**: Entire RACI is `<TO CONFIRM>`. Internal
role names not public. Materiality threshold for SVP escalation
(§4.5) — `[PROPOSED DEFAULT]`.

**§5 (round-window timing)**:
- 21-day round-1, 14-day round-2, silent-acceptance default — all
  `[PROPOSED DEFAULT]`. The "consolidate, don't 1:1 counter-back"
  disposition is inferred; `<TO CONFIRM>` whether it matches internal
  practice.

**§6 (cross-partner)**:
- Cluster thresholds (3/5/10 partners) — `[PROPOSED DEFAULT]`.
- Tier-1 formal weighting — `<TO CONFIRM>`.

**§8 (artefact lifecycle)**:
- Gate between "inline clarification" and "re-open BRD clarification
  stage" — `<TO CONFIRM>`.
- Explicit sunset clauses for circulars — `<TO CONFIRM>`.

---

## Appendix A — Sources consulted

### The Authority publications & circulars
1. The network Procedural Guidelines (2016 baseline, amended via circular).
2. The Authority Circular 04/2016 — Compliance with the Authority Circulars and PG of the network.
3. The network OC No. 32 — Multi-Bank Approach (15 Sep 2017).
4. The Authority — Interoperability features for all the reference app the network Apps (16 Mar 2018).
5. The Authority's Auto Pay Brand Guidelines (Authority website).
6. The Authority OC 151 — the network AutoPay AFA limit enhancement and compliance
   (23 Jun 2022); OC 151A enhancement (2023).
7. The Authority OC 127/2021-22 (9 Dec 2021) — the network ASBA-IPO ₹5L (referenced
   via SEBI circular 8 Mar 2022).
8. The Authority RuPay-CC-on-network operationalising circular (4 Oct 2022).
9. The Authority OC 163/2022-23 (22 Mar 2023); OC 163A (23 Oct 2023).
10. The Authority/2025-26/the network/222 (12 Sep 2025) and 222A (29 Oct 2025) —
    Settlement cycle segregation.
11. The network Steering Committee abridged minutes (Jan 2024 – Jun 2025
    series, Authority website).
12. The Authority Roles & Responsibilities doc (mirrored on IDFC FIRST, Kotak,
    IndusInd, GooglePay India).
13. The Authority Product Overview (Authority website).

### RBI / SEBI / Government publications
14. RBI Master Direction on Digital Payment Security Controls
    (18 Feb 2021).
15. RBI Offline Framework (Jan 2022, amended Dec 2023 raising the Lite wallet
    to ₹1,000/₹5,000).
16. RBI MPC Jun 2024 — the Lite wallet e-mandate auto-replenishment.
17. SEBI Circular SEBI/HO/DDHS/P/CIR/2022/0028 (8 Mar 2022) — the network
    ASBA ₹5L mandate.
18. SEBI Circular Jun 2025 — SEBI-registered intermediaries
    @validbankpsp the network handle.
19. PIB Press Release PRID 2114335 (24 Mar 2025) — Section 10A PSS
    Act and Section 269SU IT Act on zero-MDR.

### Cross-border / NIPL
20. MAS Press Release — PayNow-the network launch (21 Feb 2023).
21. NIPL/the Authority International — PRNewswire announcement expanding
    the network-PayNow to 19 Indian banks (17 Jul 2025).
22. The network-PayNet Malaysia announcement (Business Standard, 2025).

### Outages and post-incident
23. The Authority RCA reportage (Kapronasia 4 Jul 2025; Mobile ID World
    20 Apr 2025; adityakulkarni.substack.com Apr 2025).
24. The Authority public statements on outages 26 Mar / 1 Apr / 12 Apr 2025
    (Business Standard, IndiaTV News, Deccan Herald).

### Analyst / academic
25. CGAP, "the Authority and the Remaking of Payments in India" (2019).
26. IIMB the Authority report (board governance / committee structure).
27. Forrester, "the network 123Pay" (Mar 2022).
28. Communications of the ACM, "the network 123Pay" (Dec 2024).
29. The Level One Project, "the network 123Pay" (Sep 2025).

### Industry coverage (used for triangulation of limits / timelines)
30. Inc42, Business Standard, The Head and Tale, Outlook Money,
    NewsOnAir, Razorpay, Paytm, M2P Fintech, MicroSave, Easebuzz,
    Cleartax, Fi Money, KhaitanCo, RVKS, Taxguru, BusinessToday.

(~31 distinct primary-or-near-primary sources after deduplication
of mirror sites.)

---

*End of the Authority_POLICY.md research draft. Hand off to the Authority Policy
Team for items marked `<TO CONFIRM WITH AUTHORITY POLICY TEAM>` and
`[PROPOSED DEFAULT]` before this file is loaded as context by the
production resolver.*