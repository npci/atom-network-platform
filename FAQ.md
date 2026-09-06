# Frequently Asked Questions

Questions that come up when evaluating, running, or contributing to AtOM. For a
symptom-and-fix table see the Troubleshooting section of
[`README.md`](README.md); this document answers the *why* questions that table
does not.

- [About the project](#about-the-project)
- [Running it](#running-it)
- [The A2A boundary](#the-a2a-boundary)
- [AI behaviour and cost](#ai-behaviour-and-cost)
- [Security and data](#security-and-data)
- [Contributing](#contributing)

---

## About the project

### What is AtOM, in one paragraph?

A platform for the whole life of a specification change. An idea becomes deep
research, a product canvas, requirements, a technical specification and XSD
schemas; those go to the organisations that must implement them; their questions
come back; code is generated against a real repository; and the result is
certified. Each stage is an AI agent, and an evaluation gate sits between stages
so a stage advances because its output passed checks, not because a model
returned text.

### Is this only for payments?

It was built for one — a central body publishes a change and many banks
implement it — and some generated prose is still payments-specific. Domain
vocabulary is moving behind a pluggable **domain pack** (`DOMAIN_PACK`, default
`network`). The interface is `backend/app/core/domain/contract.py` and
`backend/app/packs/network/` is a complete worked example. A test enforces that
`app/core/**` never imports `app/packs/**`, so the separation cannot rot
quietly.

### Does it deploy the code it generates?

No, and this is deliberate. `PHASE_B_RUNNER_MODE` defaults to `demo`, which is
fully simulated and says so in every log line it emits. `build` compiles the
target repository and stops. `ssh` and `local` run an operator-supplied script.
A human opens the merge request in every mode.



### How does this relate to the Partner Platform?

[`atom-partner-platform`](https://github.com/npci/atom-partner-platform)
is the receiving side — the reference implementation partner organisations fork
to consume changes over A2A and plug in their own agents. It is a separate
repository and a separate stack, and reaches this platform as an authenticated
A2A client. Nothing about it needs to be deployed here.

---

## Running it

### Do I need both `.env` files?

Yes, and this catches people out. The root `.env` is what Docker Compose
interpolates into `docker-compose.yml` — it holds `POSTGRES_PASSWORD`,
`REDIS_PASSWORD` and `CERTSIM_INTERNAL_TOKEN`, none of which have defaults.
`backend/.env` is a different file, handed to the backend, celery and
celery_beat containers as their environment. Copy both examples.

### Why does Compose refuse to start over a missing network?

Three services attach to `certagent_cert-net`, an external network owned by a
certification stack that lives elsewhere. Compose expects it to exist already.
`docker network create certagent_cert-net` is enough for a local stack — it can
stay empty.

### Why do I get a 502 right after rebuilding a backend?

nginx resolves upstream DNS once at startup, so a recreated container gets a new
address and nginx keeps proxying to the old one. `docker compose restart nginx`.
This is the single most common false alarm in this stack.

### I edited a file under `backend/app/` and nothing changed.

`app/` is baked into the image. Live editing needs a `docker-compose.override.yml`
bind mount, which is gitignored and absent by default. `backend/tests/` is baked
too — after editing a test, rebuild or `docker cp` the file in.

### Can I run it without Docker?

The full stack is Docker-only in the supported path. `docker-compose.db.yml`
runs just Postgres and Redis in containers if you want to run the application
natively against them.

### Which services do I actually need?

Eight in the default stack. The `reranker` is behind a Compose profile and off
by default — it costs about 2 GB of image and 1.5 GB of RAM for a feature
disabled by default. Start it with `docker compose --profile reranker up -d` and
set `USE_RERANKER=true`.

---

## The A2A boundary

### A partner call is rejected with a signature error. Where do I start?

Authentication is layered — TLS, Bearer JWT, HMAC envelope, mTLS, CIDR
allow-list, rate limit — and each layer fails differently. Check the
`a2a_messages` audit row for the exchange first; it records which layer refused.
The most common cause is that the two sides' `hmac_signer.py` have diverged.

### Why do the two platforms share files by copying rather than importing?

Each service builds from its own Docker context, and a Dockerfile cannot `COPY`
outside its context, so an installable package at the repository root cannot
reach any of these images. The copies are therefore *generated*:
`packages/a2a-core/` is the one editable source, `scripts/ci/sync-a2a-core.sh`
writes the copies, and CI fails on drift. Editing a copy is wasted work — the
next sync overwrites it.

### My first A2A send returns "task does not exist".

Leave `Message.task_id` empty on a first send. Setting it means *continue this
existing task*, so the remote side is being asked to continue something it has
never heard of, and it answers exactly that. This is the most-repeated mistake
against this wire, and it reads like a server bug.

### Can I change the wire protocol on one side only?

No. Neither repository's CI can see the other's copies, so a one-sided change
leaves both test suites green while live calls are rejected. Wire changes are a
coordinated release across both repositories.

---

## AI behaviour and cost

### Which model does a given agent use?

Model choice is per-agent **purpose**, resolved through
`backend/app/core/llm_router.py` — `REASONING`, `ROUTING` or `UTILITY` — never a
model string pinned inside an agent. An unmapped agent name defaults to
`REASONING`, so a missed mapping costs money rather than quality.

### Can I use a provider other than Anthropic?

Yes. `LLM_PROVIDER` accepts `claude`, `openai`, `ainxt` or `ollama`. Every call
goes through `backend/app/core/llm.py`, which dispatches on that setting; agents
never call a vendor SDK directly.

### What stops a run costing an unbounded amount?

Per-run token budgets with hard caps, per-provider circuit breakers, bulkheads
limiting concurrent LLM calls, explicit timeouts, and a global cap on concurrent
agentic runs. A rejected run is reported as rejected rather than silently
truncated.

### Why are prompts stored as files rather than in the database?

Two reasons. A prompt loaded from a writable store is an injection surface —
whoever can edit the row rewrites the model's instructions for every later run.
And a prompt in git is a reviewable diff with an author. Note the trailing-newline
rule: files end with exactly one and the loader strips exactly one, because
prompt caches key on exact prefix bytes and a stray byte silently costs cache
hits on every request.

---

## Security and data

### Where do I report a vulnerability?

Privately, per [`SECURITY.md`](SECURITY.md). Never in a public issue. The GitHub
issue templates route you there.

### Does the platform handle personal data?

Yes, by design — it processes specifications and partner correspondence that can
contain account references and free text. Two tiers of control: a runtime
redaction filter, and a design-time classification in the wire protocol where
each A2A message type is marked as PII-bearing or not, with a required rationale
for every type and a test that fails CI if one is missing. `carries_pii()` fails
closed: an unrecognised task type is treated as PII-bearing.

### Is prompt injection handled?

Mitigated, not solved. Ingested specifications and uploaded documents are
treated as data rather than instructions, and XML is defused against XXE. It
remains an active risk with untrusted input — see [`SECURITY.md`](SECURITY.md).

### Why does login show a CAPTCHA?

Brute-force protection. Set `CAPTCHA_ENABLED=false` in `backend/.env` for a local
loop, and leave it on anywhere others can reach.

---

## Contributing

### What do I have to do before opening a pull request?

Sign off every commit (`git commit -s` — DCO, not a CLA; PRs without it cannot
be merged), use Conventional Commits, and run the three gates in
[`README.md`](README.md) step 8. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

### Why a DCO instead of a CLA?

A CLA would let the copyright holder relicense contributed code later, but it
needs legal administration and is a documented deterrent to casual contributors.
If a relicensing need arises it will be negotiated with contributors rather than
pre-empted.

### What is the most common way a change breaks something silently?

Six things fail quietly rather than loudly, and the pull-request template lists
them as a checklist: editing a vendored copy of the wire code instead of
`packages/a2a-core/`; changing a prompt's trailing newline; reading a setting
with `getattr` when the field is not declared on `Settings` (which is
`extra="ignore"`, so the env var is silently dropped); a migration that is not
inspector-gated; a new agent with no `llm_router` mapping; and domain vocabulary
added to `core/` instead of a pack.

### The licence is MIT — what does that mean for the marks?

MIT contains no trademark clause at all, so it conveys no trademark rights. The
names, logos and marks are reserved under ordinary trademark law and
[`TRADEMARKS.md`](TRADEMARKS.md) is the only place that position is stated.
