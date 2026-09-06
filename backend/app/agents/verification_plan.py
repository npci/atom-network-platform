# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Runtime-owned VerificationPlan + deterministic hard gates (THE BOOK §9).

The reviewer's core concern (§9.4): a free-form ``run_command`` lets the LLM pick
what "verified" means. Fix — separate diagnosis from the verdict:

* the **runtime** computes a deterministic, ordered ``VerificationPlan`` from the
  ChangeSet scope + the inter-repo build graph (§9.1/§20) + the generated-source
  lifecycle (§7.5);
* the **runtime executes it** (via the S4 PlatformAdapter) and computes the hard
  gates from ITS OWN exit codes / timeouts / parsed output (S4 maven_parser) —
  the verdict is authoritative and reproducible, never the model's say-so.

Hard gates (§9): compile == 0, required tests == 0, no timeout, required
toolchain present, required smoke present AND passed. Any fail ⇒ NOT verified.

IGW/Java-8 **soft-fail** (§9.2): a compile error whose module is in
``soft_fail_modules`` is excluded from the gate — but ONLY when that module is
untouched/out-of-scope. A change that edits IGW removes it from the effective
soft set, so its errors are real and fail the run.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.agents import workspace_local
from app.agents.maven_parser import parse_maven_output
from app.agents.platform_adapter import CommandNotAllowed, adapter
from app.core.config import settings
from app.core.domain.contract import repo_roles_of
from app.core.domain.registry import get_active_pack

logger = logging.getLogger("app.agentic")

_RE_ARTIFACT_ID = re.compile(r"<artifactId>\s*([^<]+?)\s*</artifactId>")
_SCHEMA_EXTS = (".xsd", ".xjb")


@dataclass
class VerificationStep:
    kind: str            # generate_sources | install | compile | test | smoke
    repo_id: str
    argv: list[str]
    module: str | None = None
    subdir: str | None = None   # repo-relative dir to run mvn in (the module dir)
    own_test_files: list | None = None   # test-kind steps: basenames of the CHANGE's test files


@dataclass
class StepResult:
    step: VerificationStep
    exit_code: int
    timed_out: bool
    gate_pass: bool
    hard_errors: int = 0
    soft_errors: int = 0
    output_tail: str = ""
    environment: bool = False         # failed on toolchain/deps (infra), not on the code
    # `path:line: message` lines parsed from the FULL step output at capture time —
    # output_tail is a 2K+2K display clip whose dropped middle is exactly where a long
    # reactor log keeps its file:[line,col] compile errors. Empty on old/other callers;
    # format_step_errors falls back to parsing output_tail then.
    diagnostics: list = field(default_factory=list)


@dataclass
class VerificationOutcome:
    status: str                       # "verified" | "needs_fix" | "unverified"
    gates: dict = field(default_factory=dict)
    rounds: list[StepResult] = field(default_factory=list)
    plan: list[VerificationStep] = field(default_factory=list)
    reason: str = ""                  # set for "unverified" (e.g. no local toolchain → CI)
    # Per-module build outcome for the UI's collapsible report: module → "built"
    # | "failed" | "skipped". Touched modules that compiled are "built"; ones with a
    # hard error are "failed"; ones not reached (an earlier env/timeout abort) are "skipped".
    module_results: dict = field(default_factory=dict)


def _module_label(step: VerificationStep) -> str:
    return step.module or step.subdir or step.kind


# ── Module resolution ─────────────────────────────────────────────────────────

def path_to_module(repo_dir: Path, rel_path: str) -> str | None:
    """The Maven module a changed file belongs to: the nearest ancestor dir with
    a pom.xml, named by its <artifactId> (fallback: the dir name)."""
    d = (repo_dir / rel_path).resolve().parent
    repo_dir = repo_dir.resolve()
    while True:
        pom = d / "pom.xml"
        if pom.is_file():
            try:
                m = _RE_ARTIFACT_ID.search(pom.read_text(encoding="utf-8", errors="replace"))
                return m.group(1) if m else d.name
            except OSError:
                return d.name
        if d == repo_dir or d == d.parent:
            return None
        d = d.parent


def path_to_module_dir(repo_dir: Path, rel_path: str) -> str | None:
    """Repo-relative DIR of the nearest ancestor pom.xml. We build FROM this dir
    (``cd <dir> && mvn -o install``) instead of ``-pl :artifact`` off the root
    reactor — so a repo with a broken / non-aggregating root pom (declares modules
    at paths that don't exist) still verifies. Returns '.' for the repo root."""
    repo_dir = repo_dir.resolve()
    d = (repo_dir / rel_path).resolve().parent
    while True:
        if (d / "pom.xml").is_file():
            return "." if d == repo_dir else d.relative_to(repo_dir).as_posix()
        if d == repo_dir or d == d.parent:
            return None
        d = d.parent


# ── Plan construction ─────────────────────────────────────────────────────────

def _soft_modules() -> set[str]:
    return {m.strip() for m in (settings.agentic_soft_fail_modules or "").split(",") if m.strip()}


def _skip_patterns() -> list:
    """Compiled regexes from AGENTIC_VERIFY_SKIP_MODULES. A build failure attributable to
    a matching module is excluded from the gate (unconditional, §9.2). `*` is a wildcard,
    matched case-insensitively — e.g. `*igw*,*hsm-proxy*`.

    SCR findings #4/#14 (ReDoS / Regex Injection) — this used to build the
    pattern with a bare ``spec.replace("*", ".*")``, which only substitutes the
    wildcard but leaves every OTHER regex metacharacter in the admin-supplied
    spec live (``(``, ``+``, ``{``, ``|``, ...). An admin-configurable value
    reaching ``re.compile`` unescaped is exactly the "regex injection" shape a
    scanner flags, and a crafted spec (e.g. nested quantifiers) could exhibit
    catastrophic backtracking against build-failure lines. Fix: escape the
    ENTIRE spec first (neutralising every metacharacter), then turn only the
    escaped wildcard marker back into ``.*`` — so `*igw*` still becomes
    `.*iupi.*` but `(a+)+$` becomes the literal string `\\(a\\+\\)\\+\\$`
    instead of a compilable, exploitable pattern. `re.escape` is applied to
    the wildcard character itself so the replace target matches however the
    running Python version renders it (some versions escape `*`, some don't)."""
    pats = []
    star_escaped = re.escape("*")
    for raw in (settings.agentic_verify_skip_modules or "").split(","):
        spec = raw.strip()
        if not spec:
            continue
        try:
            safe_pattern = re.escape(spec).replace(star_escaped, ".*")
            pats.append(re.compile(safe_pattern, re.IGNORECASE))
        except re.error:
            logger.warning("verify: ignoring invalid AGENTIC_VERIFY_SKIP_MODULES pattern %r", spec)
    return pats


# Java release declared in a pom: <maven.compiler.release>, .target, .source,
# <java.version>, or a bare <release> inside the compiler plugin. "1.8" → 8.
_JAVA_REL_TAGS = re.compile(
    r"<(?:maven\.compiler\.(?:release|target|source)|java\.version|release)>\s*"
    r"(?:1\.)?(\d+)\s*</", re.I)


def detect_required_java(repo_dir: Path, *, pom_cap: int = 200) -> int | None:
    """Best-effort: the highest Java release any pom in the clone declares — so the
    runtime can warn BEFORE building when the active JDK can't satisfy it. Never
    raises; returns None when nothing is declared (don't guess)."""
    best: int | None = None
    scanned = 0
    try:
        for pom in repo_dir.rglob("pom.xml"):
            if scanned >= pom_cap:
                break
            scanned += 1
            try:
                text = pom.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in _JAVA_REL_TAGS.finditer(text):
                v = int(m.group(1))
                best = v if best is None else max(best, v)
    except Exception:  # noqa: BLE001 — preflight hint only
        return best
    return best


def _root_aggregates(repo_dir: Path) -> bool:
    """True if the repo's ROOT pom is a Maven aggregator (declares <module>…), so one
    reactor ``mvn install`` from the root builds every module in dependency order."""
    root = repo_dir / "pom.xml"
    if not root.is_file():
        return False
    try:
        return "<module>" in root.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _all_module_dirs(repo_dir: Path, *, cap: int = 400) -> list[str]:
    """Repo-relative dirs of every pom.xml (shallowest first) — the non-aggregator
    fallback for a full build. Skips build-output/VCS dirs; bounded so a pathological
    tree can't explode the plan."""
    repo_dir = repo_dir.resolve()
    skip = {"target", ".git", "node_modules", "build", "dist", ".idea"}
    out: set[str] = set()
    try:
        for pom in repo_dir.rglob("pom.xml"):
            rel = pom.relative_to(repo_dir)
            if any(seg in skip for seg in rel.parts):
                continue
            d = pom.parent
            out.add("." if d == repo_dir else d.relative_to(repo_dir).as_posix())
            if len(out) >= cap:
                break
    except OSError:
        pass
    return sorted(out, key=lambda d: (0 if d == "." else d.count("/") + 1, d))


def _dir_has_schema(repo_dir: Path, rel_dir: str) -> bool:
    """True if the module dir (recursively) holds an .xsd/.xjb → needs generate-sources."""
    base = repo_dir if rel_dir == "." else repo_dir / rel_dir
    try:
        for p in base.rglob("*"):
            if p.suffix.lower() in _SCHEMA_EXTS and "target" not in p.relative_to(repo_dir).parts:
                return True
    except OSError:
        return False
    return False


def build_plan(db, run_id: str, change_set, *, app_blast_radius: bool = True
               ) -> tuple[list[VerificationStep], set[str]]:
    """Deterministic plan from a ChangeSet. Returns ``(steps, touched_modules)``.

    **Core dependency repos build FIRST, as a full reactor install.** A repo with
    ``role == "core"`` (e.g. network-core) is built from its ROOT via ``mvn -o
    -DskipTests install`` BEFORE any app module — because the dependent (network-2.0)
    resolves core's *whole* artifact set from ``~/.m2`` at build time, including
    core modules the change never touched. A per-module core build would install
    only the changed module and leave the rest absent from ``~/.m2``, so the
    network-2.0 build would then fail dependency resolution. Build the core reactor in
    full → every core module lands in ``~/.m2`` → network-2.0 resolves them. This holds
    even for an UNTOUCHED core repo that's in scope (it has no ops in the
    change-set), which is the common case for a 2.0-only change.

    App/other touched modules are then built **from their own directory** (``cd
    <module_dir> && mvn -o -DskipTests install``) — NOT via ``-pl :artifact`` off
    the root reactor — robust to a broken/non-aggregating root pom and scoped to
    what changed.
    """
    from app.models.code_repo import CodeRepo
    from app.models.agentic import AgenticRun

    by_repo: dict[str, list] = {}
    for op in change_set.operations:
        by_repo.setdefault(op.repo_id, []).append(op)

    def _repo(rid: str):
        return db.get(CodeRepo, rid) if db is not None else None

    def _role(rid: str) -> str:
        repo = _repo(rid)
        return (repo.role if repo else None) or "app"

    # All repos in scope for the run — NOT just touched ones. A 2.0-only change has
    # no ops in core, but core is still cloned and MUST be built first.
    run = db.get(AgenticRun, run_id) if db is not None else None
    selected_ids = list((run.selected_repo_ids if run else None) or by_repo.keys())

    # Core dependency repos to install (full reactor) before any app module:
    # every in-scope repo with role=="core", plus any repo named in a touched
    # repo's depends_on (honoured if populated; role is the primary signal).
    _dep_roles = {r.key for r in repo_roles_of(get_active_pack()) if r.builds_first} or {"core"}
    dep_ids: set[str] = {rid for rid in selected_ids if _role(rid) in _dep_roles}
    for rid in by_repo:
        repo = _repo(rid)
        for d in (repo.depends_on if repo and repo.depends_on else []):
            if d in selected_ids:
                dep_ids.add(d)

    def _dep_order(rid: str):
        repo = _repo(rid)
        return (len(repo.depends_on) if repo and repo.depends_on else 0, rid)

    steps: list[VerificationStep] = []
    touched: set[str] = set()

    # 1) Bootstrap: full reactor install of each core dependency repo, deps first.
    for rid in sorted(dep_ids, key=_dep_order):
        repo = _repo(rid)
        steps.append(VerificationStep(
            "install", rid, _mvn("install"),
            module=f"{(repo.label if repo else rid)} (full core build)", subdir=None))
        # Touched core modules are covered by the full build above; record them so
        # they're gated normally (not soft-failed) and don't get rebuilt per-module.
        rdir = workspace_local.repo_dir(run_id, rid)
        for op in by_repo.get(rid, []):
            md = path_to_module_dir(rdir, op.path)
            if md is not None:
                touched.add(md)

    def _order_key(rid: str):
        role = _role(rid)
        return (0 if role == "core" else 1 if role == "app" else 2, rid)

    # Does this change alter a CROSS-REPO / generated API surface? A schema edit
    # regenerates JAXB accessors (a repeating `<Limit>` → `List<Limit> getLimits()`,
    # NOT `getLimit()`), and a core-repo source edit can change a shared signature —
    # either can break a CONSUMER in an app module the agent never touched (the exact
    # `CircleProcessor.getLimit()` failure). Building only the touched modules misses
    # it (it surfaces only in a full reactor build). Scoped to the cross-repo topology
    # (a core dependency repo in scope — where schemas live in core and consumers in
    # the app repos): when so, and the operator hasn't opted out, verify every in-scope
    # app repo IN FULL (§9.1, blast-radius). A single-repo / app-local change keeps the
    # cheap touched-modules path.
    schema_changed = any(op.path.lower().endswith(_SCHEMA_EXTS)
                         for ops in by_repo.values() for op in ops)
    core_has_ops = any(rid in by_repo for rid in dep_ids)
    # Phase A (XSD) passes app_blast_radius=False: it only needs to compile the schema
    # module(s) + full-build core (which installs the regenerated XSD artifact to ~/.m2);
    # rebuilding the app consumers is Phase B's job, not the schema-review gate's.
    api_surface_changed = ((schema_changed or core_has_ops) and bool(dep_ids)
                           and app_blast_radius
                           and settings.agentic_verify_consumers_on_api_change)

    if api_surface_changed:
        # 2a) Blast radius: full build of every in-scope app repo so a consumer in an
        #     UNTOUCHED module (or an untouched repo that only CONSUMES the changed
        #     core artifact) recompiles — a broken caller now fails the gate.
        app_ids = [rid for rid in selected_ids if rid not in dep_ids]
        for rid in sorted(app_ids, key=_order_key):
            rdir = workspace_local.repo_dir(run_id, rid)
            for op in by_repo.get(rid, []):          # gate touched modules normally (§9.2)
                md = path_to_module_dir(rdir, op.path)
                if md is not None:
                    touched.add(md)
            repo = _repo(rid)
            if _root_aggregates(rdir):
                # Working aggregator root → one reactor install builds ALL modules in
                # dependency order (and runs generate-sources for any in-repo schema).
                steps.append(VerificationStep(
                    "install", rid, _mvn("install"),
                    module=f"{(repo.label if repo else rid)} (full app build)", subdir=None))
            else:
                # Non-aggregating root (build-from-dir) → build every module dir,
                # shallowest first, so we still cover all consumers.
                for md in _all_module_dirs(rdir):
                    touched.add(md)
                    if _dir_has_schema(rdir, md):
                        steps.append(VerificationStep("generate_sources", rid,
                                     _mvn("generate-sources"), module=md, subdir=md))
                    steps.append(VerificationStep("install", rid,
                                 _mvn("install"), module=md, subdir=md))
        return steps, touched

    # 2b) Cheap path (no cross-module surface change): only the touched modules. When the
    #     repo's root pom AGGREGATES, build each touched submodule WITH its in-repo
    #     dependency siblings (``-pl <module> -am`` off the root) so an intra-repo dep the
    #     change never touched (e.g. gateway-web → tcp-controller/service-mesh-common) is in
    #     ~/.m2 before the module compiles — otherwise it surfaces as a phantom "package …
    #     does not exist" the agent can only "fix" by inventing a pom dependency. A
    #     non-aggregating root keeps the broken-root-safe build-from-dir (no reactor). Core
    #     repos were already fully built above.
    for rid in sorted(by_repo, key=_order_key):
        if rid in dep_ids:
            continue
        ops = by_repo[rid]
        rdir = workspace_local.repo_dir(run_id, rid)
        aggregates = _root_aggregates(rdir)
        # changed module dir -> did an XSD/.xjb change in it (needs generate-sources)?
        mods: dict[str, bool] = {}
        for op in ops:
            md = path_to_module_dir(rdir, op.path)
            if md is None:
                continue
            mods[md] = mods.get(md, False) or op.path.lower().endswith(_SCHEMA_EXTS)
        # dependencies (shallower paths) before dependents; deterministic.
        for md in sorted(mods, key=lambda d: (d.count("/"), d)):
            touched.add(md)
            if mods[md]:
                steps.append(VerificationStep("generate_sources", rid,
                             _mvn("generate-sources"), module=md, subdir=md))
            if aggregates and md != ".":
                steps.append(VerificationStep("install", rid,
                             _mvn_module("install", md), module=md, subdir=None))
            else:
                steps.append(VerificationStep("install", rid,
                             _mvn("install"), module=md, subdir=md))
    return steps, touched


def _mvn(*goals: str) -> list[str]:
    """Maven argv for a verify step. Builds ONLINE by default (resolves from Nexus, like a
    normal `mvn clean install`): `agentic_verify_offline` is False in config AND pinned
    "false" in compose for backend+celery. Offline (`-o`) is an opt-in for an air-gapped
    host with a FULLY pre-warmed ~/.m2 only — against an incomplete cache it fails as a
    phantom `package … does not exist` compile error (reads like a code defect but isn't),
    which is what tricks the agent into inventing pom dependencies. Online instead fails as
    a dependency-resolution error, which the gate correctly classifies as environment (not
    code). Keep it off unless the offline cache is guaranteed complete."""
    argv = ["mvn"]
    if settings.agentic_verify_offline:
        argv.append("-o")
    # Reactor parallelism (Lever): builds every module still, just concurrently — keeps
    # blast-radius accuracy while cutting wall-clock. Blank setting → serial.
    threads = (getattr(settings, "agentic_verify_threads", "") or "").strip()
    if threads:
        argv += ["-T", threads]
    argv += ["-B", "-DskipTests"]
    # --fail-at-end (your --fae): keep building the rest of the reactor after a module
    # fails and report EVERY failure at the end, so the agent sees all breaks at once
    # instead of just the first. Gate still fails (non-zero exit).
    argv.append("--fail-at-end")
    # -U (your -U) forces a dependency/snapshot refresh — only meaningful ONLINE; it
    # conflicts with -o (offline), where the warmed ~/.m2 is the source of truth. So:
    # refresh when online, skip when offline.
    if not settings.agentic_verify_offline:
        argv.append("-U")
    # `clean` before an install goal → a from-scratch build that matches the
    # deployment build (`mvn clean install -DskipTests`): no stale target/ from a
    # prior verify round can mask a removed/renamed symbol. Tunable for speed.
    if getattr(settings, "agentic_verify_clean", True) and "install" in goals:
        argv.append("clean")
    argv += [*goals]
    return argv


def _mvn_module(goal: str, module: str) -> list[str]:
    """`_mvn(goal)` scoped to ONE module PLUS its in-repo dependency siblings, off the
    aggregator root: ``mvn -pl <module> -am … <goal>``. Used so a touched submodule's
    intra-repo dependencies (e.g. gateway-web → tcp-controller/service-mesh-common) are
    installed to ~/.m2 BEFORE it compiles — otherwise the sibling surfaces as a phantom
    ``package … does not exist`` the agent can only "fix" by inventing a pom dependency.
    Caller MUST confirm the root aggregates (``_root_aggregates``); ``-pl``/``-am`` need a
    parseable root reactor, so a broken/non-aggregating root keeps the build-from-dir path."""
    argv = _mvn(goal)
    return [argv[0], "-pl", module, "-am", *argv[1:]]


def _mvn_test(module: str | None, test_classes: list[str]) -> list[str]:
    """Maven argv that RUNS tests (NO -DskipTests), scoped by ``-Dtest`` to specific classes so a
    failure is unambiguously about THIS change — never an unrelated pre-existing test. When the
    root aggregates, scope to the module + its deps (``-pl <module> -am``); otherwise run from the
    module dir (via the step's ``subdir``). ``-DfailIfNoTests=false`` so a class maven can't resolve
    (renamed/moved) degrades to 'no tests ran', not a hard error. Surefire 3 gates the
    tests-exist-but-none-match-``-Dtest`` case behind a SEPARATE flag, ``failIfNoSpecifiedTests``
    (default true) — without it every upstream module ``-am`` pulls into the reactor hard-fails on
    the pattern, killing the run before the target module's tests even start."""
    argv = ["mvn"]
    if settings.agentic_verify_offline:
        argv.append("-o")
    argv.append("-B")
    if module:
        argv += ["-pl", module, "-am"]
    if not settings.agentic_verify_offline:
        argv.append("-U")
    argv += ["-Dtest=" + ",".join(test_classes), "-DfailIfNoTests=false",
             "-Dsurefire.failIfNoSpecifiedTests=false", "test"]
    return argv


def append_feature_test_steps(plan: list, run_id: str, change_set) -> None:
    """3c — append a test step per module that RUNS the change's OWN test classes (added/modified
    ``*Test.java``), so a green verdict means the behaviour RAN, not just compiled. Scoped by
    ``-Dtest`` to the change's classes → a failure is about THIS change only, never an unrelated
    pre-existing test. Gated by ``agentic_run_feature_tests`` (default off); no-op when the change
    owns no tests. Mutates ``plan`` in place, appending AFTER the install steps so classes are built
    first. run_plan maps a test failure → required_tests (needs_fix → loops back) and an
    infra/toolchain problem → environment (unverified → fail-open, never loops)."""
    if not getattr(settings, "agentic_run_feature_tests", False):
        return
    by_module: dict[tuple, list[str]] = {}
    for op in (getattr(change_set, "operations", None) or []):
        if getattr(op, "op", "") not in ("add", "modify"):
            continue
        path = getattr(op, "path", "") or ""
        if not (_is_test_path(path) and path.lower().endswith((".java", ".kt"))):
            continue
        rid = getattr(op, "repo_id", None)
        md = path_to_module_dir(workspace_local.repo_dir(run_id, rid), path)
        cls = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        by_module.setdefault((rid, md), []).append(cls)
    for (rid, md), classes in by_module.items():
        classes = sorted(set(classes))
        aggregates = _root_aggregates(workspace_local.repo_dir(run_id, rid))
        module = md if (md and md != "." and aggregates) else None
        plan.append(VerificationStep(
            "test", rid, _mvn_test(module, classes),
            module=f"{md or rid} (feature tests: {', '.join(classes)[:80]})",
            subdir=(None if module else (md or None)),
            own_test_files=[f"{c}.java" for c in classes] + [f"{c}.kt" for c in classes]))


# Maven test-compile error lines carry the SOURCE PATH of the offending file
# ("[ERROR] /…/src/test/java/…/OldTest.java:[12,5] cannot find symbol"). Surefire
# test-FAILURE lines carry class#method instead — no /src/test/ path — so this
# pattern discriminates compile-breakage from genuine test failures.
_TEST_SRC_ERR_RE = re.compile(r"\[ERROR\][^\n]*?/src/test/[^\s:\[\]]*?([A-Za-z0-9_$]+\.(?:java|kt))")


def legacy_test_compile_reason(output: str, own_test_files: list | None) -> str:
    """Non-empty reason when a feature-test step failed because LEGACY test sources
    (files this change never touched) don't compile — Maven test-compiles the whole
    ``src/test/java`` folder before running anything, so one rotten legacy test file
    kills the scoped run of the agent's own tests. That is an attribute of the REPO,
    not of this change: the caller ignores the step (fail-open, visibly labelled)
    instead of looping the code agent on errors it must not fix. An offending file
    that IS part of the change (or no parseable test-source compile error at all —
    i.e. a genuine test failure) returns "" and the normal gate applies. Never raises."""
    try:
        offenders = {m.group(1) for m in _TEST_SRC_ERR_RE.finditer(output or "")}
        if not offenders:
            return ""
        own = {str(f).rsplit("/", 1)[-1] for f in (own_test_files or [])}
        if offenders & own:
            return ""
        return ("legacy test sources fail to compile ("
                + ", ".join(sorted(offenders)[:4])
                + ") — pre-existing, not part of this change; feature-test execution "
                  "skipped for this module (fail-open)")
    except Exception:  # noqa: BLE001 — classifier must never break the gate
        return ""


# ── Execution + gate evaluation ───────────────────────────────────────────────

def _step_gate(step: VerificationStep, res, effective_soft: set[str], skip_patterns=()) -> tuple[bool, int, int, str, int | None]:
    """(gate_pass, hard_errors, soft_errors, env_reason, required_java) for one step.

    ``env_reason`` is non-empty when the failure was a build-ENVIRONMENT problem
    (JDK version mismatch, dependency resolution, repo auth) rather than a code
    defect — the runtime fails fast on those instead of driving code_change (§9)."""
    if res.timed_out:
        return False, 0, 0, "", None
    if res.exit_code == 0:
        return True, 0, 0, "", None
    # Non-zero exit: a build step passes the gate ONLY if we POSITIVELY identified
    # soft errors (untouched legacy modules) and zero hard ones. A non-zero exit
    # with NO parseable errors — a dependency-resolution failure, OOM, plugin
    # crash, network error — is a real failure and must FAIL, never silently pass.
    # Tests/smoke never soft-fail; a non-zero there is always a hard failure.
    if step.kind in ("generate_sources", "install", "compile"):
        parsed = parse_maven_output((res.stdout or "") + "\n" + (res.stderr or ""), effective_soft, skip_patterns)
        if parsed.is_environment_failure:
            return False, parsed.error_count, parsed.soft_count, _env_reason(parsed), parsed.required_java
        gate_pass = parsed.error_count == 0 and parsed.soft_count > 0
        return gate_pass, parsed.error_count, parsed.soft_count, "", None
    return False, 0, 0, "", None


def _active_jdk_major() -> int | None:
    """The JDK major the verifier would actually compile with (best-effort)."""
    from app.agents import toolchain_report
    try:
        return toolchain_report._java_major(toolchain_report._version_of("java", "-version"))
    except Exception:  # noqa: BLE001 — diagnostics only; never let this break the gate
        return None


def _env_reason(parsed) -> str:
    """Human reason for a build-environment failure — Java-version-aware. By the time
    this fires the gate already TRIED to switch to a matching installed JDK, so the
    message advises the user to INSTALL the version (we never auto-install)."""
    if parsed.required_java is not None:
        from app.agents import jdk_discovery
        active = _active_jdk_major()
        if jdk_discovery.select_jdk_home(parsed.required_java):
            return (f"build environment: this module targets Java {parsed.required_java}; a matching JDK "
                    f"IS installed but the build didn't use it — check JAVA_HOME / the system alternatives")
        return (f"build environment: this module targets Java {parsed.required_java}, which is NOT installed "
                f"on the build host (active JDK is {active}). Please install JDK {parsed.required_java}, then "
                f"resume — this is a toolchain gap, not a code defect")
    first = parsed.environment_errors[0] if parsed.environment_errors else "dependency resolution failed"
    return ("build environment: " + first + " — warm the offline ~/.m2 cache / fix repository "
            "credentials (this is not a code defect)")


def _diagnostic_lines(full: str, step: VerificationStep, *, limit: int = 25) -> list[str]:
    """``path:line: message`` lines parsed from a step's FULL output at capture time —
    run before the head+tail display clip, whose dropped middle is where a long
    ``--fail-at-end`` reactor log keeps its inline compiler errors."""
    parsed = parse_maven_output(full or "")
    out: list[str] = []
    # An environment/toolchain failure (JDK/deps/auth) can co-occur with inline compiler
    # diagnostics in a single --fail-at-end reactor log. format_step_errors returns these
    # captured diagnostics verbatim BEFORE its own environment-error labeling branch, so
    # unless we lead with the infra marker here the code agent reads an env failure as a code
    # defect and burns fix rounds editing code for it — the exact "infra must never read as a
    # code defect" invariant this module enforces. Prepend the "not a code defect" reason.
    if parsed.environment_errors:
        out.append(_env_reason(parsed))
    for d in parsed.diagnostics:
        if d.severity not in ("error", "soft"):
            continue
        loc = f"{d.file}:{d.line}" if d.file and d.line else (d.module or step.kind)
        out.append(f"{loc}: {d.message}")
        if len(out) >= limit:
            break
    return out[:limit]


def format_step_errors(sr: StepResult, *, limit: int = 25) -> list[str]:
    """Parse ONE failing step's output into ``path:line: message`` diagnostics."""
    # A timeout is a wall-clock condition, not a diagnostic. Without this branch it
    # surfaced as "failed (exit N)" — or, since a killed step has exit_code None, as an
    # EMPTY error list ("Verification failed — 0 error(s)") — and the code agent spent
    # fix rounds editing code to fix time. Same addressee rule as the environment
    # branch: infra conditions must never read as code defects.
    if sr.timed_out:
        argv = f": $ {' '.join(sr.step.argv)}" if sr.step.argv else ""
        return [f"{sr.step.kind} TIMED OUT after the step budget (a wall-clock/infra "
                f"condition, NOT a code defect){argv} — do not edit code for this; the "
                "step needs a narrower scope or a bigger timeout (operator setting)"]
    # Diagnostics captured from the FULL output (run_plan) win over re-parsing the
    # truncated display tail below — the clip drops the middle of long reactor logs.
    if sr.diagnostics:
        return list(sr.diagnostics)[:limit]
    out: list[str] = []
    parsed = parse_maven_output(sr.output_tail or "")
    for d in parsed.diagnostics:
        if d.severity not in ("error", "soft"):
            continue
        loc = f"{d.file}:{d.line}" if d.file and d.line else (d.module or sr.step.kind)
        out.append(f"{loc}: {d.message}")
        if len(out) >= limit:
            return out
    # Environment failure (JDK/deps/auth) — surface the recorded signature lines.
    for e in parsed.environment_errors:
        out.append(f"{sr.step.module or sr.step.kind}: {e}")
        if len(out) >= limit:
            return out
    # A non-zero step with no parseable diagnostic (plugin failure/network/OOM). The raw
    # tail alone is Maven's boilerplate footer — worse than useless: the ONE actionable
    # sentence is the mojo-failure line ("Failed to execute goal …: <reason>") a few
    # hundred chars earlier, and without the exact argv the agent reproduces with the
    # obvious commands (plain `mvn test`), which can all pass while the gate's scoped
    # invocation fails. Surface command + mojo line(s) first, tail last.
    if not parsed.diagnostics and not parsed.environment_errors:
        tail = (sr.output_tail or "").strip()
        if sr.exit_code is None:
            # No exit code and NOT a timeout (short-circuited above): the step runner
            # itself failed (killed process / toolchain guard refusal). Infra — without
            # this the list stayed empty and the verdict read "failed — 0 error(s)".
            if tail:
                out.append(f"{sr.step.kind} produced no exit code — the step runner failed "
                           f"(infra/toolchain, not a code defect): {tail[-300:]}")
        elif sr.exit_code != 0 and tail:
            if sr.step.argv:
                out.append(f"{sr.step.kind} command: $ {' '.join(sr.step.argv)}")
            for line in tail.splitlines():
                if line.startswith("[ERROR] Failed to execute goal") and len(out) < limit:
                    out.append(line)
            out.append(f"{sr.step.kind} failed (exit {sr.exit_code}): {tail[-300:]}")
    return out[:limit]


def format_errors(outcome: VerificationOutcome, *, limit: int = 25) -> list[str]:
    """Parse the failing steps' output into ``path:line: message`` diagnostics so the
    agent (and the retry feedback) get ACTIONABLE errors, not just a gates dict.
    Reuses the deterministic ``maven_parser``; safe on any backend (empty if none)."""
    out: list[str] = []
    seen: set[str] = set()
    for sr in outcome.rounds:
        if sr.gate_pass:
            continue
        for line in format_step_errors(sr, limit=limit):
            if line not in seen:
                seen.add(line)
                out.append(line)
            if len(out) >= limit:
                return out
    return out[:limit]


# ── Feature-test gate (WS3a) ──────────────────────────────────────────────────────────────────────
# "No tests" must NOT count as a pass for a behavioural change. A change that edits business logic
# (a validator / service / controller / handler / …) but adds no test file fails the `feature_tests`
# gate → the run loops back to the code agent to write one (self-heals). Pure XSD / doc / config / DTO
# changes are EXEMPT (no behavioural source touched → nothing to feature-test here). Deterministic.
_BEHAVIOURAL_KEYWORDS = ("validator", "service", "controller", "handler", "listener", "assembler",
                         "manager", "processor", "engine", "interactor", "usecase")
_NON_LOGIC_SUFFIXES = ("dto", "entity", "constants", "config", "properties", "model", "bean",
                       "request", "response", "pojo", "enum", "exception")


def _is_behavioural_src(path: str) -> bool:
    p = (path or "").replace("\\", "/")
    if "/src/main/" not in p:
        return False
    base = p.rsplit("/", 1)[-1]
    if not base.lower().endswith((".java", ".kt")):
        return False
    stem = base.rsplit(".", 1)[0].lower()
    if any(stem.endswith(s) for s in _NON_LOGIC_SUFFIXES):
        return False
    return any(k in stem for k in _BEHAVIOURAL_KEYWORDS)


def _is_test_path(path: str) -> bool:
    p = (path or "").replace("\\", "/")
    base = p.rsplit("/", 1)[-1].lower()
    return "/src/test/" in p or base.endswith(("test.java", "tests.java", "it.java", "test.kt", "tests.kt"))


def feature_test_gate(change_set) -> tuple[bool, str]:
    """Deterministic 'real feature tests' gate. Returns ``(passed, reason)``. Passes (exempt) when no
    behavioural source changed; fails when a behavioural change adds no test (add OR modify)."""
    ops = getattr(change_set, "operations", None) or []
    live = [o for o in ops if getattr(o, "op", "") in ("add", "modify")]
    behavioural = [o for o in live if _is_behavioural_src(getattr(o, "path", "") or "")]
    if not behavioural:
        return True, "no behavioural source changed — feature test not required"
    if any(_is_test_path(getattr(o, "path", "") or "") for o in live):
        return True, "behavioural change includes a test"
    files = ", ".join(sorted({(getattr(o, "path", "") or "").rsplit("/", 1)[-1] for o in behavioural})[:5])
    return False, f"behavioural source changed with no test added ({files})"


def run_plan(db, run_id: str, plan: list[VerificationStep], *, touched_modules: set[str],
             soft_fail_modules: set[str] | None = None, smoke_required: bool = False,
             feature_tests_ok: bool | None = None, executor=adapter) -> VerificationOutcome:
    """Execute the plan and compute the hard gates from the runtime's own results."""
    configured = _soft_modules() if soft_fail_modules is None else set(soft_fail_modules)
    effective_soft = configured - set(touched_modules)   # touched soft modules are gated normally (§9.2)
    skip_patterns = _skip_patterns()                     # AGENTIC_VERIFY_SKIP_MODULES — always excluded

    gates = {"compile": True, "required_tests": True, "smoke": True, "toolchain": True,
             "timeout": True, "environment": True}
    if feature_tests_ok is not None:                  # WS3a — "no tests" is not a pass for behavioural changes
        gates["feature_tests"] = bool(feature_tests_ok)
    smoke_passed = False
    rounds: list[StepResult] = []
    # Per-module report (Built / Failed / Skipped). Every planned module starts
    # "skipped"; it flips to "built" when a step passes and "failed" on a hard error.
    module_results: dict[str, dict] = {}
    for s in plan:
        module_results.setdefault(_module_label(s), {"status": "skipped", "errors": []})
    failed_modules: set[str] = set()      # skip the rest of a module once one of its steps fails
    compile_failed = False                 # a build step failed → tests can't run (no classes)
    env_reason = ""

    # JDK switching (Java-version awareness): build each repo with the JDK its poms
    # target, if a matching one is installed. e.g. a Java-25 repo on a box whose
    # default is 17 → build with the installed JDK 25 instead of failing on
    # "invalid target release: 25". Best-effort + cached per repo; falls back to the
    # active JDK (then the env-classifier reports "install JDK N" if that fails).
    _repo_jdk: dict[str, dict | None] = {}

    def _jdk_env_for(rid: str) -> dict | None:
        if rid in _repo_jdk:
            return _repo_jdk[rid]
        ov = None
        try:
            from app.agents import jdk_discovery
            required = detect_required_java(workspace_local.repo_dir(run_id, rid))
            home = jdk_discovery.select_jdk_home(required)
            if required and home:
                ov = {"JAVA_HOME": home}
                logger.info("verify: run=%s repo=%s build JDK → %d (%s)", run_id, rid, required, home)
            elif required:
                logger.info("verify: run=%s repo=%s targets JDK %d but none installed — "
                            "build will use the active JDK", run_id, rid, required)
        except Exception:  # noqa: BLE001 — switching is best-effort
            ov = None
        _repo_jdk[rid] = ov
        return ov

    logger.info("verify: run=%s executing %d step(s) touched_modules=%s", run_id, len(plan), sorted(touched_modules))
    # Dedicated, findable verify build log: the FULL mvn output per step lands in
    # logs/diagnostics/verify/<run_id>.log (the DB row keeps only a 4 KB head+tail,
    # and the UI shows only the Built/Failed summary). Fail-open + secret-redacted.
    try:
        from app.core import diag
        vlog = diag.open_verify_log(run_id)
        vlog.write("=" * 72)
        vlog.write(f"VERIFY run={run_id} · {len(plan)} step(s) · touched={sorted(touched_modules)}")
    except Exception:  # noqa: BLE001 — logging must never break the gate
        vlog = None
    # SCR finding #10 (Improper Resource Shutdown or Release) -- vlog.close()
    # used to run only after this whole block completed normally. Any
    # exception raised inside it that is not already caught by one of its
    # own try/excepts would propagate out of run_plan and skip the close,
    # leaking the open verify-log handle for the rest of the worker
    # process life. try/finally guarantees the close always happens.
    try:
        for i, step in enumerate(plan):
            label = _module_label(step)
            # A module whose earlier step already failed: don't run its remaining steps
            # (e.g. `install` after `generate_sources` failed) — but DO go on to the next
            # module so its build still happens and shows in the report.
            if label in failed_modules:
                continue
            # Tests/smoke need compiled classes; once any build step failed they'd only
            # produce spurious failures, so skip them (the verdict is already sealed).
            if step.kind in ("test", "smoke") and compile_failed:
                continue
            work = workspace_local.repo_dir(run_id, step.repo_id)
            if step.subdir and step.subdir not in (".", ""):
                work = work / step.subdir          # build FROM the module dir (broken-root-safe)
            logger.info("verify: run=%s step %d/%d kind=%s module=%s → %s",
                        run_id, i + 1, len(plan), step.kind, step.module, " ".join(step.argv))
            try:
                _ov = _jdk_env_for(step.repo_id)
                res = (executor.run_command(work, step.argv, env_overrides=_ov)
                       if _ov else executor.run_command(work, step.argv))
            except CommandNotAllowed as e:
                logger.warning("verify: run=%s step %d BLOCKED by toolchain guard: %s", run_id, i + 1, e)
                gates["toolchain"] = False
                sr = StepResult(step, exit_code=-1, timed_out=False, gate_pass=False, output_tail=str(e))
                rounds.append(sr)
                _persist(db, run_id, i, sr, None)
                module_results[label] = {"status": "failed", "errors": [str(e)[:200]]}
                break                              # toolchain is global — the rest can't run

            logger.info("verify: run=%s step %d/%d kind=%s exit=%s timed_out=%s duration_ms=%s",
                        run_id, i + 1, len(plan), step.kind, res.exit_code, res.timed_out,
                        getattr(res, "duration_ms", "?"))
            if vlog is not None:
                try:
                    vlog.write(f"\n=== step {i + 1}/{len(plan)} · {step.kind} · "
                               f"{step.module or step.subdir or '.'} (cwd={work}) ===")
                    vlog.write("$ " + " ".join(step.argv))
                    vlog.write(f"[exit={res.exit_code} timed_out={res.timed_out} "
                               f"duration_ms={getattr(res, 'duration_ms', '?')}]")
                    if (res.stdout or "").strip():
                        vlog.write(res.stdout)
                    if (res.stderr or "").strip():
                        vlog.write("--- stderr ---")
                        vlog.write(res.stderr)
                except Exception:  # noqa: BLE001
                    pass
            if res.timed_out:
                logger.warning("verify: run=%s step %d/%d kind=%s TIMED OUT", run_id, i + 1, len(plan), step.kind)
                gates["timeout"] = False
            gate_pass, hard, soft, step_env_reason, _req_java = _step_gate(step, res, effective_soft, skip_patterns)
            if step.kind in ("generate_sources", "install", "compile") and not gate_pass:
                gates["compile"] = False
            elif step.kind == "test" and not gate_pass:
                _legacy = ""
                if getattr(settings, "agentic_legacy_test_compile_failopen", True):
                    _legacy = legacy_test_compile_reason(
                        (res.stdout or "") + (res.stderr or ""), step.own_test_files)
                if _legacy:
                    # The repo's own broken test folder, not this change: ignore the step
                    # (user policy: legacy tests are not the agent's problem) but say so
                    # loudly in the module report — an unexecuted test is never a silent green.
                    gate_pass = True
                    module_results.setdefault(_module_label(step), {"status": "skipped", "errors": []})[
                        "errors"].append("feature tests SKIPPED — " + _legacy)
                    logger.warning("verify: run=%s %s", run_id, _legacy)
                else:
                    gates["required_tests"] = False
            elif step.kind == "smoke":
                smoke_passed = res.ok
                if not res.ok:
                    gates["smoke"] = False

            full = (res.stdout or "") + (res.stderr or "")
            # Head+tail (§21): the failing reason can be at the top (dependency error)
            # or the bottom (compile summary); keep both ends. Diagnostics are parsed from
            # the FULL output first (failing steps only) — the clip is display-only.
            tail = full if len(full) <= 4000 else full[:2000] + "\n…[truncated]…\n" + full[-2000:]
            sr = StepResult(step, res.exit_code, res.timed_out, gate_pass, hard, soft, tail,
                            environment=bool(step_env_reason),
                            diagnostics=([] if gate_pass else _diagnostic_lines(full, step)))
            rounds.append(sr)
            _persist(db, run_id, i, sr, None)

            if gate_pass:
                # Don't overwrite a module already marked failed/skipped-after-fail.
                if module_results.get(label, {}).get("status") != "failed":
                    module_results[label] = {"status": "built", "errors": []}
            else:
                module_results[label] = {"status": "failed",
                                         "errors": format_step_errors(sr)[:8]}
                failed_modules.add(label)
                if step.kind in ("generate_sources", "install", "compile"):
                    compile_failed = True

            # Environment failure (JDK/deps/auth) is infra, not code — and global: the
            # rest of the plan would fail the same way. Seal it and stop; remaining
            # modules stay "skipped" in the report.
            if step_env_reason and not env_reason:
                env_reason = step_env_reason
                gates["environment"] = False
                break

        if smoke_required and not smoke_passed:
            gates["smoke"] = False

        if not gates["environment"]:
            status = "unverified"            # fail fast — never loop code_change on infra (§9)
        elif all(gates.values()):
            status = "verified"
        else:
            status = "needs_fix"
        failed_gates = [g for g, ok in gates.items() if not ok]
        logger.info("verify: run=%s VERDICT=%s ran=%d/%d gates=%s modules=%s%s",
                    run_id, status, len(rounds), len(plan), gates,
                    {k: v["status"] for k, v in module_results.items()},
                    f" failed={failed_gates}" if failed_gates else "")
        _persist_summary(db, run_id, len(plan), status, gates, plan)
        if vlog is not None:
            try:
                vlog.write("-" * 72)
                vlog.write(f"VERDICT={status} gates={gates}"
                           + (f" failed={failed_gates}" if failed_gates else ""))
            except Exception:  # noqa: BLE001
                pass
    finally:
        if vlog is not None:
            try:
                vlog.close()
            except Exception:  # noqa: BLE001
                pass
    return VerificationOutcome(status=status, gates=gates, rounds=rounds, plan=plan,
                               reason=env_reason, module_results=module_results)


def _persist(db, run_id: str, round_i: int, sr: StepResult, reasoning: str | None) -> None:
    if db is None:
        return
    from app.models.agentic import VerificationRun
    db.add(VerificationRun(
        run_id=run_id, round=round_i, exit_code=sr.exit_code, timed_out=sr.timed_out,
        decision="pass" if sr.gate_pass else "fail", raw_output=sr.output_tail,
        smoke_passed=(sr.gate_pass if sr.step.kind == "smoke" else None),
        plan={"kind": sr.step.kind, "argv": sr.step.argv, "module": sr.step.module},
    ))
    db.flush()


def _persist_summary(db, run_id: str, round_i: int, status: str, gates: dict, plan) -> None:
    if db is None:
        return
    from app.models.agentic import VerificationRun
    db.add(VerificationRun(
        run_id=run_id, round=round_i, decision=status, gates=gates,
        plan={"steps": [{"kind": s.kind, "argv": s.argv, "module": s.module} for s in plan]},
    ))
    db.flush()
