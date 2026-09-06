# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase B build+deploy runner — Session 23 unified flow.

The platform invokes the operator's pre-existing host script
``build_and_deploy.sh`` over SSH. The script clones two repos
(network-core, network-2.0), builds with Maven, deploys 4 web artifacts and
starts services via per-service starter scripts.

We do NOT modify the host script. Instead we:

  1. SSH into the host (via :mod:`app.services.host_runner`).
  2. Stream stdout/stderr line-by-line.
  3. Strip ANSI colour codes and pattern-match to carve the unified
     log into three sections — build / deploy / startup — and to
     extract structured outcomes (artifacts, services, status).
  4. Persist everything on the ``BuildRun`` row.
  5. On success, advance the Phase B run directly to ``TEST_GEN``
     (the standalone Deploy step is removed in this flow — see
     ``frontend/.../PhaseB.jsx`` STEPS).

Parser is intentionally pragmatic: anything we can't classify falls
into the active section (default = build) so the UI always shows the
full log even when patterns don't match. Operators can refine the
regex set without schema churn.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shlex
import time
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core import diag
from app.models.base import generate_uuid, utcnow
from app.models.phase_b import (
    PhaseBRun, PhaseBStep, BuildRun, BuildRunStatus,
)
from app.services.host_runner import stream_remote_command
from app.services.local_runner import stream_local_command

logger = logging.getLogger(__name__)


# ── ANSI / log parsing ───────────────────────────────────────────────────────

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


# Stage transition cues. Order matters — we walk lines in order and flip the
# active section the first time any of these match. The fallback is "build".
_DEPLOY_CUES = (
    re.compile(r"^\s*={2,}\s*Deploy", re.IGNORECASE),
    re.compile(r"^\s*Deploying\b", re.IGNORECASE),
    re.compile(r"^\s*Copying artifact", re.IGNORECASE),
    re.compile(r"\bcp\s+.+\.(jar|war)\b"),
)

_STARTUP_CUES = (
    re.compile(r"^\s*={2,}\s*Start", re.IGNORECASE),
    re.compile(r"^\s*Starting\s+\w+", re.IGNORECASE),
    re.compile(r"\bstarter\.sh\b"),
    re.compile(r"^\s*Verifying\b", re.IGNORECASE),
    re.compile(r"\bps\s+-ef\b"),
)

# Per-line extractors.
_ARTIFACT_RE = re.compile(r"Building jar:\s+(?P<path>\S+\.jar)")
_DEPLOYED_RE = re.compile(r"(?:cp|copy)\s+(?:\S+\s+)*?(?P<path>\S+\.(?:jar|war))\s+(?P<dest>\S+)", re.IGNORECASE)
_BUILD_FAILURE_RE = re.compile(r"\bBUILD\s+FAILURE\b")
_BUILD_SUCCESS_RE = re.compile(r"\bBUILD\s+SUCCESS\b")
# `ps -ef` lines: capture java service name (best-effort).
# Example: appuser  12345  ... java -jar /path/network-backend.jar ...
#
# The name comes from the JAR the process was launched with, not from an
# allowlist of known service names. The previous pattern required the name to
# start with `upi|api|gateway|portal`, so any stack whose services are named
# otherwise silently reported "no services started" — including the
# `network-backend.jar` in the example line above, which never matched. A deploy
# script is operator-supplied and its services are named whatever the operator
# names them, so an allowlist here can only ever be wrong.
_SERVICE_PS_RE = re.compile(
    r"\s(?P<pid>\d+)\s+"                                   # the PID column of `ps -ef`
    r".*?\bjava\b"                                         # …belonging to a java process
    r".*?\s-jar\s+\S*?(?P<name>[A-Za-z0-9_.-]+)\.jar\b",   # …launched from <name>.jar
    re.IGNORECASE,
)


# Per-section line ceiling. A runaway script must not grow the in-memory
# buffers (and the TEXT columns they land in) without bound — beyond this the
# OLDEST lines of the section are dropped; the tail is what diagnoses a
# failure, and the full untruncated stream is still in the diag build log.
_MAX_SECTION_LINES = 20_000


class _LogParser:
    """Stateful slicer for the unified host-script log."""

    def __init__(self) -> None:
        self.section: str = "build"
        self.build: list[str] = []
        self.deploy: list[str] = []
        self.startup: list[str] = []
        self.artifacts: list[dict] = []
        self.services: list[dict] = []
        self.first_artifact_path: Optional[str] = None
        self.saw_build_failure: bool = False
        self.saw_build_success: bool = False
        self.truncated: bool = False

    def feed(self, raw_line: str) -> None:
        line = _strip_ansi(raw_line)

        # Stage transitions are sticky: once we move to deploy/startup we
        # don't go back. (Operators occasionally retry a sub-step inside the
        # script; treating that as a section bounce would scramble the UI.)
        if self.section == "build":
            if any(cue.search(line) for cue in _STARTUP_CUES):
                self.section = "startup"
            elif any(cue.search(line) for cue in _DEPLOY_CUES):
                self.section = "deploy"
        elif self.section == "deploy":
            if any(cue.search(line) for cue in _STARTUP_CUES):
                self.section = "startup"

        target = (self.build if self.section == "build"
                  else self.deploy if self.section == "deploy" else self.startup)
        target.append(line)
        if len(target) > _MAX_SECTION_LINES:
            del target[0]
            self.truncated = True

        # Extractors run regardless of section so a misclassified line still
        # contributes structured data.
        if _BUILD_FAILURE_RE.search(line):
            self.saw_build_failure = True
        if _BUILD_SUCCESS_RE.search(line):
            self.saw_build_success = True

        m = _ARTIFACT_RE.search(line)
        if m and self.first_artifact_path is None:
            self.first_artifact_path = m.group("path")

        m = _DEPLOYED_RE.search(line)
        if m:
            entry = {"path": m.group("path"), "dest": m.group("dest")}
            if entry not in self.artifacts:
                self.artifacts.append(entry)

        m = _SERVICE_PS_RE.search(line)
        if m:
            entry = {"name": m.group("name"), "pid": m.group("pid")}
            # de-dup by service name (last seen pid wins)
            self.services = [s for s in self.services if s["name"] != entry["name"]]
            self.services.append(entry)

    def unified_log(self) -> str:
        # Preserve original section ordering with thin separators so users
        # can still grep one big blob if they expand "Show full log".
        parts = []
        if self.truncated:
            parts.append(f"[backend] output exceeded {_MAX_SECTION_LINES} lines — "
                         "oldest lines dropped; the diagnostics build log holds the full stream")
        if self.build:
            parts.append("\n".join(self.build))
        if self.deploy:
            parts.append("=== Deploy ===\n" + "\n".join(self.deploy))
        if self.startup:
            parts.append("=== Startup ===\n" + "\n".join(self.startup))
        return "\n".join(parts)


# ── Runner ───────────────────────────────────────────────────────────────────

def _build_command(core_branch: str, app_branch: str, script: Optional[str] = None) -> str:
    """Compose the remote bash invocation. shlex-quoted throughout: the branch
    names come from the UI, and `script` (when given) is the request-supplied
    path the caller already validated against PHASE_B_SCRIPT_ROOT — quoting
    keeps each a single argv token with no shell metacharacters either way."""
    script = (script or settings.phase_b_build_script or "").strip()
    if not script:
        # Neither a request-supplied path nor a configured default. Without this
        # the command became `bash '' <a> <b>` and the operator's only clue was a
        # build log reading `bash: : No such file or directory` — which reads as
        # "the script is missing" rather than "no script was ever selected".
        raise ValueError(
            "no build+deploy script to run: the Build panel's script field was left "
            "empty and PHASE_B_BUILD_SCRIPT is not configured on this deployment. "
            "Set PHASE_B_BUILD_SCRIPT to an absolute path, or enter a path relative "
            "to PHASE_B_SCRIPT_ROOT in the panel (e.g. nlln/build_and_deploy.sh)."
        )
    return f"bash {shlex.quote(script)} {shlex.quote(core_branch)} {shlex.quote(app_branch)}"


# ── Demo runner (fully simulated) ────────────────────────────────────────────
# Used when PHASE_B_RUNNER_MODE=demo ("mock" is accepted as a deprecated alias).
#
# EVERY value this runner produces is fabricated — the build log, the deploy
# transcript, the service list, the timings. It exists so the UI flow can be
# demonstrated on a laptop with no build host, and it is legitimate for that.
# What was NOT legitimate was presenting it as a real deployment to a named
# production host, so: the host label now says "simulated", the logs carry an
# explicit SIMULATED banner, and no internal hostname appears anywhere.
# Skips SSH entirely, sleeps ~30 s with
# a realistic Maven-shaped log, persists a BuildRun row, advances the step.
# Intended as a stand-in for live demos and for environments where the real
# build host isn't reachable. The schedule mirrors the frontend canned demo
# in `frontend/src/lib/demoBuildLogs.js` so the two stay in lockstep — when
# `?demo=1` is also active, the visual stream finishes right when the
# backend mock returns its BuildRun.
_MOCK_TOTAL_SECONDS = 30
_MOCK_BUILD_LOG = """\
*** SIMULATED OUTPUT — no build or deployment actually ran ***
[INFO] Scanning for projects...
[INFO] ------------------------------------------------------------------------
[INFO] Reactor Build Order:
[INFO]
[INFO] network-core                                                          [jar]
[INFO] network-2.0-app                                                       [jar]
[INFO] network-stack                                                         [pom]
[INFO]
[INFO] ---------------------< com.example.network:network-core >----------------------
[INFO] Building network-core 2.4.7-SNAPSHOT                                  [1/3]
[INFO] --------------------------------[ jar ]---------------------------------
[INFO] Downloaded from internal-nexus: spring-boot-starter-web-3.2.0.pom (4.7 kB)
[INFO] Downloaded from internal-nexus: spring-boot-starter-data-jpa-3.2.0.pom (4.1 kB)
[INFO] Downloaded from internal-nexus: postgresql-42.7.1.jar (1.0 MB at 1.9 MB/s)
[INFO] Downloaded from internal-nexus: network-common-1.8.3.jar (412 kB at 1.2 MB/s)
[INFO] Downloaded from internal-nexus: upi-protocol-3.1.0.jar (788 kB at 2.1 MB/s)
[INFO]
[INFO] --- maven-compiler-plugin:3.12.1:compile (default-compile) @ network-core ---
[INFO] Compiling 184 source files with javac [debug release 17] to target/classes
[INFO] Compiled 184 source files in 1.49 s
[INFO]
[INFO] --- maven-surefire-plugin:3.2.5:test (default-test) @ network-core ---
[INFO] Running com.example.network.core.escrow.model.EscrowStateMachineTest
[INFO] Tests run: 9, Failures: 0, Errors: 0, Skipped: 0
[INFO] Running com.example.network.core.escrow.service.ConditionalPaymentServiceTest
[INFO] Tests run: 14, Failures: 0, Errors: 0, Skipped: 0
[INFO] Running com.example.network.core.security.JwtAuthFilterTest
[INFO] Tests run: 11, Failures: 0, Errors: 0, Skipped: 0
[INFO] Running com.example.network.core.integration.ConditionalPaymentControllerIT
[INFO] Tests run: 12, Failures: 0, Errors: 0, Skipped: 0
[INFO]
[INFO] Results:
[INFO] Tests run: 71, Failures: 0, Errors: 0, Skipped: 0
[INFO]
[INFO] Building jar: network-core/target/network-core-2.4.7-SNAPSHOT.jar
[INFO] Building jar: network-2.0-app/target/network-2.0-app-4.1.2-SNAPSHOT.jar
[INFO] ------------------------------------------------------------------------
[INFO] Reactor Summary for network-stack 1.0.0:
[INFO]
[INFO] network-core ........................................... SUCCESS [ 16.142 s]
[INFO] network-2.0-app ........................................ SUCCESS [ 10.418 s]
[INFO] network-stack .......................................... SUCCESS [  0.214 s]
[INFO] ------------------------------------------------------------------------
[INFO] BUILD SUCCESS
[INFO] ------------------------------------------------------------------------
[INFO] Total time:  26.774 s
"""

_MOCK_DEPLOY_LOG = """\
*** SIMULATED — nothing was copied to any host ***
$ scp network-core/target/network-core-2.4.7-SNAPSHOT.jar deploy@demo-host.invalid:/opt/network-core/releases/
network-core-2.4.7-SNAPSHOT.jar                          100%   78MB  86.2MB/s   00:00
$ scp network-2.0-app/target/network-2.0-app-4.1.2-SNAPSHOT.jar deploy@demo-host.invalid:/opt/network-2.0/releases/
network-2.0-app-4.1.2-SNAPSHOT.jar                       100%   91MB  82.7MB/s   00:01
$ ln -sf /opt/network-core/releases/network-core-2.4.7-SNAPSHOT.jar /opt/network-core/current.jar
$ ln -sf /opt/network-2.0/releases/network-2.0-app-4.1.2-SNAPSHOT.jar /opt/network-2.0/current.jar
── Deploy complete. ──
"""

_MOCK_STARTUP_LOG = """\
*** SIMULATED — no service was started ***
$ systemctl restart network-core
● network-core.service - UPI Core Switch
   Active: active (running); 0s ago
   Main PID: 18472 (java)
$ systemctl restart upi-2-0
● network-2-0.service - UPI 2.0 PSP Switch
   Active: active (running); 0s ago
   Main PID: 18519 (java)
── All services healthy. ──
"""

_MOCK_DEPLOYED_ARTIFACTS = [
    {
        "path": "network-core/target/network-core-2.4.7-SNAPSHOT.jar",
        "dest": "/opt/network-core/releases/network-core-2.4.7-SNAPSHOT.jar",
    },
    {
        "path": "network-2.0-app/target/network-2.0-app-4.1.2-SNAPSHOT.jar",
        "dest": "/opt/network-2.0/releases/network-2.0-app-4.1.2-SNAPSHOT.jar",
    },
]

_MOCK_SERVICES_STARTED = [
    {"name": "network-core.service", "pid": 18472},
    {"name": "network-2-0.service",  "pid": 18519},
]


def _adopt_build_run(
    db: Session,
    build_run: Optional[BuildRun],
    run: PhaseBRun,
    *,
    core_branch: str,
    app_branch: str,
    host: str,
    status: BuildRunStatus,
    script_path: Optional[str] = None,
) -> BuildRun:
    """Use the trigger endpoint's pre-created row when given — the endpoint
    returns it immediately and the UI polls it while this coroutine streams —
    else create one (legacy direct callers). Either way the row leaves here
    committed with the mode's host label and status."""
    if build_run is None:
        build_run = BuildRun(
            id=generate_uuid(),
            phase_b_run_id=run.id,
            iteration_number=run.iteration_count,
            status=status,
            triggered_at=utcnow(),
            core_branch=core_branch,
            app_branch=app_branch,
            host=host,
            script_path=script_path,
        )
        db.add(build_run)
    else:
        build_run.status = status
        build_run.host = host
        if script_path:
            build_run.script_path = script_path
    db.commit()
    db.refresh(build_run)
    return build_run


async def _run_demo_build(
    run: PhaseBRun,
    db: Session,
    *,
    core_branch: str,
    app_branch: str,
    build_run: Optional[BuildRun] = None,
) -> BuildRun:
    """Create + complete a SIMULATED BuildRun without touching SSH.

    Used when phase_b_runner_mode == "demo". Persists a real row so the
    rest of the platform (step advance, latest-build query, log download)
    behaves identically to a successful real run.

    Sleeps ~30 s in three chunks so the wall-clock matches the frontend
    visual demo. If the demo URL flag is active the user sees a streaming
    log on the page; this just paces the backend completion to match.
    Replace this function entirely once the real implementation lands —
    no other call site depends on `_run_demo_build`.
    """
    build_run = _adopt_build_run(
        db, build_run, run,
        core_branch=core_branch, app_branch=app_branch,
        host="demo (simulated — nothing was deployed)",
        status=BuildRunStatus.RUNNING,
    )

    logger.info(
        "Build+deploy SIMULATED (demo mode) started: change=%s core=%s upi2=%s",
        run.change_request_id, core_branch, app_branch,
    )

    # Three phase sleeps so cancellation (process restart) lands on a
    # boundary rather than in the middle of one giant await.
    await asyncio.sleep(_MOCK_TOTAL_SECONDS * 0.75)   # build
    await asyncio.sleep(_MOCK_TOTAL_SECONDS * 0.15)   # deploy
    await asyncio.sleep(_MOCK_TOTAL_SECONDS * 0.10)   # startup

    build_run.build_log         = _MOCK_BUILD_LOG.rstrip()
    build_run.deploy_log        = _MOCK_DEPLOY_LOG.rstrip()
    build_run.startup_log       = _MOCK_STARTUP_LOG.rstrip()
    build_run.deployed_artifacts = _MOCK_DEPLOYED_ARTIFACTS
    build_run.services_started   = _MOCK_SERVICES_STARTED
    build_run.artifact_path      = _MOCK_DEPLOYED_ARTIFACTS[0]["path"]
    build_run.completed_at       = utcnow()
    build_run.status             = BuildRunStatus.SUCCESS

    # Advance to UAT just like the real success path.
    run.current_step = PhaseBStep.TEST_GEN
    db.commit()

    logger.info(
        "Build+deploy SIMULATED (demo mode) complete: change=%s (artifacts=%d, services=%d)",
        run.change_request_id,
        len(_MOCK_DEPLOYED_ARTIFACTS), len(_MOCK_SERVICES_STARTED),
    )
    return build_run


# ── Local real-build runner (build only, no deploy) ───────────────────────────
# PHASE_B_RUNNER_MODE=build. Clones the registered repos fresh and runs a plain
# `mvn clean install -DskipTests --fail-at-end` per repo (core first, then the
# app), IN SEQUENCE — no LLM/agent, no host build_and_deploy.sh. Module exclusion
# reuses the verification gate verbatim: a build failure attributable ONLY to a
# module matched by AGENTIC_VERIFY_SKIP_MODULES (e.g. `*iupi*,*hsm-proxy*`) is
# downgraded and does not fail the build. The BUILD is real; the DEPLOY is mocked
# from the jars actually produced, so the artifact versions on screen match the
# real build. Clone + mvn run via the same contained subprocess path as verify,
# so this works wherever verify works.
# The build runs on whatever host this process runs on. There is no deploy target
# because this mode does not deploy — see _BUILD_ONLY_NOTICE.
_BUILD_HOST_LABEL = "local build (no deployment)"

# `build` mode used to fabricate a deploy: a fixed list of four "deployed"
# artifacts, four systemd services with invented PIDs, and an scp/ln transcript
# against a named production host — all emitted verbatim regardless of what the
# reactor actually produced, and shown to the user as "Deployed Artifacts" and
# "Services Up".
#
# That was a real build followed by a fictional deployment presented as fact.
# Removed. This mode now reports the jars the build ACTUALLY produced (parsed
# from the Maven log) and states plainly that nothing was deployed.
_BUILD_ONLY_NOTICE = (
    "── No deployment performed ──\n"
    "PHASE_B_RUNNER_MODE=build compiles the registered repositories and stops.\n"
    "The artifacts listed above are the jars this build produced; they have not\n"
    "been copied, installed, or started anywhere. Use PHASE_B_RUNNER_MODE=ssh\n"
    "with PHASE_B_HOST/PHASE_B_BUILD_SCRIPT configured to perform a real deploy."
)


def _repo_slug(repo) -> str:
    """`network-core` / `network-2.0` — last path segment of the GitLab repo path."""
    return (repo.gitlab_repo or repo.label or "service").rstrip("/").split("/")[-1]


def _approved_agentic_run(db: Session, change_id: str):
    """The handed-off agentic run for this change — the most recent code/full run
    with an approved manifest (mirrors /phase-b/agentic-complete). Its workspace is
    the tree the agent wrote, verified and (optionally) pushed. None for a legacy
    (non-agentic) Phase B run."""
    from app.models.agentic import AgenticRun, ChangeManifest

    aruns = (db.query(AgenticRun)
             .filter(AgenticRun.change_request_id == change_id,
                     AgenticRun.kind.in_(("code", "full")))
             .order_by(AgenticRun.created_at.desc()).all())
    for r in aruns:
        man = (db.query(ChangeManifest).filter(ChangeManifest.run_id == r.id)
               .order_by(ChangeManifest.created_at.desc()).first())
        if man is not None and man.approved_at is not None:
            return r
    return None


def _resolve_build_repos(db: Session, core_branch: str, app_branch: str,
                         repo_ids: Optional[list] = None) -> list[tuple]:
    """Repos to build — core first, then app, then others. Each entry is
    ``(repo, branch, clone_url)``. Core repos use ``core_branch``; app repos use
    ``app_branch``; anything else falls back to the repo's own default branch.
    ``repo_ids`` (the agentic run's selected repos) scopes the set to exactly what
    the workspace holds; None = all registered repos."""
    from app.agents import workspace_local
    from app.models.code_repo import CodeRepo

    q = db.query(CodeRepo)
    if repo_ids:
        q = q.filter(CodeRepo.id.in_(list(repo_ids)))
    repos = q.all()
    repos.sort(key=lambda r: (0 if r.role == "core" else 1 if r.role == "app" else 2, r.label or r.id))
    out: list[tuple] = []
    for r in repos:
        branch = (core_branch if r.role == "core"
                  else app_branch if r.role == "app"
                  else (r.gitlab_branch or "master"))
        base_url = r.gitlab_url or settings.gitlab_url
        url = workspace_local.build_clone_url(base_url, r.gitlab_repo, settings.gitlab_token)
        out.append((r, branch, url))
    return out


def _build_argv() -> list[str]:
    """The operator's normal build — `mvn clean install -DskipTests -U --fae`. `-U`
    forces a snapshot/dependency refresh; `--fail-at-end` (--fae) keeps building the
    whole reactor after a module fails so EVERY failure is reported at the end (and a
    skip-listed module's failure can be downgraded by the gate). ONLINE on purpose:
    the workspace ~/.m2 may not be warmed, so `-o` would fail dependency resolution."""
    return ["mvn", "-B", "-DskipTests", "-U", "--fail-at-end", "clean", "install"]


def _jdk_override(repo_dir: Path) -> Optional[dict]:
    """Best-effort: build with the JDK the repo's poms target (mirrors verify), so a
    Java-version mismatch doesn't masquerade as a code failure. None = active JDK."""
    try:
        from app.agents import jdk_discovery
        from app.agents.verification_plan import detect_required_java
        required = detect_required_java(repo_dir)
        home = jdk_discovery.select_jdk_home(required) if required else None
        return {"JAVA_HOME": home} if home else None
    except Exception:  # noqa: BLE001 — JDK switching is best-effort
        return None


def _do_local_build(build_key: str, change_id: str, repos: list[tuple], *, reuse: bool) -> dict:
    """BLOCKING: build each repo's existing workspace in sequence; mock the deploy.

    When ``reuse`` is set, ``build_key`` is the agentic run's workspace — the tree
    the agent already wrote/verified/pushed — and each repo is built IN PLACE with no
    re-clone. A repo missing from the workspace is cloned as a fallback (and a
    non-reuse run clones everything fresh under ``build_key``). Runs off the event
    loop (``asyncio.to_thread``) and touches NO DB — the async caller persists. Stops
    at the first repo whose build fails the gate (a broken core means the app can't
    resolve it anyway)."""
    from app.agents import workspace_local
    from app.agents.platform_adapter import adapter
    # Reuse the verification gate verbatim so module exclusion behaves identically.
    from app.agents.verification_plan import VerificationStep, _skip_patterns, _step_gate

    skip_patterns = _skip_patterns()
    sections: list[str] = []
    ok = True
    reason = ""
    t0 = time.monotonic()

    # SCR finding #10 (Improper Resource Shutdown or Release) — `blog.close()`
    # used to be a plain statement after the loop, so any exception raised
    # inside the loop body (clone, `adapter.run_command`, `_step_gate`, ...)
    # would skip it and leak the open file handle for the rest of the process
    # lifetime. `with blog:` guarantees the handle is released on every exit
    # path, including one that propagates out of this function.
    with diag.open_build_log(change_id, utcnow().strftime("%Y%m%d-%H%M%S")) as blog:
        blog.write(f"=== UPI local build + mock deploy — change={change_id} workspace={build_key} ===")
        blog.write(f"mode={'reuse-workspace' if reuse else 'fresh-clone'}  "
                   f"repos: {', '.join(_repo_slug(r) for r, _, _ in repos) or '(none)'}")
        blog.write("-" * 72)

        if not repos:
            ok = False
            reason = "no repositories in scope — register network-core + network-2.0 (or run the agent first)"
            sections.append(f"[backend] {reason}")

        for repo, branch, clone_url in repos:
            slug = _repo_slug(repo)
            rd = workspace_local.repo_dir(build_key, repo.id)
            # Reuse the existing checkout when it's there; only clone when it's absent
            # (workspace GC'd, or a repo the agentic run never cloned). `mvn clean`
            # below gives a from-scratch build of the existing source either way.
            if rd.exists() and (rd / ".git").exists():
                blog.write(f"[backend] reusing existing workspace for {slug}: {rd}")
            else:
                try:
                    workspace_local.clone(build_key, repo.id, clone_url, branch)
                except Exception as e:  # noqa: BLE001 — a clone failure is a build failure, reported
                    ok = False
                    reason = reason or f"clone failed for {slug} ({branch})"
                    sections.append(f"=== {slug} ({branch}) ===\n[backend] clone failed: {e}")
                    blog.write(f"[backend] clone failed for {slug}: {e}")
                    continue   # keep going so the remaining repos still build

            argv = _build_argv()
            env_ov = _jdk_override(rd)
            blog.write(f"\n=== {slug} ({branch}) — {' '.join(argv)} (cwd={rd}) ===")
            res = (adapter.run_command(rd, argv, env_overrides=env_ov)
                   if env_ov else adapter.run_command(rd, argv))
            out = (res.stdout or "") + (res.stderr or "")
            blog.write(out)
            blog.write(f"[exit={res.exit_code} timed_out={res.timed_out} duration_ms={res.duration_ms}]")

            step = VerificationStep("install", repo.id, argv, module=slug, subdir=None)
            gate_pass, _hard, _soft, env_reason, _req = _step_gate(step, res, set(), skip_patterns)

            tail = out if len(out) <= 4000 else out[:2000] + "\n…[truncated]…\n" + out[-2000:]
            sections.append(f"=== {slug} ({branch}) — {'OK' if gate_pass else 'FAILED'} ===\n{tail.strip()}")

            if not gate_pass:
                ok = False
                reason = reason or env_reason or f"build issues in {slug}"
                # Don't stop — `--fail-at-end` already ran the whole reactor, and we want
                # every repo to build so all failures surface in the log at the end.

        duration_s = max(1, int(time.monotonic() - t0))
        real_ok = ok
        # PHASE_B_DEMO_FORCE_SUCCESS is gone. It reported a failed build as SUCCESS,
        # which is indefensible in a platform whose whole value is telling you whether
        # your change builds. A demo that needs green output should use
        # PHASE_B_RUNNER_MODE=demo, which is labelled as simulated end to end.
        blog.write("-" * 72)
        blog.write(f"ok={ok} duration={duration_s}s "
                   f"reason={reason or '-'} finished={utcnow().isoformat()}")

    # Report what the build ACTUALLY produced. `_ARTIFACT_RE` matches Maven's
    # "Building jar: <path>" lines, so this list is real output, not a fixture.
    full_log = "\n\n".join(sections)
    artifacts = [{"path": m.group("path"), "dest": None}
                 for m in _ARTIFACT_RE.finditer(full_log)]
    services: list[dict] = []          # nothing is started — nothing to report
    deploy_log = _BUILD_ONLY_NOTICE
    first_artifact = artifacts[0]["path"] if artifacts else None

    # Reclaim a throwaway fresh clone (200MB–2GB). NEVER delete a reused agentic
    # workspace — it belongs to the run (push-deferred source / re-runs); its
    # lifecycle is the workspace GC's job, not ours.
    if not reuse:
        try:
            workspace_local.cleanup_workspace(build_key)
        except Exception:  # noqa: BLE001 — cleanup is best-effort
            pass

    return {
        "ok": ok,
        "real_ok": real_ok,
        "reason": reason,
        "build_log": full_log,
        "deploy_log": deploy_log,
        "artifacts": artifacts,
        "services": services,
        "duration_s": duration_s,
        "first_artifact": first_artifact,
    }


async def _run_local_build(
    run: PhaseBRun,
    db: Session,
    *,
    core_branch: str,
    app_branch: str,
    build_run: Optional[BuildRun] = None,
) -> BuildRun:
    """Real local clone+mvn build (core → app, sequential). Does NOT deploy.

    Persists a real BuildRun so the rest of the platform behaves identically to the
    other modes. On success advances directly to ``TEST_GEN`` like the real path."""
    build_run = _adopt_build_run(
        db, build_run, run,
        core_branch=core_branch, app_branch=app_branch,
        host=_BUILD_HOST_LABEL,
        status=BuildRunStatus.RUNNING,
    )

    # Prefer the agentic run's existing workspace — the tree the agent already wrote,
    # verified and (optionally) pushed — and build it IN PLACE (no re-clone). Fall
    # back to a fresh throwaway clone only when there's no agentic workspace on disk
    # (legacy Phase B, or the workspace was GC'd).
    from app.agents import workspace_local

    approved = _approved_agentic_run(db, run.change_request_id)
    ws_run_id = (approved.workspace_run_id or approved.id) if approved else None
    reuse = bool(ws_run_id and workspace_local.run_dir(ws_run_id).exists())
    build_key = ws_run_id if reuse else build_run.id
    repo_ids = (approved.selected_repo_ids or None) if (approved and reuse) else None
    repos = _resolve_build_repos(db, core_branch, app_branch, repo_ids=repo_ids)
    logger.info(
        "Build LOCAL started: change=%s mode=%s workspace=%s repos=%s core=%s upi2=%s",
        run.change_request_id, "reuse" if reuse else "fresh-clone", build_key,
        [_repo_slug(r) for r, _, _ in repos], core_branch, app_branch,
    )

    # Build is blocking; run it off the event loop so the API stays responsive
    # (the request still waits for the full build, like ssh mode does).
    result = await asyncio.to_thread(
        _do_local_build, build_key, run.change_request_id, repos, reuse=reuse)

    build_run.build_log          = result["build_log"]
    build_run.deploy_log         = result["deploy_log"]
    build_run.startup_log        = None        # startup log dropped from the UI (demo)
    build_run.deployed_artifacts = result["artifacts"] or None
    build_run.services_started   = result["services"] or None
    build_run.artifact_path      = result["first_artifact"]
    build_run.completed_at       = utcnow()

    if result["ok"]:
        build_run.status = BuildRunStatus.SUCCESS
        run.current_step = PhaseBStep.TEST_GEN
        db.commit()
        logger.info(
            "Build LOCAL success: change=%s real_ok=%s artifacts=%d services=%d duration=%ds",
            run.change_request_id, result.get("real_ok"),
            len(result["artifacts"]), len(result["services"]), result["duration_s"],
        )
    else:
        build_run.status = BuildRunStatus.FAILURE
        db.commit()
        logger.error("Build LOCAL failed: change=%s reason=%s", run.change_request_id, result["reason"])
    return build_run


async def run_build_and_deploy(
    run: PhaseBRun,
    db: Session,
    *,
    core_branch: Optional[str] = None,
    app_branch: Optional[str] = None,
    script_path: Optional[str] = None,
    build_run: Optional[BuildRun] = None,
) -> BuildRun:
    """Run the host-side build+deploy script and persist a BuildRun row.

    Args:
        run:           Current PhaseBRun (must be at BUILD step — caller
                       enforces).
        db:            SQLAlchemy session.
        core_branch:   Branch to clone from network-core. Defaults to "master".
        app_branch:   Branch to clone from network-2.0. Defaults to "master".
        script_path:   Request-supplied build+deploy script, ALREADY validated
                       against PHASE_B_SCRIPT_ROOT by the caller
                       (services.script_paths). ssh/local modes only; None =
                       the fixed PHASE_B_BUILD_SCRIPT.
        build_run:     Pre-created QUEUED row from the trigger endpoint, when
                       the build runs as a background task and the UI polls
                       the row for live logs. None = create internally.

    Returns:
        The completed BuildRun. On success, ``run.current_step`` advances
        directly to ``TEST_GEN`` (the legacy DEPLOY step is bypassed).

    Modes (PHASE_B_RUNNER_MODE). Only ssh/local actually deploy anything:
        ssh    — default; SSH into phase_b_host and run build_and_deploy.sh.
                 REAL build, REAL deploy.
        local  — run build_and_deploy.sh on the backend container's host.
                 REAL build, REAL deploy.
        build  — clone the registered repos and run a plain `mvn clean install`
                 per repo (core → app, sequential, skip-module aware).
                 REAL build, NO deploy — reports the jars actually produced and
                 says so explicitly. See _run_local_build.
        demo   — everything simulated, labelled as such. Nothing is built,
                 copied or started. See _run_demo_build. ("mock" is a
                 deprecated alias.)
    """
    core_branch = (core_branch or "master").strip() or "master"
    app_branch = (app_branch or "master").strip() or "master"

    mode = (settings.phase_b_runner_mode or "ssh").strip().lower()
    if mode == "mock":
        # Renamed to "demo" because "mock" undersold what it does: every value
        # it returns is fabricated. Alias kept so existing .env files keep working.
        logger.warning("PHASE_B_RUNNER_MODE=mock is deprecated; use 'demo'.")
        mode = "demo"
    if mode == "demo":
        return await _run_demo_build(
            run, db, core_branch=core_branch, app_branch=app_branch,
            build_run=build_run,
        )
    if mode == "build":
        return await _run_local_build(
            run, db, core_branch=core_branch, app_branch=app_branch,
            build_run=build_run,
        )
    if mode not in ("ssh", "local"):
        logger.warning("Unknown phase_b_runner_mode=%r, falling back to 'ssh'", mode)
        mode = "ssh"
    host_label = "localhost" if mode == "local" else settings.phase_b_host

    build_run = _adopt_build_run(
        db, build_run, run,
        core_branch=core_branch, app_branch=app_branch,
        host=host_label, status=BuildRunStatus.RUNNING,
        script_path=script_path,
    )

    logger.info(
        "Build+deploy started: change=%s mode=%s host=%s core=%s upi2=%s script=%s",
        run.change_request_id, mode, host_label, core_branch, app_branch,
        script_path or "(configured default)",
    )

    parser = _LogParser()
    exit_code = -1
    try:
        command = _build_command(core_branch, app_branch, script_path)
    except ValueError as e:
        # Misconfiguration, not a build failure. Land it on the row the UI is
        # already polling — otherwise this raises past the trigger's background
        # task and leaves the build stuck RUNNING with an empty log forever.
        build_run.build_log = f"[backend] {e}"
        build_run.status = BuildRunStatus.FAILURE
        build_run.completed_at = utcnow()
        db.commit()
        logger.error("Build+deploy not started: change=%s: %s", run.change_request_id, e)
        return build_run

    # Dedicated, untruncated per-build log file (the main app log only keeps a
    # 300-char-per-line slice). Captures the exact command, where it ran, every
    # output line, and an exit/duration footer — the file the user goes to when
    # a UAT build fails. Fail-open: a no-op writer if the dir isn't writable.
    started_at = time.monotonic()
    # SCR finding #10 (Improper Resource Shutdown or Release) — `blog.close()`
    # used to be a plain statement after the try/except below. That except
    # clause covers everything the streaming loop itself can raise, but
    # anything raised by the two `blog.write(...)` footer calls (or a future
    # statement inserted between them and the old `blog.close()`) would still
    # skip the close and leak the handle. `try/finally` closes it on every
    # exit path unconditionally.
    blog = diag.open_build_log(run.change_request_id, utcnow().strftime("%Y%m%d-%H%M%S"))
    try:
        blog.write(f"=== UPI build+deploy — change={run.change_request_id} build_run={build_run.id} ===")
        blog.write(f"mode={mode}  host={host_label}  core_branch={core_branch}  app_branch={app_branch}")
        blog.write(f"command: {command}")
        blog.write(f"started: {utcnow().isoformat()}")
        blog.write("-" * 72)
        if blog.path:
            logger.info("Build [%s]: full log → %s", run.change_request_id[:8], blog.path)

        if mode == "local":
            stream = stream_local_command(command)
        else:
            stream = stream_remote_command(
                host=settings.phase_b_host,
                user=settings.phase_b_host_user,
                private_key_path=settings.phase_b_host_key,
                command=command,
                connect_timeout=settings.phase_b_host_connect_timeout,
            )

        try:
            # Live-log flush: the trigger endpoint returns before the script
            # finishes and the UI polls /build/latest, so the row must carry
            # the log WHILE it streams, not only at the end.
            deadline = started_at + max(60, int(settings.phase_b_script_timeout_seconds))
            last_flush = time.monotonic()
            async for kind, payload in stream:
                if kind == "exit":
                    exit_code = int(payload) if isinstance(payload, int) else -1
                    break
                line = str(payload)
                parser.feed(line)
                blog.write(line)
                logger.info("Build [%s]: %s", run.change_request_id[:8], line[:300])
                now = time.monotonic()
                if now - last_flush >= 2.0:
                    # Live flush writes ONLY the unified log — it is what the
                    # polling UI renders; the per-section columns are written
                    # once after the loop, halving the per-flush rewrite.
                    build_run.build_log = parser.unified_log()
                    db.commit()
                    last_flush = now
                if now > deadline:
                    # Abandoning the generator kills the immediate bash process
                    # (stream_local_command's finally); a hung script must not
                    # hold a RUNNING row forever.
                    msg = (f"[backend] script exceeded the "
                           f"{settings.phase_b_script_timeout_seconds}s ceiling — killed")
                    parser.feed(msg)
                    blog.write(msg)
                    await stream.aclose()
                    exit_code = -1
                    break
        except Exception as e:  # noqa: BLE001
            logger.exception("Build+deploy stream error: change=%s", run.change_request_id)
            parser.feed(f"[backend] stream error: {e}")
            blog.write(f"[backend] stream error: {e}")
            exit_code = -1

        _elapsed_s = max(0.0, time.monotonic() - started_at)
        blog.write("-" * 72)
        blog.write(f"exit_code={exit_code}  build_failure_seen={parser.saw_build_failure}  "
                   f"duration={_elapsed_s:.1f}s  finished={utcnow().isoformat()}")
    finally:
        blog.close()

    build_run.build_log = parser.unified_log()
    build_run.deploy_log = "\n".join(parser.deploy) or None
    build_run.startup_log = "\n".join(parser.startup) or None
    build_run.deployed_artifacts = parser.artifacts or None
    build_run.services_started = parser.services or None
    if parser.first_artifact_path:
        build_run.artifact_path = parser.first_artifact_path

    is_success = (exit_code == 0) and not parser.saw_build_failure
    build_run.completed_at = utcnow()
    if is_success:
        build_run.status = BuildRunStatus.SUCCESS
        # Skip the legacy standalone DEPLOY step — host script already deployed.
        run.current_step = PhaseBStep.TEST_GEN
        db.commit()
        logger.info(
            "Build+deploy success: change=%s artifacts=%d services=%d",
            run.change_request_id, len(parser.artifacts), len(parser.services),
        )
    else:
        build_run.status = BuildRunStatus.FAILURE
        db.commit()
        logger.error(
            "Build+deploy failed: change=%s exit=%d build_failure_seen=%s",
            run.change_request_id, exit_code, parser.saw_build_failure,
        )

    return build_run


# Backwards-compatible alias — anything still importing the old name keeps
# working. Internally it now runs the unified flow with default branches.
run_maven_build = run_build_and_deploy
