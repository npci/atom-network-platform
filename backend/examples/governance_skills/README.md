# Example governance skills (EA + InfoSec)

Two **generic, application-neutral** review rulebooks that make the pre-build
governance stages (EA Review → InfoSec Review) runnable out of the box:

| File | Skill type | Slot name |
|---|---|---|
| `ea_review_skill.md` | `ea` | `example-ea-architecture-review` |
| `infosec_review_skill.md` | `infosec` | `example-infosec-code-review` |

They are *examples*: nothing in them is specific to any business domain,
framework, or organisation. Replace them with your own rulebooks for real
governance; keep them for demos and local runs.

## Loading them

Either upload via the UI — **Admin → Governance Skills** (one upload per skill
type) — or seed both in one step:

```bash
# native run (from backend/, venv active or via .venv/bin/python)
python scripts/seed_governance_skills.py

# docker
docker compose run --rm backend python scripts/seed_governance_skills.py
```

The seeder is idempotent: a re-run that finds the same content already active
inserts nothing. It refuses nothing else — an edited file seeds as a new
append-only version, exactly like a UI upload.

The stages also need `GOVERNANCE_REVIEWS_ENABLED=true` in the backend
environment; with the flag off the Phase B page shows no governance cards and
Build is not gated.

## Format contract (for writing your own)

A skill is a markdown file, optionally starting with YAML frontmatter
(`name:` — becomes the slot; `description:`). The parser
(`app/agents/governance_skills.py`) derives enforceable units in one of three
modes, strongest first:

1. `## RULE <id>: <title>` headings — one directive per rule (used here).
   Ids must be unique; near-miss headings (wrong depth, missing separator)
   are rejected loudly rather than silently folded into the previous rule.
2. Plain `## Section` headings — every level-2 section becomes one unit.
3. No headings — the whole document is a single unit.

Each rule should be one independently judgeable directive: the reviewer
returns an explicit PASS/FAIL per rule against the code change, so write rules
that can be decided from a diff, and say explicitly when a rule passes by
default (change doesn't touch the concern).
