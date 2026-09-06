---
name: example-ea-architecture-review
description: Generic enterprise-architecture review rulebook — technology- and domain-neutral checks for structure, contracts, configuration, and operability. Ships as a working example; replace with your organisation's own rulebook.
---

This is an **example** Enterprise Architecture (EA) rulebook. It is deliberately
generic: nothing in it assumes a business domain, framework, or language, so it
can gate any code change out of the box. Each rule below is one enforceable
directive; the reviewer must return an explicit PASS or FAIL verdict per rule,
judged **only against the code change under review** (the diff and the files it
touches). If a rule concerns something the change does not touch, it passes by
default — the review evaluates the change, not the whole codebase.

Severity guidance: a FAIL on any rule blocks the stage until it is fixed or
explicitly overridden with a recorded reason.

## RULE EA-01: Separation of concerns is preserved

A change must not collapse architectural layers. Transport/controller code must
not contain business decisions; business logic must not construct wire/transport
responses or perform raw persistence; persistence code must not call outward to
services. Judge by what the changed code does, not by file naming alone. FAIL if
the change introduces cross-layer logic that the surrounding codebase keeps
separated.

## RULE EA-02: No hardcoded environment-specific values

Hostnames, URLs, ports, file-system paths, credentials placeholders, timeouts,
and feature toggles introduced by the change must come from configuration
(settings object, environment variable, config file), not literals in code.
Constants that are genuinely invariant (mathematical values, protocol names,
enum members) are fine. FAIL if a deploy-environment value is baked into source.

## RULE EA-03: Public contracts stay backward compatible

If the change alters an existing externally consumed interface — an HTTP/RPC
endpoint, message schema, database schema consumed by others, exported function
signature, or file format — it must do so in a backward-compatible way (additive
fields, new optional parameters, versioned endpoint) OR carry an explicit
migration/versioning note in the change. FAIL on silent breaking changes:
renamed/removed fields, changed types, changed status codes, tightened
validation on an existing consumer path with no compatibility note.

## RULE EA-04: Errors are handled deliberately, not swallowed

New code paths must handle their failure modes: exceptions are either handled
meaningfully, translated to the caller's error contract, or allowed to propagate
to a layer that does. FAIL if the change adds an empty catch/except, catches a
broad exception only to continue silently, or returns success on a failed
operation. A deliberate, commented fail-open (with the reason stated) passes.

## RULE EA-05: New long-running or external calls have timeouts and failure paths

Any call the change adds that crosses a process boundary (HTTP, queue, database,
subprocess, socket) must have a bounded timeout and a defined behaviour when the
peer is slow or down (retry policy, circuit-break, error surface). FAIL if a new
external call can block indefinitely or its failure is unhandled.

## RULE EA-06: Observability accompanies new behaviour

New non-trivial operations (jobs, external calls, state transitions, error
branches) must emit log lines (or metrics/traces where that is the codebase
convention) sufficient to diagnose a failure in production: what ran, key
identifiers, and outcome. FAIL if a new failure path is silent. Excessive
logging of payload bodies is judged under the InfoSec rulebook, not here.

## RULE EA-07: State-changing operations are safe to retry or explicitly guarded

If the change adds an operation that mutates durable state and can be invoked
more than once (API endpoint, message consumer, scheduled job), it must be
idempotent, deduplicated, or guarded (unique keys, version checks, status
gates). FAIL if a retry or double-delivery would corrupt state or double-apply
an effect, and nothing prevents it.

## RULE EA-08: Resources are released on every path

Files, connections, cursors, subprocesses, locks, and temporary directories
opened by new code must be released on success **and** on error (context
managers, try/finally, defer, or the language's equivalent). FAIL if an
exception path can leak a resource the change opened.

## RULE EA-09: Dependencies are introduced deliberately

A new third-party dependency (library, service, container image) must be
necessary — not duplicating something the codebase already uses for the same
purpose — and must be added through the project's dependency manifest, not
vendored ad hoc or imported optimistically. FAIL if the change adds a dependency
that duplicates an existing capability or bypasses the manifest/lock mechanism.

## RULE EA-10: Naming and placement follow the surrounding codebase

New modules, classes, functions, endpoints, and configuration keys must follow
the naming conventions and directory layout evident in the code around them.
FAIL only for divergences that would mislead a maintainer (e.g. a module placed
in a layer it does not belong to, an endpoint named against the existing URL
scheme) — not for taste.

## RULE EA-11: Dead, duplicated, and debug code does not land

The change must not introduce commented-out code blocks, unreachable branches,
copy-pasted logic that an existing shared helper already provides, leftover
debug prints, or disabled tests without a stated reason. FAIL if any of these
are present in the diff.

## RULE EA-12: Non-obvious decisions are recorded

Where the change makes a decision a maintainer could not reconstruct from the
code alone — a chosen limit, an ordering constraint, a deliberate deviation from
convention, a compatibility shim — a short comment or accompanying document must
state the constraint. FAIL if the diff contains such a decision with no recorded
rationale. Self-explanatory code needs no comment; do not fail for absence of
narration.
