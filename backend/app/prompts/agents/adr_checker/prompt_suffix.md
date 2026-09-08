

## Design Review Cross-Check (STRICT)

Review the CONTEXT provided (research report, canvas, RAG knowledge base
excerpts, and any prior BRDs / Tech Specs / ADRs). If your proposed design
CONTRADICTS any prior Architecture Decision Record (ADR), standing design
decision, or documented constraint:

- Emit a dedicated section titled exactly `## Design Review Concerns` near
  the end of the document, BEFORE the final `## Sources` section (if any).
- For each contradiction, use this shape:

  ### Concern 1: <short title>
  - **Prior decision:** <quote or paraphrase the prior ADR/decision>
  - **New design claim:** <the contradicting statement from your output>
  - **Resolution proposed:** <either "update prior ADR because …" or
    "align new design with prior decision because …">

- If there are NO contradictions, OMIT the section entirely. Do NOT emit an
  empty or "no concerns found" section — absence of the header is the signal.
- Do NOT invent contradictions to fill the section. If the retrieved context
  doesn't contain relevant prior decisions, skip this step.

