<!-- Captured from the real canvas agent, 2026-08-10, claude-sonnet-5.
     Verbatim except for ONE redaction: a Market View line named three real
     wallet providers. Removed so a synthetic fixture in a public repo does
     not read as this organisation's opinion about named companies. The
     redaction touches no scored term. -->
## 1. Feature

This feature lets a customer set a minimum balance rule on their the network-linked wallet (e.g., "reload with INR 500 whenever balance drops below INR 100"). Once the customer sets up this rule one time — authenticating with their PIN only during this setup — every future top-up happens automatically in the background by debiting their bank account, with no PIN entry needed at the time of reload. The customer gets a notification after each auto-reload and can pause, modify, or cancel the rule anytime from their PSP app.

## 2. Need

- **Why should we do this?**
  - Removes reload friction that currently causes wallet balance depletion and transaction failures at point-of-sale/QR scan, directly hurting the network-linked PPI wallet usage (transit, small-ticket merchant QR, gig-worker payouts).
  - Extends the existing the network Autopay e-mandate rail (used today for subscriptions/SIPs/EMIs) to a new "balance-triggered" use case instead of only calendar-triggered debits.
- **Differentiation** — Incremental from a rails perspective (reuses the Authority Mandate Management System, e-mandate registration, and "as-and-when-presented" mandate category already live under the network Autopay), but exponential from a UX perspective: it is the first the network mandate type triggered by an account-state event (balance threshold) rather than a fixed date/frequency.
- **Delta in user experience** — Customer no longer opens the PSP app, checks balance, and enters PIN to reload; the wallet self-heals below threshold. First-time setup still requires one PIN authentication (regulatory AFA requirement is not waived, only the reload PIN is waived).
- **What will it cannibalize?**
  - Manual "Add Money to Wallet" flows via network Collect/Intent.
  - Auto-reload features currently built by PPI issuers using their own NACH/debit-card mandates outside the network rails.
- **What if we don't build this?** — PPI issuers continue to route auto-reload through NACH or card-based auto-debit (higher cost, T+1/T+2 settlement, lower success rates ~80-85% vs the network's real-time execution), and the network loses share of the wallet-funding transaction pool to card networks.

## 3. Market View

- **Ecosystem anticipated (informal) response**
  - PPI-issuing banks/wallets (major third-party wallet issuers) likely to push for fast adoption — direct cost and success-rate benefit over NACH.
  - Remitter banks may flag concern over unattended low-value debits stacking up without per-transaction PIN, especially fraud/complaint volume.
  - TPAPs will want SDK/API support to expose "set auto-reload threshold" as a first-class UI element, not buried in settings.
- **Ecosystem efforts (costs to make this work)**
  - PSP apps must build UI for threshold configuration, edit, pause, cancel.
  - Wallet issuer (Beneficiary Bank) must integrate a balance-monitoring trigger service that raises a debit request to the Authority Mandate Execution API when threshold is breached.
  - Remitter/Issuer Bank must support "as-and-when-presented" mandate execution without additional OTP/PIN step, consistent with existing the network Autopay mandate execution flow.
- **Anticipated regulatory view** — RBI has already permitted e-mandates with AFA-at-registration-only for recurring payments (2019 circular) and PPI interoperability (2021); this is a natural extension. RBI will likely require: (a) mandatory pre/post-transaction notification, (b) mandate cap per execution, (c) easy revocation, mirroring existing e-mandate safeguards.

## 4. Scalability

- **Market anchors to make it big (demand and supply)**
  - Demand: high-frequency, low-ticket wallet users (metro/transit commuters, QR merchants, quick-commerce) where manual reload is the #1 friction point.
  - Supply: PPI issuers already possess Autopay mandate infrastructure; only need to add "balance-event" as a trigger type alongside "fixed date."
- **Impact opportunity**
  - Assumption: ~350-400 million active the network-linked wallet users nationally could be addressable; even 10-15% opt-in reduces manual reload transactions by a similar order, freeing that volume for merchant-side the network growth instead.
  - Reduces average wallet-empty transaction decline rate (a top BD reason code today) which currently suppresses P2M conversion at the point of sale.
  - Revenue potential is indirect: does not carry its own MDR but increases downstream P2M transaction volume that does.

## 5. Validation

- **Creating and operating MVP**
  - MVP scope: enable auto-reload only for KYC-verified full-KYC wallets, single fixed reload amount and threshold per mandate (no tiered/dynamic reload logic), capped at INR 2,000 per auto-reload execution to stay under low-value recurring exemption limits.
  - Operate MVP with 2-3 PPI issuer partners and 2-3 PSP apps in a closed pilot before ecosystem-wide circular release.
- **Data it will generate to create insights**
  - Threshold-breach-to-successful-reload latency, mandate execution success/decline rate by bank, reload frequency distribution, opt-in vs manual-reload churn comparison, complaint volume tied to auto-reload debits.

## 6. Product Operating

- **3 Success KPIs**
  - Auto-reload mandate execution success rate: baseline (manual reload success ~90%) → target ≥97%.
  - Wallet-balance-related transaction decline rate at merchant QR: baseline (Assumption: ~6-8% of P2M declines today) → target reduction to <2%.
  - Mandate opt-in rate among eligible wallet users within 6 months: target ≥12%.
- **Grievance redressal (Trust)** — Auto-reload debits must be traceable back to a specific mandate ID in the Authority Mandate Management System; disputes routed through existing the network Autopay grievance flow (Issuer Bank → the Authority → Beneficiary Bank) with mandatory reversal SLA of T+1 for wrongful/duplicate reloads.
- **Day 0 automation**
  - Automated threshold-breach detection and mandate execution trigger (no manual intervention).
  - Automated post-debit notification to customer via PSP app push + SMS.
  - Automated mandate-cap enforcement (reject execution requests exceeding registered mandate amount) — classified as BD, responsible entity: The Authority (Mandate Management System validation layer).
- **Impact on SGF** — Adds a new class of low-value, high-frequency recurring debits into the settlement pool; Assumption: negligible incremental SGF exposure per transaction given the INR 2,000 execution cap, but aggregate daily volume must be modeled before scale-up.
- **Impact on FRM** — Requires FRM rule update to distinguish "customer-initiated auto-reload" mandate executions from anomalous silent debits; without this, FRM may falsely flag legitimate auto-reloads as account-takeover pattern (rapid PIN-less debits).
- **Impact on existing txns and infra** — Reuses existing the network Autopay mandate registration (ReqMandateCreate) and execution (ReqMandateExecute) APIs; requires new mandate sub-type field (e.g., mandateTriggerType = "BALANCE_THRESHOLD") to distinguish from calendar-based ("FIXED_DATE") mandates — backward compatible, no disruption to existing recurring mandate flows.

## 7. Product Comms (external + internal)

- **Product demo** — Live demo of: setup flow (PIN once) → simulated balance depletion → automatic reload → push notification, shown across one bank app and one TPAP app to prove interoperability.
- **Product video** — 60-second consumer-facing video: "Never see 'insufficient balance' at checkout again" — targeted at transit/QR-heavy user segments.
- **Explanation video by PM** — Internal 10-minute walkthrough covering mandate trigger-type distinction, FRM rule changes needed, and grievance flow differences from standard Autopay.
- **FAQs + trained LLM** — FAQ topics: how to set/change threshold, why no PIN is needed, how to cancel, what happens on failed reload, how refunds/reversals work. LLM trained on these FAQs plus existing the network Autopay mandate FAQ corpus for consistency.
- **Circular** — New the authority product circular defining "Balance-Threshold Triggered Mandate" as a sub-category under the network Autopay "as-and-when-presented" mandates, including cap, notification, and FRM tagging requirements.
- **Product doc** — Full spec covering: mandate registration fields, execution trigger logic, mandateTriggerType field addition, notification requirements, UI/UX guidelines for threshold-setting screens, and test cases for boundary conditions (exact threshold hit, multiple rapid breaches, mandate expiry mid-cycle).

## 8. Pricing

- **3-year view of pricing & revenue** — No direct MDR on the auto-reload debit itself (wallet load is currently zero-MDR under RBI/the Authority rules); revenue impact is indirect via increased downstream P2M MDR volume from better-funded wallets. Assumption: Year 1 pilot (no direct monetization), Year 2 ecosystem-wide rollout with measurable P2M volume uplift, Year 3 potential premium API tier for issuers wanting advanced auto-reload analytics.
- **Market ability to pay the price (total pie)** — PPI issuers currently spend on NACH/card mandate processing fees for auto-reload; total addressable cost-saving pool is the NACH/card mandate fee they currently pay, which shifts to the network at near-zero incremental cost.
- **Market view to pay the price** — PPI issuers are highly incentivized to adopt at zero/near-zero the Authority fee given existing infra reuse; unlikely to accept a new per-mandate fee given wallet-load is a zero-MDR category by regulation.

## 9. Potential Risks

- **Fraud risk** — Compromised device/app session could silently register a high-frequency auto-reload mandate; mitigated by keeping AFA mandatory at registration and capping per-execution amount at INR 2,000.
- **Infosec risk** — Balance-monitoring service at the Beneficiary Bank (wallet issuer) continuously polling/listening to account balance state introduces a new API surface; must be secured with mutual TLS and rate-limiting to prevent balance-inference attacks.
- **Legal risk** — Consumer disputes over "I never approved this specific reload" despite having approved the mandate; must have a documented mandate consent trail (T&C acceptance at registration) admissible in grievance proceedings.
- **Data privacy risk** — Balance-threshold data and reload patterns reveal spending behavior; must be processed only by Beneficiary Bank/PSP directly involved, consistent with DPDP Act data-minimization principle — no third-party analytics sharing without explicit consent.
- **2nd order negative effect** — Widespread auto-reload could reduce customers' active balance awareness, increasing "one more automated debit" fatigue that lowers scrutiny of all the network mandates, potentially increasing susceptibility to fraudulent mandate registration scams (a known the network Autopay abuse vector).

## 10. Compliance

- **Existing guideline change** — Amend the network Autopay mandate circular to add "BALANCE_THRESHOLD" as a permitted trigger type alongside existing "FIXED_DATE" and "AS_AND_WHEN_PRESENTED" categories.
- **New guideline addition** — New circular section mandating: (a) per-execution cap for balance-triggered mandates, (b) mandatory post-debit notification (pre-debit notification exemption applies only below the existing low-value recurring threshold), (c) FRM tagging standard for this mandate type.
- **Must have compliances in the authority product circular for ecosystem**
  - Mandatory AFA at mandate registration (no exception).
  - Mandatory post-execution notification to customer within defined SLA.
  - Mandatory mandate pause/cancel self-service available in PSP app.
  - Mandatory mandateTriggerType field in mandate registration and execution API payloads for FRM and audit traceability.
  - Mandatory grievance routing through existing the network Autopay dispute resolution flow with defined reversal SLA.