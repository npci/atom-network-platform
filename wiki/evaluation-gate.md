# The evaluation gate

> **Verified at:** alembic head `0138_integration_exchange_query`, commit `e116cea`.
> Counts checked against `backend/app/services/evaluation/` — 21 deterministic
> checks, 12 hard-fail catalogue entries.

A stage does not advance because an agent returned text. It advances because the
text passed a gate. This is the layer that makes the pipeline something other
than a chain of hopeful prompts.

## Four kinds of check, in increasing cost

| Kind | Asks | Cost |
|---|---|---|
| **Deterministic** | Is it structurally valid? | free |
| **Grounding** | Is it supported by retrieved sources? | cheap |
| **Critic** | What is wrong with it? | one model call |
| **Judge** | Is it good enough to pass? | one model call |

The ordering is the design. Deterministic checks are pure functions over the
artifact, so a document with unfilled placeholders is rejected **without spending
a model call** on judging its prose. Paying a frontier model to discover that a
template was never filled in is the waste this ordering avoids.

## Deterministic checks

Twenty-one of them, each a plain function returning a list of problems. They
catch the failures that are embarrassing rather than subtle:

- placeholder text that was never replaced
- mandatory sections missing
- requirement numbering that does not follow the expected pattern
- a required table absent
- **internal markers left in output** — reasoning scaffolding that should never
  reach a reader
- an empty payload

That last pair matters more than it looks. A model that emits its own working
notes into a deliverable produces something that *reads* fine to an automated
scorer and is obviously wrong to a human.

## Hard fails

A **hard-fail catalogue** defines every failure code, each with a meaning,
example evidence and remediation. Twelve codes at present.

Two properties worth knowing:

- A contract may only reference codes defined in the catalogue, and **this is
  enforced at import time** — a misconfigured contract fails at startup rather
  than at the moment a document is being evaluated.
- Every code carries remediation text. A gate that says "FAIL" and stops tells
  an operator nothing; the catalogue exists so a failure is actionable.

## Contracts and policy

What is checked, and how strictly, is per artifact type — declared in contracts
rather than hard-coded per stage. Results are persisted, so a stage's evaluation
history is inspectable after the fact rather than only visible in logs.

## The honest limit

The gate scores **outputs against contracts**. It cannot tell you the output was
grounded in the *right* sources, only that it was grounded in the ones retrieved.
A confident, well-structured, correctly-formatted document built on a corpus that
silently failed to ingest passes every check here.

That is not a defect in the gate — it is the boundary of what output scoring can
establish, and it is why [retrieval](retrieval.md) health is worth checking
independently.

## Related

- What produces the artifacts: [workflow phases](workflow-phases.md)
- Where the sources come from: [retrieval](retrieval.md)
- Measuring end output quality: the harness in `../backend/tests/golden/`
