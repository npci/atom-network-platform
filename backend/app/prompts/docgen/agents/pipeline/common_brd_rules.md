
═══════════════════════════════════════════════════════════
BRD RULES — business intent only, NOT implementation
═══════════════════════════════════════════════════════════

A BRD describes WHAT changes for the business. It does NOT describe HOW
the change is implemented at the wire level. Implementation depth belongs
in the Technical Specification Document (TSD), which is authored AFTER
BRD approval and uses the BRD as input.

DO NOT INCLUDE in a BRD (these belong in the TSD):
  · Sample XML / XSD / JSON request or response payloads, or any wire-level
    samples in code blocks.
  · Field-level data types, dLength, lengths, or validation regex.
  · Code-level error mapping with per-code wire classification (the domain's
    RB, XT, etc. by code).
  · Library/SDK version numbers, namespace URIs, routing-category values,
    credType byte-level encoding.
  · Switch routing rules, credential-block format, cryptographic algorithm
    selection (key wrap, AES mode, PBKDF2 iterations).
  · Idempotency-key construction algorithms, dedup TTL implementation,
    retry-with-backoff numeric parameters.
  · Database schema, primary-key types, index definitions, ORM models.
  · State Transition tables that name "Triggering API" — that's TSD content.

DO INCLUDE at the business level:
  · Canonical APIs by NAME and BUSINESS PURPOSE only (state in one clause what
    business instruction each message carries, e.g. "Req<Xxx> carries the
    customer's <business intent>"). NEVER show the wire format.
  · Business entities by NAME, PURPOSE, and KEY
    BUSINESS ATTRIBUTES — NO datatypes, NO lengths.
  · Business lifecycle states (PENDING_APPROVAL → ACTIVE → EXPIRED) and the
    BUSINESS EVENTS that drive transitions ("approver clicks accept",
    "EMI cycle expires"), NOT API calls.
  · Functional Requirements as testable business statements
    ("The system shall reject if monthly cap exceeded").
  · Error CATEGORIES at customer-facing level (auth-failure, business-decline,
    customer-cancellation, technical-failure) with the customer message.
    Not implementation-level wire codes.
  · Risk/Fraud SCENARIOS and mitigation CONCEPTS (not implementation).
  · Business decisions, ownership boundaries, dispute paths.
  · Regulatory mapping (which regulator/authority directive applies, citation [S#]).

API NAMING — REUSE EXISTING APIs FIRST:
  Reference APIs by their canonical name exactly as the domain defines it
  (commonly a Req<Pascal> / Resp<Pascal> pair). Use ONLY names present in the
  supplied domain knowledge or context — never invent one, and never import a
  name from another ecosystem.
  · Default behaviour: extend an existing API. Note it as "<ExistingApi> (new
    sub-type for <purpose>)" — keep the canonical name UNCHANGED.
  · NEW API only when no existing one fits, justified in 1-2 sentences
    inline. Pattern: ReqXxxYyy / RespXxxYyy (PascalCase).
  · NEVER use SCREAMING_SNAKE_CASE (REQ_FOO_BAR is rejected).
  · NEVER prefix with REQ_ / RESP_ (uppercase + underscore).

OWNERSHIP MODEL (layer model — DO NOT VIOLATE):
  · Authority  = switching, routing, protocol validation. It does NOT
                 enforce business policy or transaction limits.
  · Participant  = business logic, limits, UX, consent capture.
  Domain-specific role responsibilities (which roles own auth, fulfilment,
  settlement, etc.) are supplied by the active domain pack below — use ONLY
  those roles; never import another ecosystem's.

CITATIONS — RAG corpus is supplied via the user message:
  · The user message contains a retrieved-corpus evidence block
    with [S1], [S2], ... tags + a "## Source index" footer.
  · Every regulatory obligation, named API, business limit, settlement
    timeline, dispute SLA, or compliance claim that has corpus support MUST
    carry an inline [S#] tag at the END of the sentence.
  · The "References" section at the END of the document MUST reproduce the
    Source index VERBATIM as a numbered list of cited tags. Non-optional.

ANTI-HALLUCINATION ON QUANTITATIVE CLAIMS — three escape hatches:
  Every business limit, latency / throughput target, monetary value, dispute
  SLA, retry count, or go-live date MUST use one of:
    (a) CITED       — "settlement T+1 [S3]" — corpus evidence supports it.
    (b) ASSUMED     — "Assumption: TTL is 24h; final value defined in TSD."
    (c) ILLUSTRATIVE — "for example, INR 4,500 against a INR 5,000 cap"
                      — phrase "for example" / "e.g." / "illustrative" must
                      appear in the same sentence.
  Round numbers without one of the three forms are FORBIDDEN.

NEVER FABRICATE METADATA:
  · Do NOT emit Document ID, Version, Date, Classification, "Prepared by",
    or revision history in the body. The platform wrapper supplies these.
  · Do NOT emit a cover-page title in the body — start at the first section.
═══════════════════════════════════════════════════════════

