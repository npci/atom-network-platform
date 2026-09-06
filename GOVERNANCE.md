# AtOM — Governance

This document describes how the AtOM project is governed — how
decisions are made, who the maintainers are, and how the community can
participate.

---

## Model

**Single-vendor open source.** The National Payments Corporation of India (the Authority)
owns the copyright, employs the maintainers, and makes the final call on scope,
architecture and releases.

This is stated plainly because the alternative — implying a neutral,
multi-stakeholder foundation that does not exist — wastes contributors' time.
If that changes, this document changes with it.

---

## Principles

- **Open development** — all technical discussion happens in public issues and
  pull requests.
- **Consensus-seeking** — we prefer rough consensus over voting; objections are
  taken seriously.
- **Meritocracy** — influence is earned through sustained, quality contributions.
- **Transparency** — decisions and their rationale are documented publicly.

---

## Roles

### Users

Anyone who uses AtOM. Users are the most important people in the
project — their feedback drives priorities.

### Contributors

Anyone who has submitted a pull request that was merged, filed a bug report that
led to a fix, or improved documentation. Contributors are listed in
[AUTHORS.md](AUTHORS.md).

### Maintainers

Maintainers have write access to the repository and are responsible for:

- Reviewing and merging pull requests
- Triaging issues
- Cutting releases
- Enforcing the [Code of Conduct](CODE_OF_CONDUCT.md)

Current maintainers:

| Name | GitHub | Areas |
|---|---|---|
| atom-admin | [@atom-admin](https://github.com/atom-admin) | Overall; domain-pack contract, A2A |

`atom-admin` is a shared maintainer identity operated by the team
behind the project, not an individual. Mail reaches the same inbox as the
contacts in `SECURITY.md`.

Areas are a routing hint for reviewers, not ownership — any maintainer may
review anything. Add a row per maintainer; a single-maintainer project is
honest but should say so rather than imply a team.

---

## Decision Making

### Day-to-day decisions

Maintainers make day-to-day decisions (bug fixes, minor features, dependency
updates) by consensus in pull request reviews. A PR can be merged when at least
one maintainer approves and no maintainer objects within 48 hours.

### Significant changes

Significant changes (new features, breaking changes, architecture decisions, new
dependencies) require:

- An issue or discussion opened for community input
- At least 2 maintainer approvals
- A 5-business-day comment period before merging

### Breaking changes

Breaking changes additionally require:

- A deprecation notice in the prior release (where feasible)
- An entry in the release notes under a `Breaking Changes` heading
- A major or minor version bump per [Semantic Versioning](https://semver.org/)

### Specific categories

- **Bug fixes, tests, docs** — any maintainer may merge after one review.
- **New features, dependencies, schema changes** — maintainer consensus; a
  single objection blocks until resolved in the issue.
- **Changes to the domain-pack contract** (`app/core/domain/contract.py`) —
  these are the public API. They need an issue with rationale, and they are
  breaking changes until 1.0 says otherwise.
- **Licence, trademark, governance** — the copyright holder decides.

Disagreement is resolved in the issue thread, in public. If it cannot be, the
maintainers decide by majority; a tie goes to the status quo.

---

## Becoming a Maintainer

Contributors who have made sustained, high-quality contributions over at least
3 months may be nominated as maintainers by an existing maintainer. Nomination
requires approval from a majority of current maintainers.

Maintainers who are inactive for 6 months may be moved to emeritus status.

---

## New Domain Packs

A pack is accepted when it has a **committed maintainer** — someone who knows
that ecosystem and will answer issues about it. A pack without one is a
liability: it rots, and its rot is read as the platform's.

Packs live in-tree while the contract is unstable. Once it settles, they may
move to their own repositories.

---

## Releases

Semantic versioning, with the **domain-pack contract** as the public API.

- `0.x` — the contract will break between minors. It is not stable and does not
  pretend to be.
- `1.0` — not before a third pack has been written by someone outside the
  founding team. Until then there is no evidence the contract generalises.

Release cadence is monthly **at most**, and less if there is nothing worth
shipping.

---

## Security Issues

Security vulnerabilities are handled privately. See [SECURITY.md](SECURITY.md)
for the responsible disclosure process.

---

## Code of Conduct

All participants are expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
Maintainers are responsible for enforcement.

---

## Support

Best-effort. No SLA. Security reports are prioritised over everything else
([SECURITY.md](SECURITY.md)).

Maintaining a repository this size realistically costs 16–25 hours a week in
triage, review, dependency updates and releases before any feature work. That
is stated here so expectations — including the maintainers' own — are set
honestly.

---

## Amendments

This governance document may be amended by a pull request with approval from a
majority of current maintainers and a 5-business-day comment period.

---

*Last updated: 2026-08-22*