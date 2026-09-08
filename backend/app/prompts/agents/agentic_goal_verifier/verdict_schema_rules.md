

When finished, output ONLY this JSON object (no prose around it):
{"refuted": true|false, "findings": [{"kind": "bug|gap|todo", "location": "path:line or where", "detail": "one line the implementer can act on"}], "evidence": "one-line citation of the decisive ground (REQUIRED — a verdict with no evidence is rejected)", "confidence": "high|medium|low", "blocking": "none|contradiction|unverifiable", "details_md": "short markdown summary"}
- `findings` is the PRIMARY output the implementer acts on: one item per gap, each with a concrete `location` and an actionable `detail`. When the honest fix is to refactor the shipped code (not to patch a test around an untestable unit), say so in `detail`.
- `blocking`: use `none` for an ordinary model-fixable gap (the default). Use `contradiction` only when the objective/plan internally precludes itself, and `unverifiable` only when there is no honest evidence path in THIS environment — those two signal the goal needs a human decision, not a retry.
- `refuted: false` only after a thorough audit with every contract item confirmed.
