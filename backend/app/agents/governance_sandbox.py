# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Sandboxed execution of governance-skill scripts + contract-true result parsing.

Runs ONE bundle script against a target directory and parses its result per the
script's declared execution contract (governance_bundle.validate_exec_manifest).
The contract encodes the forensic probe's three traps as behaviour:

1. **Never trust exit codes when a findings contract exists** — the probe found a
   real secret-validator that exits 0 even when it finds secrets; findings come
   from ``findings_parse`` (e.g. ``stdout.json.total_findings``), the exit code
   only signals crashed-vs-ran.
2. **Normalize declared non-deterministic fields** (uuid/timestamp) before any
   hash/drift comparison.
3. **Only role=validator results ever gate** — generators produce context.

Backends:
- ``docker``: preferred when a docker daemon is reachable — network=none,
  read-only rootfs for the target, memory/cpu caps, non-root.
- ``subprocess``: the operative backend where no daemon exists (the dev stack's
  celery container has the CLI but no socket). Confinement = the upload-time
  static safety gate (primary control: no privilege/download-exec/secret-read
  scripts are ever stored) + rlimits (CPU/AS/NOFILE/FSIZE) + scrubbed env +
  timeout + cwd inside a scratch dir. HONEST LIMIT: this backend cannot block
  network egress at the OS level; the gate rejects scripts that DECLARE network
  need, static scan warns on network-capable imports, and the env carries no
  proxy/credentials. Use the docker backend where real isolation is required.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger("app.agentic.governance")

_ENV_KEEP = ("PATH", "LANG", "LC_ALL", "PYTHONIOENCODING", "TZ")
_OUT_CAP = 2_000_000          # bytes of stdout/stderr we retain


@dataclass
class ScriptResult:
    script: str
    role: str
    ran: bool                          # process started and finished (any exit code)
    exit_code: int | None
    findings_count: int | None         # None = no findings contract (or unparseable)
    findings: list = field(default_factory=list)   # structured items when available
    stdout: str = ""
    stderr: str = ""
    error: str | None = None           # harness-level failure (timeout, spawn error, parse)
    duration_s: float = 0.0

    @property
    def gate_findings(self) -> list:
        """What may GATE a review: only a validator's parsed findings."""
        return self.findings if self.role == "validator" else []


def _scrubbed_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k in _ENV_KEEP}
    env["HOME"] = "/tmp"
    env["GOVERNANCE_SANDBOX"] = "1"
    # Belt-and-braces network dampening for the subprocess backend: no proxies,
    # and a resolv-breaking hint is NOT portable — the static gate is the control.
    env["NO_PROXY"] = "*"
    return env


def _docker_available() -> bool:
    if getattr(settings, "governance_sandbox_backend", "auto") == "subprocess":
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _rlimit_preexec(timeout_s: int):
    import resource

    def _apply():
        os.setsid()
        cpu = max(30, timeout_s)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 30))
        mem = 2 * 1024 * 1024 * 1024                     # 2 GB address space
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        fsz = 512 * 1024 * 1024                          # 512 MB max written file
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsz, fsz))
    return _apply


def _render_invocation(invocation: str, *, bundle_dir: Path, target_dir: Path,
                       output_path: Path) -> list[str]:
    rendered = (invocation
                .replace("{target}", str(target_dir))
                .replace("{output}", str(output_path))
                .replace("{bundle}", str(bundle_dir)))
    return shlex.split(rendered)


def run_script(contract: dict, *, bundle_dir: Path, target_dir: Path,
               scratch_dir: Path | None = None) -> ScriptResult:
    """Execute one script per its contract. Never raises for a script failure —
    everything lands in the ScriptResult so callers decide what gates."""
    import time as _time

    script = contract["path"]
    role = contract.get("role") or "generator"
    timeout_s = int(contract.get("timeout_seconds") or 300)
    res = ScriptResult(script=script, role=role, ran=False, exit_code=None,
                       findings_count=None)
    spath = (bundle_dir / script)
    if not spath.is_file():
        res.error = f"script not found in bundle: {script}"
        return res
    scratch = Path(scratch_dir) if scratch_dir else Path(tempfile.mkdtemp(prefix="govrun-"))
    scratch.mkdir(parents=True, exist_ok=True)
    output_path = scratch / "output.json"
    argv = _render_invocation(contract.get("invocation") or f"python3 {script} {{target}}",
                              bundle_dir=bundle_dir, target_dir=target_dir,
                              output_path=output_path)
    t0 = _time.monotonic()
    try:
        if _docker_available():
            image = getattr(settings, "governance_sandbox_image", "python:3.10-slim")
            # Run as the INVOKING uid (non-root in the stack): mkdtemp dirs are 0700
            # owner-only, so a fixed 'nobody' uid cannot even read the mounted bundle.
            uid = f"{os.getuid()}:{os.getgid()}"
            argv = ["docker", "run", "--rm", "--network=none", "--memory=2g",
                    "--cpus=1", "--pids-limit=256", "--user", uid,
                    "-v", f"{bundle_dir}:{bundle_dir}:ro",
                    "-v", f"{target_dir}:{target_dir}:ro",
                    "-v", f"{scratch}:{scratch}:rw",
                    "-w", str(bundle_dir), image] + argv
            proc = subprocess.run(argv, capture_output=True, timeout=timeout_s + 60,
                                  env=_scrubbed_env())
        else:
            proc = subprocess.run(argv, capture_output=True, timeout=timeout_s,
                                  cwd=str(bundle_dir), env=_scrubbed_env(),
                                  preexec_fn=_rlimit_preexec(timeout_s))
        res.ran = True
        res.exit_code = proc.returncode
        res.stdout = proc.stdout.decode("utf-8", errors="replace")[:_OUT_CAP]
        res.stderr = proc.stderr.decode("utf-8", errors="replace")[:_OUT_CAP]
    except subprocess.TimeoutExpired:
        res.error = f"timed out after {timeout_s}s"
        return res
    except Exception as e:  # noqa: BLE001 — spawn/backend failure, not the script's fault
        res.error = f"sandbox failure: {type(e).__name__}: {e}"
        return res
    finally:
        res.duration_s = round(_time.monotonic() - t0, 2)

    _parse_findings(res, contract, output_path)
    return res


def run_shell(command: str, *, cwd: Path, ro_dirs: list[Path] | None = None,
              rw_dirs: list[Path] | None = None, timeout_s: int = 300) -> dict:
    """Claude-Code-parity shell for the governance review agent: run one
    ``bash -lc`` command inside the sandbox. The agent follows SKILL.md's own
    invocations verbatim instead of a pre-declared contract; auditability = the
    transcript, exactly the Claude Code model.

    DOCKER IS REQUIRED HERE, unlike run_script. That is a deliberate divergence
    from upstream, decided for this repo:

    `run_script` executes only DECLARED scripts — they pass the upload static
    gate and the validator floor, and the command is on disk to audit — so its
    rlimit-subprocess fallback still has real containment behind it. `gov_bash`
    runs MODEL-AUTHORED commands and bypasses the static gate by construction
    (the command is never stored). On the subprocess backend there is no
    filesystem or network isolation, while the tool schema tells the model
    "network DISABLED" — so the fallback would both lose the containment and
    lie about it.

    Refusing is therefore the honest failure: no daemon, no shell. Skills that
    need a shell do not silently run unisolated; they report that the backend
    is unavailable. ro/rw_dirs bind only in docker, which is now the only path."""
    import time as _time
    import uuid as _uuid

    timeout_s = max(10, min(int(timeout_s or 300), 1800))
    out: dict = {"ran": False, "exit_code": None, "stdout": "", "stderr": "",
                 "error": None, "duration_s": 0.0}
    t0 = _time.monotonic()
    cname: str | None = None
    try:
        if _docker_available():
            image = getattr(settings, "governance_sandbox_image", "python:3.10-slim")
            uid = f"{os.getuid()}:{os.getgid()}"
            # Named so a timeout can actually STOP it: subprocess.run's timeout kills the
            # docker CLIENT, not the container, and `--rm` only reaps on the container's
            # own exit — so a hung command would otherwise keep running (holding its 2g +
            # cpu + the bind mounts) with nothing left watching it.
            cname = f"gov-sh-{_uuid.uuid4().hex[:12]}"
            argv = ["docker", "run", "--rm", "--name", cname,
                    "--network=none", "--memory=2g",
                    "--cpus=1", "--pids-limit=256", "--user", uid]
            for d in (ro_dirs or []):
                argv += ["-v", f"{d}:{d}:ro"]
            for d in (rw_dirs or []):
                argv += ["-v", f"{d}:{d}:rw"]
            argv += ["-w", str(cwd), image, "bash", "-lc", command]
            proc = subprocess.run(argv, capture_output=True, timeout=timeout_s + 60,
                                  env=_scrubbed_env())
        else:
            # Option (b): require docker for bash. See the docstring — the
            # subprocess backend cannot isolate a model-authored command, and
            # degrading to it silently is worse than not running.
            out["error"] = (
                "bash is unavailable: it requires the docker sandbox backend and no "
                "docker daemon is reachable. Declared skill scripts (run_script) still "
                "run on the subprocess backend; ad-hoc shell does not."
            )
            out["duration_s"] = round(_time.monotonic() - t0, 3)
            return out
        out["ran"] = True
        out["exit_code"] = proc.returncode
        out["stdout"] = proc.stdout.decode("utf-8", errors="replace")[:_OUT_CAP]
        out["stderr"] = proc.stderr.decode("utf-8", errors="replace")[:_OUT_CAP]
    except subprocess.TimeoutExpired:
        out["error"] = f"timed out after {timeout_s}s"
        if cname:
            try:                          # best-effort reap; --rm removes it once killed
                subprocess.run(["docker", "kill", cname], capture_output=True, timeout=30)
            except Exception:  # noqa: BLE001 — cleanup must not mask the timeout
                pass
    except Exception as e:  # noqa: BLE001 — spawn/backend failure
        out["error"] = f"sandbox failure: {type(e).__name__}: {e}"
    finally:
        out["duration_s"] = round(_time.monotonic() - t0, 2)
    return out


# ── Contract-true findings parsing ────────────────────────────────────────────

def _dig(obj, dotted: str):
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _parse_findings(res: ScriptResult, contract: dict, output_path: Path) -> None:
    """Fill findings_count/findings per the declared contract. TRAP 1 lives here:
    with a findings contract, the exit code is IGNORED for pass/fail — a validator
    that exits 0 with findings still reports them."""
    fmt = contract.get("output_format") or "exit_code"
    parse = contract.get("findings_parse")
    if fmt == "xml":
        _parse_xml(res, contract, output_path)
        return
    try:
        if fmt == "exit_code":
            # exit_semantics like "0=pass nonzero=findings" — findings count is 0/1ish.
            if res.role == "validator" and res.ran:
                res.findings_count = 0 if res.exit_code == 0 else 1
                if res.findings_count:
                    res.findings = [{"why": f"{res.script} failed "
                                            f"(exit {res.exit_code}): {res.stderr[:300] or res.stdout[:300]}"}]
            return
        raw = None
        if fmt in ("json_stdout",):
            raw = json.loads(res.stdout or "null")
        elif fmt in ("json_file", "sarif", "junit"):
            if output_path.is_file():
                raw = json.loads(output_path.read_text(encoding="utf-8", errors="replace"))
            else:
                res.error = f"declared output file missing: {output_path.name}"
                return
        elif fmt == "freetext":
            return
        raw = _normalize(raw, contract.get("normalize") or [])
        if fmt == "sarif":
            items = [{"why": (r.get("message") or {}).get("text") or "",
                      "file": ((r.get("locations") or [{}])[0].get("physicalLocation") or {})
                      .get("artifactLocation", {}).get("uri"),
                      "rule": r.get("ruleId")}
                     for run_ in (raw or {}).get("runs", []) for r in run_.get("results", [])]
            res.findings, res.findings_count = items, len(items)
            return
        if parse:
            count = _dig(raw, parse.split("stdout.json.")[-1] if parse.startswith("stdout.json.")
                         else parse)
            items = _dig(raw, "items") or _dig(raw, "findings") or []
            res.findings_count = int(count) if count is not None else (len(items) or None)
            res.findings = items if isinstance(items, list) else []
            if res.findings_count is None:
                res.error = f"findings_parse {parse!r} matched nothing in the output"
        else:
            items = (raw or {}).get("items") or (raw or {}).get("findings") or []
            res.findings, res.findings_count = list(items), len(items)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        res.error = f"output did not parse per contract ({fmt}): {e}"


def _xml_item(el) -> dict:
    """Best-effort finding record from a scanner-XML element (Checkmarx Result,
    generic <finding>/<issue>), reading whichever common attribute names exist."""
    a = {k.lower(): v for k, v in el.attrib.items()}
    return {"why": a.get("name") or a.get("query") or a.get("message")
                   or a.get("description") or el.tag,
            "file": a.get("filename") or a.get("file") or a.get("uri") or a.get("path"),
            "line": a.get("line") or a.get("linenumber") or a.get("linenum"),
            "severity": (a.get("severity") or a.get("sev") or "").lower() or None,
            "rule": a.get("queryid") or a.get("ruleid") or a.get("rule") or a.get("cweid")}


def _parse_xml(res: ScriptResult, contract: dict, output_path: Path) -> None:
    """Count findings in XML scanner output (Checkmarx ``CxXMLResults`` with
    ``<Result>`` elements, generic issue XML). ``findings_parse`` is an
    ElementTree element path — the finding count is the number of matching
    elements. Reads the ``{output}`` file if the script wrote one, else stdout.
    Parsed XXE-safe via defusedxml (external entities/DTDs rejected). Same
    contract as every validator: unparseable output is an error, never a pass."""
    from defusedxml.ElementTree import fromstring as _xml_fromstring

    src = ""
    if output_path.is_file():
        src = output_path.read_text(encoding="utf-8", errors="replace")
    if not src.strip():
        src = res.stdout or ""
    if not src.strip():
        res.error = "xml output was empty (no {output} file written and stdout empty)"
        return
    try:
        root = _xml_fromstring(src)
    except Exception as e:  # noqa: BLE001 — malformed XML or a rejected entity, not a pass
        res.error = f"xml did not parse per contract: {type(e).__name__}: {e}"
        return
    # Default to Checkmarx's per-instance element; authors override via findings_parse
    # (e.g. './/Result[@Severity=\"High\"]' to gate on High only).
    path = contract.get("findings_parse") or ".//Result"
    try:
        matches = root.findall(path)
    except (SyntaxError, KeyError) as e:
        res.error = f"findings_parse {path!r} is not a valid element path: {e}"
        return
    res.findings_count = len(matches)
    res.findings = [_xml_item(el) for el in matches[:500]]


# Volatile keys normalized when the contract declares none of its own — the probe's
# generator emits uuid4 + datetime.now(), making every identical re-run look changed.
# 'id' is deliberately NOT here: finding ids are meaningful, not volatile.
_DEFAULT_VOLATILE = ("uuid", "serialNumber", "timestamp", "generated_at", "created", "date")


def _normalize(obj, fields: list[str]):
    """TRAP 2: overwrite non-deterministic field VALUES (keys preserved) so re-runs
    compare stable. Declared fields win; else the default volatile set applies."""
    if obj is None:
        return None
    text = json.dumps(obj, sort_keys=True)
    for f in (fields or _DEFAULT_VOLATILE):
        text = re.sub(rf'"{re.escape(f)}"\s*:\s*"[^"]*"', f'"{f}":"<normalized>"', text)
    return json.loads(text)


# ── Bundle materialization + smoke ────────────────────────────────────────────

def materialize_bundle(row, dest: Path) -> Path:
    """Extract a skill row's bundle archive into ``dest`` (traversal-safe by
    construction: we re-parse via governance_bundle and write member by member)."""
    from app.agents import governance_bundle as GB

    parsed = GB.parse_bundle(row.bundle_bytes, row.bundle_filename or "bundle.tar.gz")
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    for f in parsed.files:
        p = dest / f.path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(f.content)
        if f.classification == "script":
            p.chmod(0o755)
    return dest


_SMOKE_BAD = {
    "src/app.py": ('AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"\n'
                   'password = "hunter2-super-secret"\n'
                   'token = "ghp_16charsoffakegithubtoken000000"\n'),
    "src/ok.py": "def add(a, b):\n    return a + b\n",
}
_SMOKE_GOOD = {"src/app.py": "def add(a, b):\n    return a + b\n"}


def smoke_bundle(row, work_dir: Path) -> dict:
    """The prove-it-runs gate (design §7). Every script must EXECUTE and PARSE per
    contract; every validator must additionally flag its known-BAD fixture
    (>= expect_bad_min findings) and run clean on its known-GOOD one.

    A skill may ship its OWN fixtures (these bundles carry ``evals/``) and point at
    them via a smoke block — per-script ``smoke``, else the bundle-level ``smoke``;
    otherwise the built-in Python-secret fixtures are used. A fixture may be a
    DIRECTORY (a scanner walks it) or a single FILE (a report-grader that takes
    ``--in <file>``). Returns {status: green|failed, scripts:[...]}. Smoke is now
    advisory — a non-green skill still runs, with a warning — but the verdict here
    is what marks a skill 'proven'."""
    em = row.exec_manifest_json or {}
    contracts = em.get("scripts") or []
    bundle_smoke = em.get("smoke") or {}
    bundle_dir = materialize_bundle(row, work_dir / "bundle")

    # Built-in fallback fixtures — used only for scripts that declare none.
    builtin: dict[str, Path] = {}
    for kind, files in (("bad", _SMOKE_BAD), ("good", _SMOKE_GOOD)):
        d = work_dir / f"builtin_{kind}"
        shutil.rmtree(d, ignore_errors=True)
        for rel, text in files.items():
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        builtin[kind] = d

    results: list[dict] = []
    ok = True
    for c in contracts:
        sm = c.get("smoke") or bundle_smoke or {}
        expect_min = int(sm.get("expect_bad_min") or 1)
        for kind in ("bad", "good"):
            rel = sm.get(kind)
            target = (bundle_dir / rel) if rel else builtin[kind]   # skill fixture (file/dir) or built-in
            r = run_script(c, bundle_dir=bundle_dir, target_dir=target,
                           scratch_dir=work_dir / f"scratch_{kind}_{Path(c['path']).stem}")
            entry = {"script": c["path"], "role": c["role"], "fixture": kind,
                     "fixture_src": (f"skill:{rel}" if rel else "built-in"),
                     "ran": r.ran, "exit_code": r.exit_code,
                     "findings_count": r.findings_count, "error": r.error,
                     "duration_s": r.duration_s}
            if not target.exists():
                entry["verdict"] = f"failed: fixture {rel!r} missing from bundle"
                ok = False
            elif not r.ran or r.error:
                entry["verdict"] = "failed"
                ok = False
            elif c["role"] == "validator" and kind == "bad" and (r.findings_count or 0) < expect_min:
                entry["verdict"] = (f"failed: validator found {r.findings_count or 0} in the "
                                    f"known-bad fixture, expected >= {expect_min}")
                ok = False
            elif c["role"] == "validator" and kind == "good" and (r.findings_count or 0) != 0:
                # A real known-good must be clean. This also catches a false-green: a
                # report-grader fed a directory errors out and its error can look like a
                # finding — that's a fixture mismatch, not a pass.
                entry["verdict"] = (f"failed: validator found {r.findings_count} in the "
                                    "known-good fixture, expected 0 (wrong fixture kind?)")
                ok = False
            elif c["role"] == "generator" and (r.exit_code or 0) != 0:
                # A generator's contract is 0=ok — a nonzero exit is a BROKEN script,
                # not a finding; it must not pass the prove-it-runs gate.
                entry["verdict"] = f"failed: generator exited {r.exit_code}"
                ok = False
            else:
                entry["verdict"] = "ok"
            results.append(entry)
    return {"status": "green" if ok else "failed", "scripts": results}
