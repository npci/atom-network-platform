
## Citation Preservation Rules (STRICT)

The upstream context (research report / canvas / BRD) you are working
with may contain inline citation markers like `[1]`, `[2]`, `[3, 5]`,
`[4][7]`. These markers identify which source documents the upstream
agent grounded its claims in.

When you re-author or restate any cited claim from the upstream context,
you MUST preserve the matching `[N]` marker(s) at the end of the sentence
that carries the claim. Example:

    Upstream:  "The retry limit is 3 attempts per minute [4]."
    Your output: "FR-04: The system shall retry failed transactions up to
                  3 times per minute [4]."

Hard rules:
- Never strip a citation marker without good reason.
- When you synthesise / rephrase / split / merge upstream sentences,
  carry forward EVERY `[N]` that contributed to the new sentence.
- If you author a NEW claim that wasn't in the upstream context, mark
  it with `[NO SOURCE]` so reviewers can flag it for verification.
- Do NOT invent new `[N]` numbers; only use the ones already in the
  upstream context.

This preservation is what lets reviewers trace any factual claim in
your output back to the original {{CORPUS_LABEL}} document via the upstream
agent's `## Sources` section.
