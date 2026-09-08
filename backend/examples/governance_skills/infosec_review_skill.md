---
name: example-infosec-code-review
description: Generic information-security review rulebook — technology- and domain-neutral secure-coding checks for any code change. Ships as a working example; replace with your organisation's own rulebook.
---

This is an **example** Information Security (InfoSec) rulebook. It is
deliberately generic — no business domain, product, or framework is assumed —
so it can gate any code change out of the box. Each rule is one enforceable
directive; the reviewer must return an explicit PASS or FAIL verdict per rule,
judged **only against the code change under review** (the diff and the files it
touches). A rule about something the change does not touch passes by default.

Trust-boundary definition used throughout: any data that originates outside the
process — HTTP parameters and bodies, message payloads, file contents, CLI
arguments, environment values controlled by callers, model/LLM output, and
records written by other systems — is **untrusted input**.

## RULE IS-01: No secrets in source or diffs

The change must not add credentials, API keys, tokens, private keys,
connection strings with passwords, or seed secrets to source files, config
templates, tests, fixtures, or documentation. Secrets come from a secret store
or environment at runtime; committed defaults must be empty or clearly
non-functional placeholders. FAIL on any committed secret-like value, even in
test code, unless it is unmistakably fake and labelled as such.

## RULE IS-02: Untrusted input is validated before use

Every new code path that consumes untrusted input must validate it — type,
length/size bounds, format or allowed set — before acting on it, at the point
where it crosses the trust boundary. FAIL if new input flows into logic,
storage, or output with no validation, or if size-unbounded input (bodies,
uploads, lists) is accepted with no limit.

## RULE IS-03: No injection sinks — queries, commands, and paths are parameterised

Untrusted input must never be concatenated or interpolated into SQL/NoSQL
queries, shell commands or subprocess strings, LDAP/XPath expressions, or
template/eval constructs. Use parameterised queries, argument vectors
(no shell), and escaping/quoting helpers. FAIL on any new string-built query or
command that includes external data, even if the current caller "trusts" it.

## RULE IS-04: File-system and URL access is confined

If the change accepts a path, filename, or URL from outside, it must confine
the result: resolve paths and enforce containment under an allowlisted root
(rejecting traversal such as `../` and symlink escapes), and restrict outbound
URLs to an allowlist or validated scheme+host — never fetch or execute an
arbitrary caller-supplied location (SSRF/RFI). FAIL if a new path or URL from
untrusted input reaches open/read/write/fetch/execute without containment.

## RULE IS-05: New endpoints and operations enforce authentication and authorisation

Every new externally reachable operation (HTTP endpoint, RPC method, message
handler, WebSocket, admin script exposed via UI) must require authentication
and enforce authorisation appropriate to what it does — including object-level
checks (the caller may access *this* record) and privilege checks for
state-changing or administrative actions. FAIL if a new operation is reachable
anonymously by omission, or checks only that a user is logged in when the
action clearly needs a narrower role or ownership check.

## RULE IS-06: Sensitive data stays out of logs, errors, and responses

New logging and error handling must not emit passwords, tokens, keys, session
identifiers, or bulk personal data. Error responses to callers must be generic;
detailed diagnostics (stack traces, internal paths, raw exception text, SQL)
belong in server-side logs only. FAIL if the change logs or returns sensitive
values or leaks internals to the caller on the error path.

## RULE IS-07: Cryptography uses current standards, correctly

Any cryptographic operation the change introduces must use vetted library
primitives and current algorithms — no MD5/SHA-1 for security purposes, no DES/
RC4/ECB mode, no home-rolled ciphers or token schemes, no disabled TLS/
certificate verification, and no predictable randomness (`random`-style PRNGs)
for security tokens; use the platform's CSPRNG. Keys and IVs must not be
constant. FAIL on any of these; hashing for non-security purposes (cache keys,
checksums against accidental corruption) may pass with a clear non-security use.

## RULE IS-08: Deserialisation and parsing of external data is safe

The change must not deserialise untrusted data with mechanisms that can execute
or instantiate arbitrary types (e.g. pickle-style object serialisation, YAML
unsafe load, XML parsing with external entities enabled, dynamic class lookup
from payload fields). Use safe loaders, schema-validated formats, and entity-
disabled parsers. FAIL if untrusted bytes reach an unsafe loader.

## RULE IS-09: Server-side enforcement — the client is never the control

Security decisions introduced by the change (limits, roles, prices, state
transitions, feature gates) must be enforced server-side. Client-supplied
fields must not select the record owner, role, tenant, or approval state.
Hidden form fields, UI disabling, and client-side validation are usability, not
controls. FAIL if the server trusts a client-asserted privilege or identity
field.

## RULE IS-10: Concurrency and multi-step operations cannot be raced into an unsafe state

Where the change implements check-then-act sequences on shared state (balance
checks, quota checks, unique-name claims, file create-then-write), the check
and the act must be atomic (transactions, unique constraints, locks, compare-
and-swap) or the design must tolerate the race safely. FAIL if two concurrent
callers could both pass the check and produce an unsafe combined outcome.

## RULE IS-11: Untrusted content is treated as data, not instructions

Where the change feeds external content into an interpreter of any kind — an
LLM prompt, a rules engine, a template renderer, generated HTML — the content
must be clearly delimited as data (escaping, sandboxed rendering, explicit
untrusted-content framing) so it cannot smuggle instructions or markup into the
surrounding context (XSS for HTML, prompt injection for LLMs). FAIL if external
text is concatenated into an instruction context with no delimitation or
encoding.

## RULE IS-12: New execution surfaces are constrained

If the change lets the system run something it did not run before — a
subprocess, a script chosen by input, a job whose definition is stored data, a
webhook — the executable set must be constrained (fixed binary or allowlisted
directory), run with least privilege, given bounded resources (timeout, output
cap), and its inputs passed as arguments rather than interpolated into a shell
line. FAIL if the change lets input select or compose an arbitrary executable,
or runs one with no timeout or resource bound.
