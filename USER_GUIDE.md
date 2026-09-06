# User Guide — AtOM

For the people who use the platform: product owners, product managers, tech
leads, reviewers and administrators. It follows one change from an idea to a
certified partner rollout, and says what each screen asks of you at each step.

This is not a developer or operator document. For installation see
[`DEPLOYMENT_GUIDE.md`](DEPLOYMENT_GUIDE.md); for how the system works inside,
see [`wiki/`](wiki/).

- [The shape of the work](#the-shape-of-the-work)
- [Roles](#roles)
- [Phase A — from an idea to approved documents](#phase-a--from-an-idea-to-approved-documents)
- [Approvals](#approvals)
- [Phase B — generating code](#phase-b--generating-code)
- [Phase C — partners and certification](#phase-c--partners-and-certification)
- [Everyday screens](#everyday-screens)
- [Administration](#administration)
- [Reading what the platform tells you](#reading-what-the-platform-tells-you)

---

## The shape of the work

A change moves through three phases.

```
   Phase A                    Phase B                 Phase C
   Idea → approved      →     Design → code    →      Distribution
   documents                                          → certification
                        ↘                          ↗
                          both consume Phase A
```

**Phase A** turns a prompt into a set of approved documents. **Phase B** turns
those documents into code against a real repository. **Phase C** distributes the
result to the organisations that must implement it, and certifies what they
build.

Phase B and Phase C both consume Phase A's output, and they are independent of
each other. A change can go to partners without any code having been generated —
which is the normal case when the implementing work belongs to the partners
rather than to you.

**Every stage is gated.** A stage does not advance because an AI agent produced
text; it advances because that text passed a set of checks — structural,
grounding, critic and judge, cheapest first. You will see a stage refuse to
advance and tell you why.

---

## Roles

Six roles exist. A user is assigned one or more and acts as one at a time; the
role you are currently acting as decides what you can see and do.

| Role | What it is for |
|---|---|
| `product_owner` | Raises changes, answers clarifications, approves the business requirements |
| `product_manager` | Shapes the canvas and requirements alongside the owner |
| `tech_lead` | Reviews and approves the technical specification and schemas |
| `infosec_reviewer` | Approves from a security standpoint |
| `risk_reviewer` | Approves from a risk standpoint |
| `admin` | Manages users, partners, knowledge bases and platform configuration |

Roles are assigned in **Admin → Users**. A user assigned more than one role can
switch between them; approvals are recorded against the role you were acting as.

---

## Phase A — from an idea to approved documents

Start at **New Change** and give the platform a prompt describing what you want
to change. From there the change moves through nine states in a fixed order, and
each has its own screen under the change:

| State | Screen | What happens |
|---|---|---|
| `prompt_enhancement` | Prompt Enhancement | Your prompt is sharpened into something specific enough to research |
| `research` | Research | Deep research: market context, regulatory considerations |
| `canvas` | Canvas | A structured product canvas — the shape of the change |
| `clarification` | Clarify | The platform asks *you* questions, generated from the canvas |
| `brd` | BRD | Business requirements, with multi-stakeholder approval |
| `tech_spec` | Tech Spec | The technical specification |
| `xsd` | XSD | Schema changes |
| `product_kit` | Product Kit | The distributable bundle: documents, FAQ, test cases, circular |
| `completed` | — | Phase A closed |

### Why clarification sits where it does

It comes **after** the canvas and **before** the requirements, and that ordering
is deliberate. The questions are generated from the canvas and answered before
requirements are written, so the BRD is authored *with* your answers rather than
patched afterwards. Answering thoroughly here is the highest-leverage thing you
do in the whole flow — a vague answer produces a vague requirement that someone
disputes three stages later.

### Working with a generated document

Each document screen shows the generated content with the sources it drew on.
You can:

- **Edit it directly.** Your edits are kept; regenerating a section does not
  silently discard them.
- **Regenerate a section** rather than the whole document.
- **See what it cited.** Agents are grounded in an ingested corpus and in real
  source code, and the screen shows which passages a section used. If a claim
  looks wrong, check its sources first — an ungrounded-looking answer is often a
  corpus problem rather than a model problem.
- **Send it for approval** when it is ready.

### If a stage will not advance

It will tell you what failed. The common causes are structural and quick to fix:
placeholder text that was never replaced, a mandatory section missing,
requirement numbering that does not follow the expected pattern, a required
table absent, or an empty payload. These are checked before anything expensive
runs, so the answer arrives quickly.

A document can also be **complete but thin** — fallback content exists so a long
document still assembles when one section could not be produced. Read what you
are approving.

---

## Approvals

**Approvals** lists everything waiting on you, filtered to your active role.

Five artifact types go through approval: the **product canvas**, the **BRD**,
the **technical specification**, the **XSD schemas**, and a **decline spec**
where a change is being formally declined.

Each approval is **approve** or **reject**, with comments. A rejection returns
the artifact for revision rather than ending the change. Approvals are recorded
against you and the role you were acting as, and that record is what later
stages check — the technical specification cannot be frozen for code generation
until its approval exists.

**Team Inbox** shows the same work at team level rather than filtered to you,
which is the view to use when covering for someone.

---

## Phase B — generating code

Open **Phase B** on a change to generate code against a real repository.

The run has its own lifecycle, visible on the screen as it progresses: a
workspace is prepared, retrieval context is assembled from the actual codebase,
edits are planned and applied, then the result is reviewed and repaired. A real
build runs at the end.

Three things worth knowing before you start one:

**It does not deploy.** The default runner mode is fully simulated and says so
in every log line it emits. Other modes compile the target repository and stop,
or run a script an operator supplied. Nothing ships without a person.

**A human opens the merge request.** Generated code is built, reviewed against
an adversarial pass, and gated — but the final step is yours.

**An interrupted run resumes.** If a run dies, it restarts from its last
recorded phase rather than from the beginning. You do not need to babysit it.

The screen shows the plan, the files touched, the build output and the review
findings. Where the platform is unsure it says so rather than presenting a guess
as a result.

---

## Phase C — partners and certification

Open **Phase C** on a change to distribute it, or work from the certification
screens for a cross-change view.

### Distribution and tracking

Each partner has its own assignment with its own lifecycle, so one slow partner
does not block another:

```
assigned → communicated → acknowledged → in_progress → ready
  → received → accepted → applied → tested
  → ready_for_certification → certifying → certified
```

Partners also report coarse progress independently — design complete, coding
complete, testing complete — which is what the readiness view uses.

### The screens

| Screen | What it is for |
|---|---|
| **CR Dashboard** | Every change in certification, at a glance |
| **Partner Entries** | The partners registered against a change |
| **Agent Messaging** | The conversation with a partner, message by message |
| **Cert Status** | Where each certification run stands |

### Three mechanisms that are easy to miss

**Negotiation is a round-based loop, not one exchange.** Partners can counter
rollout terms. Rounds close on a timer, and if a partner does not respond the
platform applies **silent acceptance** — silence becomes an explicit decision
rather than an absence. You will see that recorded, not inferred.

**Blockers are first-class.** A partner can be "in progress but blocked
critically" rather than merely late, with its own severity and status.

**Delivery assumes failure.** A failed message is retried by a scheduled job,
not by the request that first attempted it. A delivery that has not arrived yet
is usually in that queue rather than lost.

### Certification

Certification drives switch-level test cases through simulators, over the same
protocol, and ends in a sign-off. Results arrive per case. Where a case fails,
triage classifies it — and a failure that breaks a documented schema constraint
is treated as a real defect rather than something waivable.

---

## Everyday screens

| Screen | What it shows |
|---|---|
| **Dashboard** | Your changes and where each one is |
| **Approvals** | What is waiting on you |
| **Team Inbox** | The same, at team level |
| **Product Kit** | Published kits across changes |
| **Usage** | Token and cost consumption per change and per run |
| **Escalations** | Changes that need attention beyond the normal flow |

**Usage** is worth checking periodically rather than only when something looks
wrong. Every run is bounded by a token budget, and a run rejected for exceeding
one is reported as rejected — the number tells you whether budgets are set
sensibly for the kind of change you are doing.

---

## Administration

Under **Admin**, for users with the `admin` role:

| Screen | What it manages |
|---|---|
| **Users** | Accounts and role assignments |
| **Partners** | Registered partner organisations and their credentials |
| **Product Knowledge** | The document corpus agents are grounded in |
| **Code Knowledge** | Ingested source repositories |
| **Code Indexing** | The state of that ingestion |
| **API Registry** | API messages and fields, and which change introduced them |
| **Agentic Codegen** | Code-generation runs and their settings |
| **Configuration** | Provider, model, endpoint and secret settings |

**Product Knowledge and Code Knowledge decide answer quality.** Retrieval
quality is not visible in any output gate: a confident, well-structured document
built on a corpus that silently failed to ingest passes every check the platform
makes. If answers look ungrounded, check the corpus is populated *before*
suspecting the model.

**Configuration is the runtime edit surface.** Provider, model and endpoint
settings, and every secret, are edited here rather than in files — see
[`CONFIGURATION.md`](CONFIGURATION.md).

---

## Reading what the platform tells you

A few conventions worth internalising, because they are consistent everywhere:

**Refusal is a result.** When the platform cannot do something safely it refuses
and says why, rather than producing a plausible-looking substitute. A refused
sandbox run, a rejected stage, a held-back test case — these are working as
intended.

**Simulated is labelled.** Anything simulated says so in its output. If a Phase
B run does not say it is simulated, it is not.

**Skipped is not passed.** In certification, a constraint that cannot be checked
is *skipped with a reason*, never quietly passed. An assertion that always
passes is indistinguishable from one that was never made.

**Coverage is reported before results.** Where the platform generated test cases
or assertions from a registry, it leads with what is **not** covered — the APIs
it could not reach, and the fields carrying no assertable constraint. Read that
first; it tells you what the pass actually means.

---

## Related documents

| Document | Covers |
|---|---|
| [`README.md`](README.md) | Overview, architecture, quick start |
| [`FAQ.md`](FAQ.md) | Common questions |
| [`DEPLOYMENT_GUIDE.md`](DEPLOYMENT_GUIDE.md) | Installing and running the platform |
| [`CONFIGURATION.md`](CONFIGURATION.md) | Settings, and which layer wins |
| [`wiki/workflow-phases.md`](wiki/workflow-phases.md) | The three phases in mechanical detail |
