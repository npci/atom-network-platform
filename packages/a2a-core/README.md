# a2a-core — canonical A2A wire code

**If you are here to change HMAC signing, the A2A protocol envelope, or the
executor base: edit the file in `a2a_common/` here, then run
`scripts/ci/sync-a2a-core.sh`. Do not edit the copies.**

## What this is

The files in `a2a_common/` are the single editable source for the A2A wire code
that must be **byte-identical** across the services in this repository. The
copies that live in each service tree are generated artifacts carrying a
`DO NOT EDIT` banner.

| Canonical file | Vendored into |
|---|---|
| `hmac_signer.py` | npci backend · cert-agent · bank-agent |
| `protocol.py` | npci backend |
| `executor_base.py` | npci backend |

The authoritative list is [`MANIFEST`](MANIFEST) — both the sync script and the
hygiene gate read it, so it cannot fall out of step with either.

### Scope: this repository only

The partner platform keeps its own copies of these files in its own repository
([atom-partner-platform](https://github.com/npci/atom-partner-platform)) and validates
them with its own CI. This gate cannot see them.

`hmac_signer.py` must still be byte-identical on both sides — the platform and
the partner hash the same wire bytes — but that is now a **release-coordination
responsibility rather than a build-time guarantee**. Land a signing change in
both repositories together. If you do not, both test suites still pass and the
first symptom is a rejected signature on a live A2A call.

## Why copies instead of an installed package

Every Python service builds from its **own** Docker context — `./backend`,
`./cert-agent`, `./bank-agent`. A Dockerfile cannot `COPY` outside its context,
so a package at the repo root cannot reach any of these images without moving
every build context to the repo root and rewriting each Dockerfile.
`cert-agent/app/a2a_common/__init__.py` states the same constraint: *"separate
images and cannot share on-disk code."*

The partner platform makes the point conclusively: it is a different repository
now, so it could not import from here even if the contexts were unified.

Vendoring keeps the copies the build needs while removing what made them
dangerous: within this repository there is exactly one file a human edits, and
drift is a build failure rather than a silent divergence in code that both sides
of a trust boundary rely on.

If the build contexts are ever unified, this directory is already shaped like a
package — adding packaging metadata and switching to a real import is then a
contained change.

## Workflow

```bash
# 1. edit the canonical file
$EDITOR packages/a2a-core/a2a_common/hmac_signer.py

# 2. push it to every consumer
scripts/ci/sync-a2a-core.sh

# 3. confirm (also run by scripts/ci/hygiene-check.sh, which gates commits)
scripts/ci/sync-a2a-core.sh --check
```

Adding a file: put it here, add a `MANIFEST` line naming its destinations, run
the sync. **Only add files that are already byte-identical across their
consumers.** A file that legitimately differs per service — `client.py`,
`mount.py`, the `authority_*` / `partner_*` / `sdk_*` modules — is not shared
code; those stay per-service and are baselined separately by the hygiene gate.

## What this does not cover

`cert-agent` and `bank-agent` share a second lineage of wire code
(`auth_middleware.py`, `handshake.py`, `jwt_tools.py`, `nonce_store.py`,
`outbound_auth.py`, `__init__.py`) with no the Authority counterpart, so it has no
canonical home here. Those are byte-identical within that pair and the hygiene
gate hard-fails on any divergence between them.
