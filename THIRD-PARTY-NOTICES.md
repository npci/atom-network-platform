# Third-party notices

This product bundles no third-party source. It depends on packages installed at
build time, whose licences are summarised here.

**Generated**, not hand-maintained:

```bash
docker build -t atom-backend:latest ./backend
syft scan docker:atom-backend:latest --output syft-json=sbom.json
```

Last regenerated **2026-09-03** against a locally built backend image:
**185 Python packages**. Regenerate whenever `requirements.txt` changes, and
publish an SBOM per release.

> **The count depends on the architecture you build on, and right now the two
> locks disagree.** `backend/Dockerfile` selects `requirements.${TARGETARCH}.lock`,
> so an arm64 build and an amd64 build install different closures — 170 pins
> against 145. That is expected in kind (the ML stack resolves differently) but
> not in this degree: the **arm64 lock is stale**. It predates the PyJWT
> migration and still carries `paramiko`, `pynacl` and `ecdsa`, all of which the
> amd64 lock has dropped. `scripts/ci/hygiene-check.sh` reports it as
> *"npci lock (arm64) — 4 pin(s) absent"* while amd64 is *in sync*.
>
> The figures below were measured on an **arm64** image and therefore describe
> the stale closure. Regenerate the arm64 lock, then regenerate this file, before
> treating these counts as the shipped inventory.

## Licence distribution

| Licence | Packages |
|---|---:|
| MIT (incl. variants) | 79 |
| BSD (2/3-clause and unversioned) | 44 |
| Apache-2.0 (incl. variants) | 32 |
| Copyleft / weak-copyleft (below) | 10 |
| Dual permissive (`MIT OR Apache-2.0`, etc.) | 6 |
| PSF-2.0 / CNRI-Python | 3 |
| HPND / MIT-CMU | 1 |
| ISC | 1 |
| Unlicense | 1 |
| No licence metadata | 7 |
| Other single declarations | 1 |

The seven without metadata ship no licence field and must be checked by hand
before a public release: `aiologic`, `culsans`, `jinja2`, `multilspy`,
`prompt-toolkit`, `safetensors`, `tokenizers`. Most are almost certainly
permissive — `jinja2` is BSD-3-Clause upstream — so this is a metadata gap
rather than a licensing one, but "almost certainly" is not the standard a
release note should meet.

## Copyleft and weak-copyleft dependencies

Read this section before distributing binaries or a container image. **None of
these change the licence of this project's own code**, which is MIT —
they are imported at runtime, unmodified, as installed from PyPI.

**`pyphen` (GPL-2.0 metadata) is no longer a dependency.** It was transitive via
`weasyprint`, which nothing in `app/` imported; both were removed on 2026-08-11
(review SEC-4). An SCA report predating that date will still name it — reconcile
against `backend/requirements.txt`, not against the older report.

**`paramiko` (LGPL-2.1) was removed as a direct pin on 2026-08-20** — nothing in
the repo imported it, and the SSH path has always run on `asyncssh`. The removal
clears CVE-2026-44405 and orphans `pynacl` (paramiko was its only consumer).

**It has not left the arm64 build.** `requirements.txt` records the removal and
says to expect the regenerated locks to drop both packages. `requirements.amd64.lock`
did. `requirements.arm64.lock` did not, and still pins `paramiko==3.5.0` and
`pynacl` — so an arm64 image built today still installs them, and the scan above
still finds them. Do not treat this row as resolved until the arm64 lock is
regenerated.

| Package | Version | Declared | Notes |
|---|---|---|---|
| `asyncssh` | 2.24.0 | `EPL-2.0 OR GPL-2.0-or-later` | Dual-licensed; EPL-2.0 elected. Under Apache-2.0 that election was forced, because Apache-2.0 is one-way incompatible with GPL-2.0. MIT is compatible with both, so the choice is now free rather than constrained; EPL-2.0 is retained so the position does not change silently. |
| `ldap3` | 2.9.1 | LGPL-3.0 | Imported unmodified. |
| `python-gitlab` | 4.9.0 | LGPL-3.0-or-later | Imported unmodified. |
| `psycopg2-binary` | 2.9.10 | LGPL with exceptions | The exceptions permit this use. |
| `docstring-to-markdown` | 0.17 | LGPL-2.1-or-later | Transitive via `multilspy`. |
| `autocommand` | 2.2.2 | LGPLv3 | Transitive. |
| `certifi` | 2026.7.22 | MPL-2.0 | File-level copyleft; unmodified use is fine. |
| `orjson` | 3.11.9 | `MPL-2.0 AND (Apache-2.0 OR MIT)` | Fine. |
| `tqdm` | 4.70.0 | `MPL-2.0 AND MIT` | Fine. |

### The position, and its limits

On the standard reading — a Python `import` is dynamic linking, and LGPL §6 is
satisfied by shipping unmodified upstream packages via pip — none of the above
requires this project to change its licence.

**That is a legal conclusion, and this file is not legal advice.** It is written
so counsel has the facts in one place rather than an SBOM to wade through.
Confirm it before publication.

## Heavyweight optional dependencies

`torch` and `sentence-transformers` are pulled in for the local reranker: a
multi-gigabyte install that every adopter currently pays for whether or not they
use it. Moving them behind an optional extra is tracked in the OSS-readiness
plan.
