# The A2A security layers

> **Verified at:** alembic head `0138_integration_exchange_query`, commit `e116cea`.
> Error codes and enforcement points checked against
> `backend/app/a2a_common/` — the codes below were confirmed present in the
> middleware rather than transcribed`.
>

> checked; that is a finding, not an assumption.

Eight layers guard the boundary between the platform and a partner. They are
**independent by design**: each can be reasoned about, debugged, and disabled in
development on its own. When a call is rejected, exactly one layer rejected it,
and the error code says which.

| # | Layer | What it establishes |
|---|---|---|
| 1 | TLS | The channel is private |
| 2 | JWT | The caller holds a currently-valid session |
| 3 | HMAC envelope | The body was not altered in transit |
| 4 | mTLS | The caller presents a pinned client certificate |
| 5 | CIDR allow-list | The call came from an expected network |
| 6 | Rate limit | One caller cannot exhaust the service |
| 7 | Audit trail | What happened is recoverable afterwards |
| 8 | Key lifecycle | Credentials age out and can be revoked |

Layers 2 and 3 answer genuinely different questions, and conflating them is the
most common misreading. A valid JWT proves *who is calling*. The HMAC envelope
proves *this exact body is what they sent*. A stolen token still fails the
envelope check; a tampered body still fails it even with a perfectly valid token.

## Two tiers of transport

Not every partner gets the same ingress:

| Tier | Auth | Typically used by |
|---|---|---|
| **JWT** (default) | Bearer JWT — short-lived access token plus a refresh token | Most partners |
| **mTLS** | Bearer JWT **and** a pinned client certificate | Banks |

The tier is a property of the partner record, so it is provisioned per partner
rather than being a global switch. A partner marked as mTLS that has no
fingerprint provisioned is rejected — it does not silently fall back to the
weaker tier. That is deliberate: a downgrade that "works" is the failure you
never notice.

## Why the envelope code is generated

`hmac_signer.py` is the reason `packages/a2a-core/` exists.

Every service hashes **the same wire bytes**. If one side changes what it feeds
the signer — field order, encoding, which headers are covered — signatures stop
matching across a trust boundary, and the symptom is a generic authentication
failure on the *other* side of the boundary from the edit.

So the signer, the protocol module and the executor base have exactly one
editable source and are vendored into four service trees. Editing a copy is
wasted work: the sync overwrites it, and the hygiene gate fails on drift
meanwhile.

`client.py` and `mount.py` legitimately differ per service — one side is the
authority, the other a peer. They are not vendored, but the gate baselines their
diff so the gap cannot widen unnoticed.

## Reading a rejection

Rejections carry a machine-readable code. Knowing which layer owns which code
turns a 401 into a one-line diagnosis:

| Code | Means |
|---|---|
| `missing_bearer_token` | No `Authorization` header at all |
| `invalid_token` | Signature or token type wrong |
| `session_unknown` | Token is well-formed but has no session row |
| `session_revoked` | Session was explicitly revoked |
| `session_expired` | Session row has passed its expiry |
| `partner_unknown` | No such partner |
| `partner_inactive` | Partner exists but is not active |
| `mtls_required` | Partner is mTLS-tier but sent no certificate fingerprint |
| `mtls_not_provisioned` | Partner is mTLS-tier and no fingerprint was ever set |

The last two are the pair worth internalising. `mtls_required` means the caller
did not present what it should have; `mtls_not_provisioned` means **the platform
side was never configured** — an onboarding gap, not a caller error. They are
easy to confuse and lead to opposite fixes.

## Development versus production

The production nginx configuration is TLS-only and adds mTLS and rate limiting.
The development configuration deliberately has **none of them**, and this is not
an oversight:

- A fresh clone must run without operator-supplied certificates. The production
  configuration requires certificate files and crash-loops without them, which is
  the single most common first-run failure.
- **Do not add hardening to the development configuration.** It stays minimal on
  purpose.

The consequence to hold onto: layers 1, 4 and 6 are **absent in development**.
Testing that something is rejected locally proves only that layers 2, 3, 5, 7 or
8 rejected it. It says nothing about the transport layers, which exist only where
the production configuration is deployed.

## Outbound URLs: the SSRF guard

The layers above protect traffic coming *in*. One class of risk runs the other
way: a partner's `endpoint_url` is supplied through the admin API and then
fetched by the backend, from inside the network perimeter, with the response
returned to the caller. An unrestricted value therefore reads internal services
on the attacker's behalf.

`app/core/ssrf_guard.py` resolves the hostname and classifies the resulting IP
addresses — loopback, link-local, private, reserved — rather than pattern-matching
the URL text. That distinction matters: the check it replaced compared literal
`http://` prefixes, so `https://169.254.169.254` (the cloud metadata service,
which issues IAM credentials) passed straight through, as did IPv6 (`[::1]`),
integer notation (`2130706433`), and any hostname whose DNS answer pointed
somewhere private.

It is **enforced by default**, and it distinguishes two tiers of target:

- **Never legitimate** — loopback, link-local, multicast, unspecified. No
  partner is reachable at `127.0.0.1` or `169.254.169.254`, so these are refused
  unconditionally and no setting re-enables them.
- **Site-local (RFC-1918)** — refused by default, but genuinely used by internal
  partners, so two escape hatches exist.

| Setting | Values | Meaning |
|---|---|---|
| `SSRF_GUARD_MODE` | `enforce` (default), `observe`, `off` | anything unrecognised enforces, so a typo fails safe |
| `SSRF_ALLOWED_INTERNAL_HOSTS` | comma-separated hosts | exempt specific hosts — the narrow, preferred option |
| `SSRF_ALLOW_PRIVATE_NETWORKS` | `false` (default) | permit RFC-1918 wholesale when naming each partner is impractical |

If an upgrade refuses a legitimate partner, the log line names the host and the
reason; add it to the allowlist. `observe` mode exists for a staged rollout where
that inventory is not yet known.

### DNS rebinding

Validating a hostname and then handing that *hostname* to an HTTP client leaves a
window: the client resolves it again, and an attacker controlling the DNS answer
can return a public address for the check and `127.0.0.1` for the fetch.

`pin_url()` closes it by resolving once, verifying every address in the answer,
and returning a URL that targets the vetted address literally, with the original
hostname carried in the `Host` header so virtual hosting still works. The partner
probe uses this for `http://` targets. It is deliberately *not* applied to
`https://`, where the certificate is issued to the hostname and fetching an IP
literal would fail verification — for TLS the equivalent protection is
certificate validation itself.

## Where the detail lives

- The endpoints these layers guard: [API reference](reference/api.md) — generated.
- Session, partner and audit tables: [data model](reference/data-model.md) — generated.
- How the boundary fits the system: [architecture](architecture.md).
- Slice-by-slice history and the full error table
