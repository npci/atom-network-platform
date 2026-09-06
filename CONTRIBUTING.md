# Contributing to AtOM

Thank you for your interest in contributing! This document explains how to get
involved, what we expect from contributors, and how the review process works.

> **This project is pre-1.0 and the domain-pack contract is still moving —
> expect breaking changes.** Contributions are accepted under a **DCO** (see
> [Developer Certificate of Origin](#developer-certificate-of-origin-dco)) —
> no paperwork, just `git commit -s`.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Ways to Contribute](#ways-to-contribute)
- [Getting Started](#getting-started)
- [Development Workflow](#development-workflow)
- [Commit Guidelines](#commit-guidelines)
- [Developer Certificate of Origin (DCO)](#developer-certificate-of-origin-dco)
- [Pull Request Process](#pull-request-process)
- [Coding Standards](#coding-standards)
- [Testing](#testing)
- [Architecture Rules](#architecture-rules-worth-knowing-early)
- [Adding a Domain Pack](#adding-a-domain-pack)
- [License](#license)

---

## Code of Conduct

By participating in this project you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md). Please read it before contributing.

---

## Ways to Contribute

- **Bug reports** — open an issue using the bug report template.
- **Feature requests** — open an issue using the feature request template.
- **Documentation** — fix typos, improve clarity, add examples.
- **Code** — fix bugs, implement features, improve performance.
- **Reviews** — review open pull requests and provide constructive feedback.

---

## Getting Started

### Prerequisites

- Python 3.12+
- Node.js 18+
- Docker & Docker Compose (for full-stack local dev)
- PostgreSQL 16+ with pgvector extension
- Redis 7+

### Local Setup

```bash
# 1. Fork and clone
git clone https://github.com/<your-fork>/atom-network-platform.git
cd atom-network-platform

# 2. Bootstrap (generates config, starts the stack, seeds admins)
bash scripts/bootstrap.sh
```


---

## Development Workflow

1. **Create a branch** from `main`:

   ```bash
   git checkout -b feat/my-feature   # or fix/my-bug
   ```

2. **Make your changes** — keep commits focused and atomic.

3. **Run tests and linting** before pushing (see [Testing](#testing)).

4. **Push and open a PR** against `main`.

5. **Address review feedback** — maintainers may request changes.

6. **Merge** — a maintainer will merge once approved.

---

## Commit Guidelines

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short summary>

[optional body]

[optional footer — DCO sign-off goes here]
```

**Types:** `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`, `ci`

**Examples:**

```
feat(llm-proxy): add circuit-breaker retry with exponential backoff
fix(guardrails): correct PII regex for Aadhaar format
docs(readme): update quick-start for Docker Compose
```

Keep the summary line under 72 characters. Use the body to explain *why*, not
*what* (the diff shows what).

---

## Developer Certificate of Origin (DCO)

This project uses the **Developer Certificate of Origin (DCO)** instead of a
Contributor License Agreement (CLA). By signing off your commits you certify
that you have the right to submit the contribution under the MIT license.

**Sign off every commit** with `-s`:

```bash
git commit -s -m "feat(agents): add tool-call retry logic"
```

This appends:

```
Signed-off-by: Your Name <your.email@example.com>
```

The full DCO text is in the [DCO.md](DCO.md) file at the root of this
repository.

> **Note:** PRs without a DCO sign-off on every commit will not be merged. If
> you forgot, you can amend: `git commit --amend -s` (for the last commit) or
> `git rebase --signoff HEAD~N` (for the last N commits).

A DCO was chosen over a CLA deliberately: a CLA would let the copyright holder
relicense contributed code later, but it needs legal administration and is a
documented deterrent to casual contributors. If a relicensing need ever arises,
it will be negotiated with contributors rather than pre-empted here.

---

## Pull Request Process

1. Fill in the PR template completely.
2. Link the related issue (e.g., `Closes #123`).
3. Ensure all CI checks pass (lint, tests, hygiene gate).
4. Request a review from the relevant code owners (see `CODEOWNERS` file in the
   repository root).
5. Do not merge your own PR — at least one maintainer approval is required.

### Before you open a pull request

```bash
bash scripts/ci/hygiene-check.sh               # secrets, internal hosts, trademarks
docker compose run --rm backend pytest         # platform tests
cd frontend && npm run lint && npm run build   # frontend gate
```

CI runs the same checks. The hygiene gate is not advisory — it fails the build.

### PR Size Guidelines

| Size   | Lines changed | Guidance                                    |
|--------|---------------|---------------------------------------------|
| Small  | < 200         | Preferred — fast to review                  |
| Medium | 200–500       | Fine — include a clear description          |
| Large  | > 500         | Split if possible; add a detailed description |

---

## Coding Standards

### Python

- **Formatter:** `ruff format` (Black-compatible)
- **Linter:** `ruff check`
- **Type hints:** required for all public functions and class methods
- **Docstrings:** Google style for public APIs

```bash
ruff format .
ruff check . --fix
```

### TypeScript / JavaScript

- **Formatter:** Prettier (config in `frontend/.prettierrc`)
- **Linter:** ESLint (config in `frontend/.eslintrc`)

```bash
cd frontend && npm run lint && npm run format
```

### General

- No hardcoded secrets, credentials, or internal hostnames — use env vars.
- No `verify=False` on TLS connections.
- No wildcard CORS (`allow_origins=["*"]`) in production code.
- Keep `.env` out of commits — it is in `.gitignore`.

### What we look for in reviews

- **Verify behaviour, not just types.** "The tests should pass" is not "I ran
  the tests". For UI changes, run the stack and click through.
- **Surgical diffs.** A bug fix should not reformat the file. If you spot
  unrelated dead code, mention it; do not remove it in the same commit.
- **Explain *why* in the commit body.** The subject says what changed; the body
  says why now, and what you considered instead.
- **No new abstractions until the third instance.** Two similar blocks is not
  duplication worth a framework.
- **Do not add domain knowledge to `core/`.** Anything that knows about a
  specific ecosystem belongs in a domain pack — there is a test that enforces
  this and it will fail you.

---

## Testing

### Python

```bash
# Run all tests
docker compose run --rm backend pytest

# Run with coverage
docker compose run --rm backend pytest --cov=. --cov-report=term-missing
```

New features must include tests. Bug fixes should include a regression test.

### Frontend

```bash
cd frontend && npm test
```

### CI

All PRs run the full CI pipeline:

- Python lint (`ruff`) + tests (`pytest`)
- Node build + lint
- Secret scan (`gitleaks`)
- Hygiene gate (internal hosts, trademarks, confidential files)

---

## Architecture Rules Worth Knowing Early

- `app/core/**` must not import `app/packs/**`. Core resolves a pack through
  `core/domain/registry`. A test pins this.
- The A2A primitives are **mirrored** between this platform and the partner
  platform, and `hmac_signer.py` must stay byte-identical — both sides hash the
  same wire bytes. Fix a bug in one, fix it in the other.

  The partner platform now lives in a
  [separate repository](https://github.com/npci/atom-partner-platform), and each
  repository's CI validates only its own copies. Nothing checks the two against
  each other, so a signing change must be landed on both sides as a coordinated
  release. Skip that and both test suites still pass — the first symptom is a
  rejected signature on a live A2A call. Within this repository, edit
  `packages/a2a-core/` and run the sync; never edit a vendored copy.
- Prompt text is cached by the LLM provider. Changing a shared prompt block
  invalidates that cache for every request, so byte-identical refactors are
  tested as byte-identical.
- Migrations are idempotent and inspector-gated. Copy the shape of the most
  recent one.

---

## Adding a Domain Pack

The interesting contribution. Implement
`app/core/domain/contract.DomainPack` and register it in
`core/domain/registry`. Declare a capability your domain lacks by **omitting**
the method — do not stub it. `certification_of(pack)` returning `None` means
"this ecosystem has no certification body", which is a true statement about
some domains and must stay expressible.

The interface is `backend/app/core/domain/contract.py`; `backend/app/packs/network/`
  is a complete worked example.

---

## License

By contributing to AtOM, you agree that your contributions will be
licensed under the [MIT License](LICENSE).

You retain copyright of your contributions. The DCO sign-off certifies that you
have the right to submit the work under this license.