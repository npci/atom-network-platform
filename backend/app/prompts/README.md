# `app/prompts/` — externalised prompt text

One prompt per file. Loaded via `app.core.prompts.load_prompt("<area>/<name>.md")`.
Populated by Phase 2 of `docs/PROMPT_EXTERNALIZATION_PLAN.md`; empty but for this
file until then.

## Rules

1. **Files end with exactly one trailing newline.** The loader strips exactly
   one, so the loaded text matches the Python constant it replaced byte for
   byte. A prompt that must genuinely end in a blank line needs two newlines.
   This is not style: prompt-cache keys are exact prefix bytes, so one stray
   byte silently costs cache hits on every request.

2. **Never load a prompt from anywhere writable** — no DB rows, no upload dir,
   no user-supplied path. Whoever can write the prompt owns the model's
   instructions for every later run.

3. **Domain vocabulary comes from the active domain pack**, not from the prompt
   body. Use `app.core.domain.registry.prompt_block(...)`. A prompt file with
   `the Authority` or `the network` hardcoded in it defeats the point of moving it here.

4. **Mechanical moves must not change bytes.** `tests/core/test_prompt_snapshot.py`
   enforces this; a migration that leaves it green is provably
   behaviour-preserving. Intentional rewording re-blesses the snapshot AND runs
   the golden-output harness (`docs/GOLDEN_OUTPUTS.md`).

## Customizing the TSD's architecture principles

`docgen/agents/pipeline/architecture_principles.md` is the engineering /
enterprise-architecture standard generated TSDs are held to (modularity,
concurrency, autoscaling, observability, failure handling, and so on). It
ships with the principles this project was originally built around, but it
is deliberately a plain prompt file like any other — edit it, trim it, or
replace it wholesale with your own organization's standards. No code change
is required: the pipeline loads whatever text is in this file and injects it
into both the TSD planning prompt and the TSD section-writer prompt.

An empty file is a valid choice — the TSD pipeline runs fine with no
architecture guidance beyond what the section blueprints already ask for.

## Watch out: `.dockerignore` says `*.md`

Prompts under this directory ship into the image **only because Docker's `*.md`
pattern matches root-level files, not nested ones.** Changing that line to
`**/*.md` would strip every prompt out of the image, and the failure would
surface at runtime as a missing system prompt rather than at build time.

`tests/core/test_prompts_loader.py::test_prompt_files_ship_in_the_image` exists
to make that a failing test instead of a production incident.
