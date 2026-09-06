# The A2A wire

> **Verified at:** alembic head `0138_integration_exchange_query`, commit `e116cea`.
> Vendoring checked against `packages/a2a-core/MANIFEST`; task types counted in
> `packages/a2a-core/a2a_common/protocol.py`.
>
> For **authentication** on this wire, see [security layers](security-layers.md).
> This page is the protocol and the code-sharing mechanism.

## One editable source, several copies

Five modules are **generated into** the service trees rather than imported. The
canonical copies live in `packages/a2a-core/a2a_common/`, and `MANIFEST` is the
single list of where each one lands — read by both the sync script and the
hygiene gate, so the two cannot disagree.

| Module | Vendored into (this repository) | Carries |
|---|---|---|
| `hmac_signer.py` | `backend/app/a2a_common` | The envelope signature |
| `protocol.py` | `backend/app/a2a_common` | The task-type vocabulary |
| `executor_base.py` | `backend/app/a2a_common` | The dispatch skeleton |
| `integration_contract.py` | `backend/app/a2a_common` | The tunnel's request/response shapes |
| `integration_allowlist.py` | `backend/app/a2a_common` | The tunnel's alias allowlist — its SSRF control |

Each lands in exactly one tree here. It used to be more: `hmac_signer.py` was
also vendored into `certagent/cert-agent` and `certagent/bank-agent`, and those
entries outlived the trees themselves — a `MANIFEST` path that does not exist
makes the sync script abort *before* it reaches the later files, so the gate
silently stopped covering `protocol.py` and `executor_base.py` while appearing
to pass. The dead destinations were removed rather than the abort made
tolerant, because a manifest that names a missing tree is a bug either way.

**The partner platform holds a second copy of all five, outside this
repository** — see [atom-partner-platform](https://github.com/npci/atom-partner-platform).
`MANIFEST` covers only the trees above, so the gate cannot detect drift against
the partner's copies; keeping them in step is a release-coordination duty.

Editing a vendored copy is wasted work — the next sync overwrites it, and the
hygiene gate fails on drift meanwhile. Edit `packages/a2a-core/`, then run
`scripts/ci/sync-a2a-core.sh`.

**Why this machinery exists at all:** every service hashes the same wire bytes.
Change what one side feeds the signer — field order, encoding, which headers are
covered — and signatures stop matching *across a trust boundary*. The symptom
appears on the other side of the boundary from the edit, as a generic
authentication failure, which is close to the worst possible debugging
experience. Making the file physically un-editable in place is cheaper than
diagnosing that twice.

## What is deliberately not shared

`client.py` and `mount.py` differ per service, because the two ends are not
peers: one is the authority, the other a participant. They are **not** vendored.

The gate does not ignore them, though — it baselines the size of their
difference, so the gap cannot widen unnoticed. That is the middle position
between "force them identical" (wrong) and "stop looking" (how drift starts).

## The message vocabulary

**37 task types**, each declared with a direction and an expected cardinality —
"once per change and version", "any, per question" — in a single table in the
protocol module rather than implied across handler code.

Having cardinality declared next to the type is what makes duplicate-delivery
handling reviewable. A handler that quietly accepts a second
`change_acknowledgement` for the same version is a bug you can see in the table,
not one you have to infer from code.

The types cover distribution, acknowledgement, queries and clarifications,
progress and readiness, negotiation, and the certification lifecycle. The first 28 are the frozen contract; the rest extend it — five for the
certification lifecycle, and three added with the integration-testing tunnel
(`http_exchange_request`, `http_exchange_response`, `cert_execution_start`).

> **There is a second enum with the same name.** `backend/app/models/phase_c.py`
> declares its own `A2ATaskType` with 25 members — 13 shared with the wire, 12 of
> its own. It is a persistence-layer record, not the wire vocabulary, and the
> column it backs is a plain `varchar` deliberately decoupled from it. Count the
> wire in `a2a_common/protocol.py`; count nothing in the model.
>
> `authority_executor.py` accepts the **union** of the two, so the divergence is
> handled where it matters most. One path is narrower:
> `adapters/channel/a2a.py::deliver()` validates outbound `message.kind` against
> the *model* enum alone, and would refuse the 24 wire-only types with
> "A2A has no task type …". Worth confirming no pack routes certification
> traffic through that adapter.

## Two wires, one endpoint

Both backends mount JSON-RPC at `/a2a-rpc/rpc` and advertise an agent card.
Discovery is served **unprefixed** at the root, because a remote agent fetches
the card without knowing anything about local path layout — publishing it under a
prefix once broke discovery outright.

Task-store state is persisted, so a task is not lost when a process restarts
mid-conversation.

## The one that bites

**Outbound `Message.task_id` means "continue this existing task."** Setting it on
a first send asks the remote side to continue a task it has never heard of, and
it answers exactly that: *task does not exist*. Leave it empty for new sends.

This is the single most-repeated mistake against this wire, and it reads like a
server bug when you hit it.

## Related

- Authentication and rejection codes: [security layers](security-layers.md)
- Where the boundary sits: [architecture](architecture.md)
- What flows across it: [workflow phases](workflow-phases.md)
