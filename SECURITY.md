# Security Policy

## Reporting a Vulnerability

**Do not open a public issue for a security problem.**

Report privately to **`atom.support@npci.org.in`**, with `[SECURITY]`
at the start of the subject line, or use GitHub's **Report a vulnerability**
button (Security → Advisories) once private reporting is enabled on the
repository.

That address is the project's shared open-source inbox and is monitored by the
maintainers; the subject-line tag is what routes it for triage ahead of general
correspondence.

Please encrypt sensitive reports using our PGP key if available (link TBD).

---

## What to Include

A good vulnerability report includes:

- **Description** — what the vulnerability is and its potential impact.
- **Steps to reproduce** — a minimal, reliable reproduction path.
- **Affected component** — which service, file, or endpoint is affected.
- **Suggested fix** — optional, but appreciated.
- **Your contact details** — so we can keep you updated and credit you.

---

## Response Timeline

| Stage | Target |
|---|---|
| Acknowledgement | Within **48 hours** of receipt |
| Initial assessment | Within **5 business days** |
| Fix or mitigation | Within **30 days** for critical; **90 days** for others |
| Public disclosure | Coordinated with reporter after fix is released |

We follow a **coordinated disclosure** model. We will not take legal action
against researchers who report vulnerabilities in good faith and follow this
policy.

---

## Scope

### In Scope

- The AtOM backend (`backend/app/`)
- The frontend (`frontend/`)
- Authentication and authorization logic
- LLM proxy and egress handling
- Sandbox isolation
- Any component in this repository

### Out of Scope

- **The partner platform.** It now lives in its own repository — report against
  <https://github.com/npci/atom-partner-platform> instead. Findings in the A2A
  wire code as it exists *in this repository* remain in scope here.
- Vulnerabilities in third-party dependencies (report those upstream)
- Social engineering attacks
- Physical security
- Denial-of-service attacks that require significant resources

---

## Supported Versions

Pre-1.0. Only `main` receives fixes. There is no long-term-support branch and
no backporting.

---

## Threat Model — Read Before Deploying

This platform **generates code, reviews it, drives git, and invokes builds**.
That is a larger blast radius than a typical web application, and the honest
posture matters more than a reassuring one.

### Prompt injection is mitigated, not solved

Specifications, uploaded documents and retrieved corpus chunks are **untrusted
input that reaches a model with tools**. The platform wraps untrusted content
and carries anti-injection instructions, and generated code is gated behind a
build and a human-opened merge request.

None of that is a proof. A sufficiently clever document may still influence
generated output. **Treat every generated artifact as a proposal requiring human
review, never as an authority**, and do not ingest documents from sources you
would not accept a pull request from.

### What is and is not automated

- Code is generated, reviewed and **built**. It is **not deployed** — see the
  README's *Capabilities and limits*.
- Opening a merge request is human-gated.
- The first dispatch of a change to partners should be human-gated.

### Deployment expectations

- **Never expose the backends directly.** They sit behind nginx, which
  terminates TLS and enforces mTLS and rate limits. Binding backend ports on a
  public interface bypasses all of it.
- **Set every secret explicitly.** `SECRET_KEY`, `SESSION_JWT_SECRET` and the
  database passwords have no safe defaults. Rotate the seeded admin passwords
  immediately.
- **Keep `CAPTCHA_ENABLED=true`** anywhere reachable by others; it is a real
  brute-force control.
- **Redis backs login lockout, CAPTCHA answers and the JWT denylist.** Losing
  Redis degrades those controls — run it with persistence and monitoring.
- **Give the platform the narrowest git credentials that work.** A token that
  can push to `main` is a token an injected prompt can try to use.

### Known gaps

We would rather tell you than have you find out:

- An internal review recorded findings across authorisation, rate limiting and
  transport that are tracked but not all remediated. Ask before deploying
  anything internet-facing.
- Dependencies include a GPL-licensed transitive package and several
  LGPL-licensed ones — see `THIRD-PARTY-NOTICES.md` if that matters to you.
- The certification simulators (`precert/`, `precert-bank-sim/`) carry known
  unremediated secrets — see `docs/genericization/01-exposure-audit.md` rows
  A3–A5. They are host-run and ship with nothing, but do not publish that tree.

---

## Cryptography

What the platform uses, what is quantum-resistant, and the trigger for
revisiting. In short: the
symmetric layer (HMAC-SHA256, bcrypt, HS256) needs no migration, TLS key
exchange offers hybrid post-quantum **X25519MLKEM768**, and certificates remain
RSA pending a CA that issues ML-DSA.

Dependencies are **hash-locked** — `requirements.<arch>.lock` installed with
`--require-hashes` — so the supply-chain pin audit that used to be listed here
as outstanding is done.

---

## Hardening the Repository Itself

`scripts/ci/hygiene-check.sh` and `.gitleaks.toml` gate secrets, internal
hostnames and confidential file types on every change. Run the hygiene check
locally before opening a pull request:

```bash
bash scripts/ci/hygiene-check.sh
```

The gitleaks configuration carries **custom rules for credential formats this
project mints itself**. Off-the-shelf scanning missed two real leaks here; if
you add a new credential format, add a rule in the same commit.

---

## License

This security policy is part of AtOM, licensed under the MIT License.
See [LICENSE](LICENSE) for details.