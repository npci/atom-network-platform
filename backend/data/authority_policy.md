---
maintainer: <YOUR AUTHORITY'S POLICY TEAM>
profile_version: 0.1-template
confidence_overall: none
---

# Authority Policy — Change-Management Resolver Brief

> **This is an empty template, not policy.** It ships as the platform default so
> a fresh deployment boots with a well-formed, domain-neutral document instead
> of some other ecosystem's rulebook. Every section below is a prompt for your
> authority's own content.
>
> The feasibility resolver reads this document as LLM context when deciding
> whether a partner's request can be accommodated, and cites back the section
> labels it relied on (`§1`, `§2.3`, …). **Until you fill it in, the resolver is
> reasoning from an empty brief** and will say so rather than invent a policy.
>
> Two ways to supply real content:
>
> 1. **Admin UI** — paste it in. The DB row is the runtime source of truth from
>    first boot onward; this file is only the seed and the reset-to-seed source.
> 2. **`AUTHORITY_POLICY_PATH`** — point it at your own file and restart. The
>    bind mount in `docker-compose.yml` maps this path into the container, so
>    editing this file on the host and re-seeding also works.
>
> If your domain pack ships an example policy at `<pack dir>/data/
> authority_policy.md`, the seeder falls back to it when this file is absent.
> The bundled network pack ships one; treat it as a worked example of the shape,
> not as policy you can adopt.

---

## Quick reference

A TL;DR for downstream LLMs reading this file as context. Three to eight bullets
stating the constraints that decide most cases, each with a source reference.

- *<Where does your authority sit relative to a regulator, and which of its
  requirements are therefore non-negotiable?>*
- *<Which role or licence assignments cannot be traded away?>*
- *<Which limits are hard ceilings and which are movable?>*
- *<How does your authority typically stage a rollout — pilot cohort, big bang,
  phased by participant tier?>*

---

## 1. Non-negotiable principles

Things the authority will never concede, whatever the partner's justification.
State the constraint, then what makes it non-negotiable (statute, regulator
floor, interoperability requirement, safety).

### 1.1 Regulator-floor controls

*<Requirements imposed on your authority from above, which it cannot waive.>*

### 1.2 Licence-bound role assignments

*<Roles that only certain authorised entity classes may hold.>*

### 1.3 Schema and interoperability minimums

*<The contract guarantees every participant relies on.>*

---

## 2. Standard tolerances per category

For each category of ask, the default latitude: what is normally granted, what
needs escalation, and what is refused. Keep the categories aligned with the
change types your domain pack declares.

### 2.1 Production deadline

### 2.2 Scope (flows / channels / segments)

### 2.3 Limits and thresholds

### 2.4 Technical spec / API contract

### 2.5 Upstream dependency timelines

### 2.6 Certification role assignments

---

## 3. Standard response patterns by query type

The recurring partner asks and the authority's usual answer to each. Phrase
these as patterns the resolver can match against, not as one-off decisions.

### 3.1 "We need missing spec X"

### 3.2 "Push the deadline by N days"

### 3.3 "Reduce scope to exclude flow F"

### 3.4 "Modify a cap or threshold"

### 3.5 "Change our certification role"

### 3.6 "Vendor readiness is blocking our timeline"

---

## 4. Escalation matrix

Who inside the authority can approve what, in ascending order. The resolver uses
this to decide whether it may answer at all or must route the request onward.

### 4.1 Product owner alone

### 4.2 Product owner + Compliance

### 4.3 Product owner + Architecture

### 4.4 Product owner + Legal

### 4.5 Executive

---

## 5. Round-window timing policy

How long each negotiation round stays open, what happens on silence, and when
the authority counters versus accepts.

---

## 6. Cross-partner considerations

When one partner's ask has to be weighed against the rest of the network:
fan-out thresholds, broadcast versus 1:1, how conflicting asks are reconciled.

---

## 7. Historical patterns

Past rollouts and what they established as precedent. The most useful section in
practice — the resolver reasons far better from "last time we did X, this is
what happened" than from abstract rules. One subsection per significant rollout.

---

## 8. Artefact lifecycle

How your authority's specifications, circulars and guidelines are versioned,
superseded and sunset, and at which stage a clarification re-opens a document
rather than being handled inline.

---

## 9. Confirmation needed

Anything in this document that is inferred, proposed, or not yet ratified by the
policy team. Tag proposed defaults `[PROPOSED DEFAULT]` and open questions
`<TO CONFIRM>` so a reader — human or model — can tell ratified policy from a
placeholder. An unmarked guess is worse than an acknowledged gap.
