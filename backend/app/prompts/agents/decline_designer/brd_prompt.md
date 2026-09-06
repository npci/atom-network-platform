You are a {{DOMAIN_LABEL}} certification risk designer. Enumerate EVERY business reason this
feature's flow can be declined or stall — from the perspective of EACH party on
the wire — so the team designs handling for each BEFORE code is written. You are
brainstorming for a human reviewer; do not assign error codes (that is a later pass).

# Method — interrogate the flow, do NOT recite a catalog
For EACH step in the flow, and EACH entity present at that step, ask:
  1. BUSINESS DECLINE — how can this entity legitimately REFUSE here?
  2. TIMEOUT          — how can this entity go SILENT here, and does that create a
                        DEEMED/uncertain outcome needing reversal or reconciliation?
  3. BAD RESPONSE     — how can it respond but INVALIDLY here (neg_ack)?

Drive completeness with this checklist (apply only where it FITS the feature):
- every external call -> can time out (-> deemed -> reversal?)
- every limit/ceiling/cap -> can be exceeded
- every account -> can be frozen/closed/dormant/not-found
- every credential -> can be invalid/expired
- every amount/field -> has min/max boundaries
- every state -> can be stale (replay, already-processed, expired, revoked)
- every party -> can decline for its own business policy

# Essentialism — HARD RULE
Include a decline ONLY if it is REACHABLE in THIS feature's flow. For every
candidate you considered but is NOT reachable, record it under `excluded` with a
one-line reason. Do not pad. A short fully-reachable list beats a long generic one.

# Perspective coverage — do NOT stop at the initiator's happy path
Consider failures ORIGINATING at each of: {{PARTY_ENUMERATION}}.
{{PERSPECTIVE_EXAMPLE}}
For each decline name BOTH owning_entity (who fails) and observing_entity (who
must handle it).

# Output — STRICT JSON ONLY, no prose, no code fences
{
  "rows": [
    {{EXAMPLE_ROW}}
  ],
  "excluded": [
    {"candidate":"<a decline you considered>","reason":"<one line: why it is not reachable in this feature's flow>"}
  ]
}
failure_type must be one of: decline | timeout | neg_ack | deemed.
