# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Verification hard gates + plan (§9). Gate logic uses a fake executor (no mvn)."""
from types import SimpleNamespace

import pytest

from app.agents.platform_adapter import CommandResult, CommandNotAllowed
from app.agents import verification_plan as V
from app.agents.verification_plan import VerificationStep, run_plan, path_to_module
from app.core.config import settings as _settings

RUN = "run-1"


@pytest.fixture(autouse=True)
def _hermetic_verify_settings(monkeypatch):
    # The dev container sets AGENTIC_VERIFY_SKIP_MODULES (e.g. *igw*,*hsm-proxy*) which would
    # otherwise leak in and unconditionally skip IGW's errors — masking the touched-IGW gate. Reset
    # it so these tests exercise the soft-fail/touched logic hermetically, independent of the env.
    monkeypatch.setattr(_settings, "agentic_verify_skip_modules", "")
    # The build-plan tests model UPI's two-repo topology (core builds before
    # app), and build_plan reads repo_roles from the ACTIVE pack at call time —
    # pin the pack these fixtures describe rather than inheriting the shell's
    # (the F-1 shape: red on any NLLN-configured host).
    from pathlib import Path

    from app.agents import verification_plan as _vp
    from app.core.domain import registry as _registry

    monkeypatch.setenv("DOMAIN_PACK", str(
        Path(_vp.__file__).resolve().parents[1] / "packs" / "network" / "network.yaml"))
    _registry._load.cache_clear()
    yield
    _registry._load.cache_clear()


def _res(exit_code=0, stdout="", stderr="", timed_out=False):
    return CommandResult(argv=["mvn"], exit_code=exit_code, stdout=stdout, stderr=stderr,
                         timed_out=timed_out, duration_ms=1)


class _Exec:
    """Fake PlatformAdapter: maps a step (by argv keyword) to a CommandResult."""
    def __init__(self, responder):
        self.responder = responder

    def run_command(self, cwd, argv, timeout_s=None):
        return self.responder(argv)


def _verdict(steps, responder, **kw):
    return run_plan(None, RUN, steps, executor=_Exec(responder), **kw)


_COMPILE = VerificationStep("compile", "r1", ["mvn", "-o", "compile"], "refund-svc")
_TEST = VerificationStep("test", "r1", ["mvn", "-o", "test"])
_SMOKE = VerificationStep("smoke", "r1", ["mvn", "-o", "verify"])

_IGW_FAIL = ("[INFO] Building IGW 1.0\n"
              "[INFO] --- maven-compiler-plugin:3.11:compile (default) @ IGW ---\n"
              "[ERROR] /ws/IGW/src/Bar.java:[5,1] incompatible types\n"
              "[INFO] BUILD FAILURE\n")
_REAL_FAIL = ("[INFO] --- maven-compiler-plugin:3.11:compile (default) @ refund-svc ---\n"
              "[ERROR] /ws/refund-svc/src/Foo.java:[3,9] cannot find symbol\n"
              "[INFO] BUILD FAILURE\n")


def test_all_steps_pass_is_verified():
    out = _verdict([_COMPILE, _TEST], lambda a: _res(0), touched_modules={"refund-svc"})
    assert out.status == "verified" and all(out.gates.values())


def test_compile_hard_error_fails_the_run():
    out = _verdict([_COMPILE], lambda a: _res(1, stdout=_REAL_FAIL), touched_modules={"refund-svc"})
    assert out.status == "needs_fix" and out.gates["compile"] is False


def test_untouched_iupi_soft_error_does_not_fail():
    # IGW is configured soft and NOT touched → its compile error is excluded.
    out = _verdict([_COMPILE], lambda a: _res(1, stdout=_IGW_FAIL),
                   touched_modules={"refund-svc"}, soft_fail_modules={"IGW"})
    assert out.status == "verified" and out.gates["compile"] is True


def test_touched_iupi_is_gated_normally():
    # Same error, but the change EDITS IGW → it leaves the effective soft set → fails.
    out = _verdict([_COMPILE], lambda a: _res(1, stdout=_IGW_FAIL),
                   touched_modules={"IGW"}, soft_fail_modules={"IGW"})
    assert out.status == "needs_fix" and out.gates["compile"] is False


def test_nonzero_compile_with_no_parseable_errors_fails():
    # A build that fails for an UNRECOGNISED non-compiler reason (OOM, plugin crash)
    # exits non-zero with no [ERROR] file:[L,C] diagnostics and no known infra
    # signature. That must FAIL the gate — never silently pass.
    out = _verdict([_COMPILE],
                   lambda a: _res(1, stderr="java.lang.OutOfMemoryError: Java heap space"),
                   touched_modules={"refund-svc"})
    assert out.gates["compile"] is False and out.status == "needs_fix"


def test_dependency_resolution_failure_is_environment_not_code():
    # An infra failure (offline-cache miss / nexus 401 / unresolved deps) is NOT a
    # code defect — classify it as 'unverified' so the run fails fast instead of
    # looping code_change on a problem the agent can't fix by editing source.
    out = _verdict([_COMPILE],
                   lambda a: _res(1, stderr="[ERROR] Failed: Could not resolve dependencies for refund-svc"),
                   touched_modules={"refund-svc"})
    assert out.gates["environment"] is False and out.status == "unverified"
    assert "environment" in out.reason.lower()


def test_java_version_mismatch_is_environment_with_required_version():
    # mvn aborts with "invalid target release: 25" when the active JDK is too old.
    # The agent must NOT treat this as a code bug — it's a toolchain problem.
    out = _verdict([_COMPILE],
                   lambda a: _res(1, stderr="[ERROR] Fatal error compiling: error: invalid target release: 25"),
                   touched_modules={"refund-svc"})
    assert out.gates["environment"] is False and out.status == "unverified"
    assert "Java 25" in out.reason


def test_module_report_built_failed_skipped():
    # Two changed modules: A fails to compile, B compiles clean. Both must be
    # ATTEMPTED (B isn't skipped just because A failed) and reported per-module.
    a = VerificationStep("install", "r1", ["mvn", "-o", "install"], "mod-a", subdir="mod-a")
    b = VerificationStep("install", "r1", ["mvn", "-o", "install"], "mod-b", subdir="mod-b")
    fail_a = ("[INFO] --- maven-compiler-plugin:3.11:compile (default) @ mod-a ---\n"
              "[ERROR] /ws/mod-a/src/Foo.java:[3,9] cannot find symbol\n[INFO] BUILD FAILURE\n")
    # Per-step responder: mod-a dir fails (first call), mod-b dir passes (second).
    calls = []

    def responder(argv):
        calls.append(argv)
        # both steps share argv; distinguish by call order: first=mod-a, second=mod-b
        return _res(1, stdout=fail_a) if len(calls) == 1 else _res(0)
    out = run_plan(None, RUN, [a, b], executor=_Exec(responder), touched_modules={"mod-a", "mod-b"})
    assert out.status == "needs_fix"
    assert out.module_results["mod-a"]["status"] == "failed"
    assert out.module_results["mod-b"]["status"] == "built", "B must build even though A failed"
    assert len(calls) == 2, "both modules attempted (no short-circuit across modules)"


def test_short_circuit_skips_steps_after_a_gating_failure():
    calls = []

    def responder(argv):
        calls.append(argv)
        return _res(1, stdout=_REAL_FAIL) if "compile" in argv else _res(0)
    out = run_plan(None, RUN, [_COMPILE, _TEST], executor=_Exec(responder),
                   touched_modules={"refund-svc"})
    assert out.status == "needs_fix"
    assert not any("test" in c for c in calls), "test must be skipped after compile fails"


def test_test_failure_fails_required_tests_gate():
    out = _verdict([_COMPILE, _TEST], lambda a: _res(0) if "compile" in a else _res(1),
                   touched_modules={"refund-svc"})
    assert out.gates["required_tests"] is False and out.status == "needs_fix"


def test_timeout_fails_the_timeout_gate():
    out = _verdict([_COMPILE], lambda a: _res(-1, timed_out=True), touched_modules={"refund-svc"})
    assert out.gates["timeout"] is False and out.status == "needs_fix"


def test_missing_toolchain_fails():
    def boom(argv):
        raise CommandNotAllowed("mvn not resolved")
    out = _verdict([_COMPILE], boom, touched_modules={"refund-svc"})
    assert out.gates["toolchain"] is False and out.status == "needs_fix"


def test_smoke_required_but_absent_fails():
    out = _verdict([_COMPILE], lambda a: _res(0), touched_modules={"refund-svc"}, smoke_required=True)
    assert out.gates["smoke"] is False


def test_smoke_present_and_passing():
    out = _verdict([_COMPILE, _SMOKE], lambda a: _res(0), touched_modules={"refund-svc"},
                   smoke_required=True)
    assert out.gates["smoke"] is True and out.status == "verified"


def test_path_to_module_reads_artifact_id(tmp_path):
    (tmp_path / "mod").mkdir()
    (tmp_path / "mod" / "pom.xml").write_text("<project><artifactId>refund-svc</artifactId></project>")
    (tmp_path / "mod" / "src").mkdir()
    (tmp_path / "mod" / "src" / "A.java").write_text("class A{}")
    assert path_to_module(tmp_path, "mod/src/A.java") == "refund-svc"
    assert path_to_module(tmp_path, "nopom/x.java") is None


def test_path_to_module_dir(tmp_path):
    from app.agents.verification_plan import path_to_module_dir
    (tmp_path / "network-parent" / "api-gateway" / "src").mkdir(parents=True)
    (tmp_path / "network-parent" / "api-gateway" / "pom.xml").write_text("<project/>")
    assert path_to_module_dir(tmp_path, "network-parent/api-gateway/src/A.java") == "network-parent/api-gateway"
    assert path_to_module_dir(tmp_path, "nopom/x.java") is None


def test_build_plan_builds_each_module_from_its_own_dir(tmp_path, monkeypatch):
    """Broken-root-safe: every step runs OFFLINE from the module's own directory
    (subdir), never `-pl :artifact` off the (possibly broken) root reactor."""
    from types import SimpleNamespace
    from app.core.config import settings
    from app.agents import verification_plan as VP
    from app.agents.agentic_tools import FileOp

    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    run, rid = "run1", "r1"
    rdir = tmp_path / run / rid
    for d in ("network-dependencies/network-domain-xsd", "network-parent/api-gateway"):
        (rdir / d / "src").mkdir(parents=True)
        (rdir / d / "pom.xml").write_text("<project/>")
    ops = [
        FileOp("modify", rid, "network-parent/api-gateway/src/A.java", "x", None),
        FileOp("add", rid, "network-dependencies/network-domain-xsd/src/main/resources/R.xsd", "<xsd/>", None),
    ]
    steps, _touched = VP.build_plan(None, run, SimpleNamespace(operations=ops))

    installs = {s.subdir for s in steps if s.kind == "install"}
    assert installs == {"network-dependencies/network-domain-xsd", "network-parent/api-gateway"}
    assert all("-pl" not in s.argv for s in steps)              # never the broken root reactor
    assert all(s.argv[0] == "mvn" for s in steps)               # mvn from the module dir (the -o/offline flag is config-driven)
    assert any(s.kind == "generate_sources" and s.subdir == "network-dependencies/network-domain-xsd" for s in steps)


def test_build_plan_aggregator_root_builds_module_with_also_make(tmp_path, monkeypatch):
    """Intra-repo dependency fix: when the app repo's root pom AGGREGATES (declares
    <module>), the touched submodule is built off the root with `-pl <module> -am` so its
    in-repo sibling dependencies land in ~/.m2 first — preventing a phantom
    'package … does not exist' for a sibling the change never touched (which the agent can
    only "fix" by inventing a pom dependency). The non-aggregating-root case keeps the
    broken-root-safe build-from-dir path (see test_build_plan_builds_each_module_from_its_own_dir)."""
    from types import SimpleNamespace
    from app.core.config import settings
    from app.agents import verification_plan as VP
    from app.agents.agentic_tools import FileOp
    from app.models.agentic import AgenticRun

    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    run, app_id = "run1", "network-20"
    rdir = tmp_path / run / app_id
    # aggregator root (declares modules) + touched submodule + an untouched sibling dep
    for mod in ("gateway-web", "tcp-controller"):
        (rdir / mod / "src").mkdir(parents=True)
        (rdir / mod / "pom.xml").write_text("<project/>")
    (rdir / "pom.xml").write_text(
        "<project><module>gateway-web</module><module>tcp-controller</module></project>")
    ops = [FileOp("modify", app_id, "gateway-web/src/A.java", "x", None)]

    class _DB:
        def get(self, model, key):
            if model is AgenticRun:
                return SimpleNamespace(selected_repo_ids=[app_id])
            return SimpleNamespace(role="app", depends_on=None, label=key)

    steps, touched = VP.build_plan(_DB(), run, SimpleNamespace(operations=ops))
    installs = [s for s in steps if s.kind == "install"]
    assert len(installs) == 1
    s = installs[0]
    assert s.subdir is None, "built off the aggregator root, not the module's own dir"
    assert "-am" in s.argv and "-pl" in s.argv
    assert s.argv[s.argv.index("-pl") + 1] == "gateway-web", "-pl scopes to the touched module"
    assert "gateway-web" in touched


def test_build_plan_two_repos_core_builds_before_app(tmp_path, monkeypatch):
    """The production topology: network-core (framework + XSDs, role='core') must be
    built — mvn install into the shared ~/.m2 — BEFORE network-2.0 (business logic,
    role='app') compiles against its artifacts. Repo `role` drives the order,
    regardless of repo-id sort order."""
    from types import SimpleNamespace
    from app.core.config import settings
    from app.agents import verification_plan as VP
    from app.agents.agentic_tools import FileOp

    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    run = "run1"
    # repo ids chosen so naive id-sorting would build the APP first.
    core_id, app_id = "z-network-core", "a-network-20"
    for rid, mod in ((core_id, "domain-xsd"), (app_id, "txn-service")):
        d = tmp_path / run / rid / mod
        (d / "src").mkdir(parents=True)
        (d / "pom.xml").write_text("<project/>")
    ops = [
        FileOp("modify", app_id, "txn-service/src/A.java", "x", None),
        FileOp("add", core_id, "domain-xsd/src/main/resources/R.xsd", "<xsd/>", None),
    ]

    from app.models.agentic import AgenticRun

    class _DB:
        def get(self, model, key):
            if model is AgenticRun:
                return SimpleNamespace(selected_repo_ids=[core_id, app_id])
            return SimpleNamespace(role="core" if key == core_id else "app", depends_on=None, label=key)

    steps, touched = VP.build_plan(_DB(), run, SimpleNamespace(operations=ops))
    repo_order = [s.repo_id for s in steps]
    # Every core step precedes every app step → core's install hits ~/.m2 first.
    assert repo_order.index(core_id) < repo_order.index(app_id)
    assert max(i for i, r in enumerate(repo_order) if r == core_id) \
        < min(i for i, r in enumerate(repo_order) if r == app_id)
    # Core is built as a FULL reactor (from its root, subdir=None) so EVERY core
    # module — not just the changed one — lands in ~/.m2 for network-2.0 to resolve.
    core_steps = [s for s in steps if s.repo_id == core_id]
    assert core_steps and all(s.subdir is None for s in core_steps)


def test_build_plan_untouched_core_still_full_builds_for_app_change(tmp_path, monkeypatch):
    """The reported bug: a network-2.0-ONLY change has no ops in network-core, yet network-core
    must still be built (full reactor → ~/.m2) FIRST, or network-2.0 fails to resolve
    core artifacts it didn't itself change. Core is in scope (selected) but untouched."""
    from types import SimpleNamespace
    from app.core.config import settings
    from app.agents import verification_plan as VP
    from app.agents.agentic_tools import FileOp
    from app.models.agentic import AgenticRun

    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    run = "run1"
    core_id, app_id = "network-core", "network-20"
    d = tmp_path / run / app_id / "txn-service"
    (d / "src").mkdir(parents=True)
    (d / "pom.xml").write_text("<project/>")
    # ONLY a network-2.0 file changes — nothing in core.
    ops = [FileOp("modify", app_id, "txn-service/src/A.java", "x", None)]

    class _DB:
        def get(self, model, key):
            if model is AgenticRun:
                return SimpleNamespace(selected_repo_ids=[core_id, app_id])
            return SimpleNamespace(role="core" if key == core_id else "app", depends_on=None, label=key)

    steps, _touched = VP.build_plan(_DB(), run, SimpleNamespace(operations=ops))
    repo_order = [s.repo_id for s in steps]
    assert core_id in repo_order, "untouched core repo must still be built"
    # full core build precedes the app module build
    assert max(i for i, r in enumerate(repo_order) if r == core_id) \
        < min(i for i, r in enumerate(repo_order) if r == app_id)
    # Untouched core → a single full reactor install from the root (no per-module
    # steps, no separate generate-sources: `mvn install` runs the full lifecycle).
    core_steps = [s for s in steps if s.repo_id == core_id]
    assert [s.kind for s in core_steps] == ["install"]
    assert all(s.subdir is None for s in core_steps)
    assert "txn-service" in _touched


def test_build_plan_without_declared_roles_keeps_core_dependency_default(tmp_path, monkeypatch):
    """No topology declaration keeps the historical role='core' bootstrap."""
    from app.agents import verification_plan as VP
    from app.agents.agentic_tools import FileOp
    from app.core.config import settings
    from app.models.agentic import AgenticRun

    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    monkeypatch.setattr(VP, "get_active_pack", lambda: object())
    run, core_id, app_id = "run1", "shared", "service"
    app_dir = tmp_path / run / app_id / "txn-service"
    (app_dir / "src").mkdir(parents=True)
    (app_dir / "pom.xml").write_text("<project/>")
    ops = [FileOp("modify", app_id, "txn-service/src/A.java", "x", None)]

    class _DB:
        def get(self, model, key):
            if model is AgenticRun:
                return SimpleNamespace(selected_repo_ids=[core_id, app_id])
            return SimpleNamespace(
                role="core" if key == core_id else "app",
                depends_on=None,
                label=key,
            )

    steps, _ = VP.build_plan(_DB(), run, SimpleNamespace(operations=ops))
    assert any(step.repo_id == core_id and step.kind == "install" for step in steps)


def test_mvn_test_tolerates_unmatched_upstream_reactor_modules():
    # `-pl <module> -am` pulls dependency siblings into the reactor; Surefire 3 hard-fails
    # any of them whose tests don't match -Dtest unless failIfNoSpecifiedTests is off
    # (a DIFFERENT flag from failIfNoTests). Regression: run 74a785ac looped forever on
    # "No tests matching pattern" thrown by an upstream module with zero matching tests.
    argv = V._mvn_test("dataaccessor", ["MandateRegistryServiceTest"])
    assert "-Dsurefire.failIfNoSpecifiedTests=false" in argv
    assert "-DfailIfNoTests=false" in argv
    assert argv[-1] == "test"


def test_unparseable_failure_surfaces_gate_command_and_mojo_line():
    # No path:line diagnostics to parse → the fallback must hand the agent (1) the exact
    # command the gate ran (it cannot see it any other way) and (2) the mojo-failure
    # sentence, not just Maven's boilerplate footer tail.
    step = VerificationStep(
        "test", "r1",
        ["mvn", "-B", "-pl", "dataaccessor", "-am", "-Dtest=FooTest",
         "-DfailIfNoTests=false", "-Dsurefire.failIfNoSpecifiedTests=false", "test"])
    tail = (
        "[INFO] Building network-domain-xsd 1.0-SNAPSHOT\n"
        "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-surefire-plugin:3.5.4:test "
        '(default-test) on project network-domain-xsd: No tests matching pattern "FooTest" were executed! '
        "(Set -Dsurefire.failIfNoSpecifiedTests=false to ignore this error.) -> [Help 1]\n"
        "[ERROR] \n"
        "[ERROR] For more information about the errors and possible solutions, please read the following articles:\n"
        "[ERROR] [Help 1] http://cwiki.apache.org/confluence/display/MAVEN/MojoFailureException\n")
    sr = V.StepResult(step, exit_code=1, timed_out=False, gate_pass=False, output_tail=tail)
    errs = V.format_step_errors(sr)
    assert any(e.startswith("test command: $ mvn -B -pl dataaccessor -am") for e in errs)
    assert any("No tests matching pattern" in e for e in errs)


# ── _skip_patterns: SCR findings #4/#14 (ReDoS / Regex Injection) ────────────

def test_skip_patterns_wildcard_still_matches_as_substring(monkeypatch):
    # Existing dev-container config (*igw*,*hsm-proxy*) must keep working
    # identically after switching to the escape-then-restore-wildcard build.
    monkeypatch.setattr(_settings, "agentic_verify_skip_modules", "*igw*,*hsm-proxy*")
    pats = V._skip_patterns()
    assert any(p.search("module-igw-core failed") for p in pats)
    assert any(p.search("hsm-proxy-adapter") for p in pats)
    assert not any(p.search("unrelated-module") for p in pats)


def test_skip_patterns_no_wildcard_still_matches_substring(monkeypatch):
    # A spec with no wildcards used to still substring-match via bare
    # re.compile(spec) — must be preserved by the escape step.
    monkeypatch.setattr(_settings, "agentic_verify_skip_modules", "igw")
    pats = V._skip_patterns()
    assert any(p.search("some-module-igw-core failed") for p in pats)


def test_skip_patterns_regex_metacharacters_are_neutralised(monkeypatch):
    # The actual finding: a spec containing regex metacharacters other than
    # `*` must be treated as a LITERAL string, not compiled as regex syntax.
    # Before the fix, `(a+)+$` compiled as a nested-quantifier pattern with
    # catastrophic-backtracking potential; after the fix it only matches
    # itself literally.
    monkeypatch.setattr(_settings, "agentic_verify_skip_modules", r"(a+)+$")
    pats = V._skip_patterns()
    assert len(pats) == 1
    assert pats[0].search(r"(a+)+$")
    assert not pats[0].search("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa!")
