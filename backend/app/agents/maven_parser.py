# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Parse Maven / javac output into structured diagnostics (THE BOOK §9.3/§9.4).

The runtime drives builds through one ``run_command`` (§18.2) and feeds the raw
output here. The hard gates (§9) are computed by the runtime from the *exit
code* — this parser turns the text into ``{severity, file, line, col, module,
message}`` records for diagnosis and for **soft-fail tagging** (§9.2): an error
whose Maven module is in ``soft_fail_modules`` (e.g. the legacy Java-8 ``IGW``)
is downgraded to ``severity='soft'`` so the runtime can exclude it from the gate
**only when that module is untouched / out-of-scope** (the in-scope decision is
the runtime's, §9.2 — this parser just tags origin).

Pure and deterministic: no I/O, no config import. The caller supplies the
soft-fail module set (from settings) so the parser stays testable in isolation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class MavenDiagnostic:
    severity: str            # "error" | "warning" | "soft"
    message: str
    file: str | None = None
    line: int | None = None
    col: int | None = None
    module: str | None = None


@dataclass
class MavenParseResult:
    diagnostics: list[MavenDiagnostic] = field(default_factory=list)
    build_success: bool | None = None      # None when no BUILD SUCCESS/FAILURE line
    modules_with_errors: set[str] = field(default_factory=set)
    # Build-ENVIRONMENT failures (JDK version mismatch, dependency resolution, nexus
    # 401, offline-cache miss). These are NOT code defects — the agent cannot fix them
    # by editing source, so the runtime must classify them apart from compile errors
    # (§9) and fail fast instead of looping code_change on an infra problem.
    environment_errors: list[str] = field(default_factory=list)
    required_java: int | None = None       # JDK major the build demanded (from a release error)

    @property
    def error_count(self) -> int:
        return sum(1 for d in self.diagnostics if d.severity == "error")

    @property
    def soft_count(self) -> int:
        return sum(1 for d in self.diagnostics if d.severity == "soft")

    @property
    def is_environment_failure(self) -> bool:
        """An environment failure with NO source-level compile diagnostics — i.e. the
        build aborted on the toolchain/deps, not on the generated code. (A JDK or
        dependency-resolution failure produces no ``file:line`` diagnostics because the
        compiler never reaches the code.)"""
        return bool(self.environment_errors) and self.error_count == 0


# maven-compiler-plugin: "[ERROR] /abs/Foo.java:[12,34] cannot find symbol"
#                        "[WARNING] /abs/Foo.java:[12] deprecated"  (col optional)
_MVN_DIAG = re.compile(
    r"^\[(?P<sev>ERROR|WARNING)\]\s+(?P<file>(?:[A-Za-z]:)?[^:\[\]]+\.\w+):"
    r"\[(?P<line>\d+)(?:,(?P<col>\d+))?\]\s*(?P<msg>.*)$"
)
# Plain javac (no [ERROR] prefix): "/abs/Foo.java:12: error: cannot find symbol"
_JAVAC_DIAG = re.compile(
    r"^(?P<file>(?:[A-Za-z]:)?[^:\s]+\.\w+):(?P<line>\d+):\s*(?P<sev>error|warning):\s*(?P<msg>.*)$"
)
# Current module: "[INFO] --- maven-compiler-plugin:3.11:compile (default) @ network-core ---"
#            or:  "[INFO] Building network-core 2.0.0"
_MODULE_PLUGIN = re.compile(r"@\s+(?P<mod>[A-Za-z0-9_.\-]+)\s+---\s*$")
_MODULE_BUILDING = re.compile(r"^\[INFO\]\s+Building\s+(?P<mod>\S+)")
_BUILD_RESULT = re.compile(r"^\[INFO\]\s+BUILD\s+(?P<result>SUCCESS|FAILURE)")

# Build-ENVIRONMENT failure signatures (§9 — infra, not code). The JDK-version cases
# carry the demanded major so the runtime can say "needs Java N but active JDK is M".
#   "invalid target release: 25" / "invalid source release: 25"
#   "error: release version 25 not supported"
_JAVA_RELEASE_ERR = re.compile(
    r"(?:invalid (?:target|source) release:\s*|release version\s+)(\d+)", re.I)
#   "Source option 6 is no longer supported. Use 7 or later."
_JAVA_OPTION_ERR = re.compile(r"(?:Source|Target) option\s+(\d+)\s+is no longer supported", re.I)
# Dependency-resolution / plugin-resolution / repository-auth / offline-cache
# failures. These mean the build environment (the warmed ~/.m2 + nexus access),
# not the generated code, is broken — e.g. an offline `mvn -o` whose local cache
# is missing a plugin like maven-source-plugin ("Plugin not found in any plugin
# repository" / PluginVersionResolutionException). The agent cannot fix these by
# editing source, so they must NOT drive code_change.
_DEP_RESOLVE_ERR = re.compile(
    r"Could not resolve dependencies|Could not (?:find|transfer) artifact|"
    r"Cannot access .+ in offline mode|Failed to read artifact descriptor|"
    r"Non-resolvable (?:parent|import) POM|The following artifacts could not be resolved|"
    r"Plugin (?:.+ )?not found in any plugin repository|PluginVersionResolutionException|"
    r"PluginResolutionException|Error resolving version for plugin|"
    r"Could not resolve plugin|No plugin found for prefix|"
    # Local-repo init failure (a broken/unwritable ~/.m2). Not a prod concern — the native
    # build runs as the OS user with a real home — but classifying it env keeps ANY broken
    # env from burning code_change retries on something no source edit can fix.
    r"Could not create local repository|LocalRepositoryNotAccessibleException|"
    r"status code:\s*401|Authentication failed|Access denied|Return code is:\s*40\d",
    re.I)

# Reactor can't start because a declared <module> directory is absent (module removed,
# or present only at a different/wrong version). The line names the missing module path.
_CHILD_MODULE_MISSING = re.compile(r"Child module .+ does not exist", re.I)


def parse_maven_output(
    text: str,
    soft_fail_modules: frozenset[str] | set[str] = frozenset(),
    skip_patterns=(),
) -> MavenParseResult:
    """Parse ``mvn``/``javac`` output. ``soft_fail_modules`` downgrades matching modules'
    errors to ``severity='soft'`` (origin tagging only — §9.2). ``skip_patterns`` (compiled
    regexes, from AGENTIC_VERIFY_SKIP_MODULES) downgrade ANY failure attributable to a
    matching module — compile error, missing reactor module, or unresolved dependency — to
    'soft', unconditionally, so an out-of-scope module never reaches the gate."""
    result = MavenParseResult()
    soft = set(soft_fail_modules)
    skip_pats = list(skip_patterns)
    current_module: str | None = None

    def _skip(s) -> bool:
        return bool(s) and any(p.search(s) for p in skip_pats)

    for raw in (text or "").splitlines():
        line = raw.rstrip()

        m = _MODULE_BUILDING.search(line) or _MODULE_PLUGIN.search(line)
        if m:
            current_module = m.group("mod")
            continue

        br = _BUILD_RESULT.search(line)
        if br:
            # A reactor can print SUCCESS for early modules then FAILURE; the
            # run as a whole fails if ANY FAILURE appears.
            failed = br.group("result") == "FAILURE"
            result.build_success = False if failed else (result.build_success is not False)
            continue

        # AGENTIC_VERIFY_SKIP_MODULES: a missing-reactor-module or dependency-resolution
        # failure that names an out-of-scope module (or occurs while building one) is
        # recorded 'soft' — NOT an environment/hard failure — so it never fails the gate.
        if skip_pats and (_DEP_RESOLVE_ERR.search(line) or _CHILD_MODULE_MISSING.search(line)) \
                and (_skip(line) or _skip(current_module)):
            result.diagnostics.append(MavenDiagnostic(
                severity="soft", message=line.strip()[:300], module=current_module))
            continue

        # Build-environment failures (infra, not code). Recorded separately so the
        # runtime can fail fast with a precise reason instead of driving code_change.
        jr = _JAVA_RELEASE_ERR.search(line) or _JAVA_OPTION_ERR.search(line)
        if jr:
            result.required_java = int(jr.group(1))
            result.environment_errors.append(line.strip()[:300])
            continue
        if _DEP_RESOLVE_ERR.search(line):
            result.environment_errors.append(line.strip()[:300])
            continue

        diag = _MVN_DIAG.match(line)
        if diag:
            sev = "error" if diag.group("sev") == "ERROR" else "warning"
        else:
            diag = _JAVAC_DIAG.match(line)
            if not diag:
                continue
            sev = diag.group("sev")

        # Match the skip pattern against the error's FILE PATH too, not just current_module:
        # a parallel reactor build (`mvn -T`) interleaves module output, so current_module is
        # unreliable — but the path always carries the module dir (…/iupi-domain/src/…).
        if sev == "error" and (current_module in soft or _skip(current_module) or _skip(diag.group("file") or "")):
            sev = "soft"
        if sev == "error" and current_module:
            result.modules_with_errors.add(current_module)

        result.diagnostics.append(MavenDiagnostic(
            severity=sev,
            message=diag.group("msg").strip(),
            file=diag.group("file"),
            line=int(diag.group("line")),
            col=int(diag.group("col")) if diag.groupdict().get("col") else None,
            module=current_module,
        ))

    return result
