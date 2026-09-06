# Example Phase B scripts (build + deploy, UAT tests)

Sample operator scripts for the script-based Phase B stages. The Build panel
and the combined UAT panel take a **script path** as a parameter; the backend
validates it against the `PHASE_B_SCRIPT_ROOT` allowlist directory (symlinks
resolved, containment enforced, `.sh` only, `local` runner mode only) and
streams the script's output live into the UI.

To use these samples, point the backend at this directory:

```
PHASE_B_RUNNER_MODE=local
PHASE_B_SCRIPT_ROOT=<absolute path to>/backend/examples/phase_b_scripts
```

then enter a path **relative to that root** in the panel, e.g.
`nlln/build_and_deploy.sh`.

## Script contracts

**Build + deploy** — invoked as `bash <script> <branch_a> <branch_b>`.
Recognised output cues (all optional, see `services/build_runner.py`):
`== Deploy ==` / `== Startup ==` section headers, `Building jar: <path>`,
`cp <artifact> <dest>` deploy lines, and a final `BUILD SUCCESS` /
`BUILD FAILURE`. Exit 0 on success.

**UAT tests** — invoked as `bash <script> [base_url]`; the combined
test-gen + test-exec step. Emit one `PASS <id> <title>` / `FAIL <id> <title>`
/ `SKIP <id> <title>` line per case and finish with
`TESTS: total=N passed=N failed=N skipped=N` (see `services/uat_script.py`).
Exit 0 iff no failures — the pipeline advances to Triage either way; failures
are what the AI triage step is for.

## `nlln/` — the current sample use case

Scripts for the NLLN library-loan fixture repo
(`gitlab.com/NirbhayN/nllm-fixtures`, expected at
`<repo-root>/.run/repos/nllm-fixtures` or via `NLLN_FIXTURES_DIR`):

- `nlln/build_and_deploy.sh` — validates the `nlln-v1.xsd` contract, packages
  the contract module into a versioned artifact, deploys it to
  `<repo-root>/.run/deploy/nlln/releases/`, and verifies it. No services are
  started, and the log says so — no simulated output.
- `nlln/run_uat_tests.sh` — seven contract-derived checks (schema parses,
  namespace, Req/Resp pairing, error-catalogue format, forbidden state-machine
  transition, deployed artifact, optional live health probe when a base URL is
  supplied).

Both are dependency-light: bash + python3, with `xmllint`/`jar` used when
present.
