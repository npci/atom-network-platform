## What this changes, and why

<!-- The diff says what. This says why now, and what you considered instead. -->

Closes #

## Type

- [ ] `feat` — new capability
- [ ] `fix` — behaviour was wrong before
- [ ] `docs` / `refactor` / `perf` / `test` / `build` / `ci` / `chore`

## Size

<!-- CONTRIBUTING.md: <200 lines preferred, 200-500 fine with a clear
     description, >500 split if you can. -->

## Checks

- [ ] Every commit is signed off (`git commit -s`) — **PRs without a DCO sign-off on every commit cannot be merged**
- [ ] Commit messages follow Conventional Commits
- [ ] `bash scripts/ci/hygiene-check.sh` passes
- [ ] `docker compose run --rm backend pytest` passes
- [ ] `cd frontend && npm run lint && npm run build` passes
- [ ] New behaviour has tests; a bug fix has a regression test

## Verification

<!-- "The tests should pass" is not "I ran the tests". Say what you actually ran.
     For UI changes, say that you brought the stack up and clicked through. -->

## Conventions this change touches

<!-- Tick only what applies. Each of these fails SILENTLY when it is got wrong. -->

- [ ] **A2A wire code** — edited `packages/a2a-core/`, not a vendored copy, and ran `scripts/ci/sync-a2a-core.sh`. If signing changed, the partner platform ships the same change in the same release
- [ ] **Prompts** — the file ends with exactly one trailing newline (prompt-cache keys are exact prefix bytes)
- [ ] **New setting** — declared as a field on `Settings`; `getattr(settings, ...)` on an undeclared name silently drops the env var
- [ ] **Migration** — idempotent and inspector-gated, with `.with_variant(..., "sqlite")` for Postgres-native types
- [ ] **New agent** — mapped in `llm_router._AGENT_PURPOSE`; unmapped names default to `REASONING`
- [ ] **Domain vocabulary** — lives in a pack, not in `core/`
