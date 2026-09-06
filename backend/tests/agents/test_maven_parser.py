# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""maven_parser is pure — exercise it directly (§9.3/§9.4)."""
from app.agents.maven_parser import parse_maven_output

_MVN = """\
[INFO] Building network-core 2.0.0
[INFO] --- maven-compiler-plugin:3.11.0:compile (default-compile) @ network-core ---
[ERROR] /ws/network-core/src/main/java/Foo.java:[12,34] cannot find symbol RefundStatus
[WARNING] /ws/network-core/src/main/java/Foo.java:[3] deprecated API
[INFO] --- maven-compiler-plugin:3.11.0:compile (default-compile) @ IGW ---
[ERROR] /ws/IGW/src/Bar.java:[5,1] incompatible types
[INFO] BUILD FAILURE
"""


def test_parses_errors_with_file_line_col_and_module():
    r = parse_maven_output(_MVN)
    foo = next(d for d in r.diagnostics if d.file.endswith("Foo.java") and d.severity == "error")
    assert (foo.line, foo.col, foo.module) == (12, 34, "network-core")
    assert r.build_success is False


def test_warning_keeps_module_but_is_not_an_error():
    r = parse_maven_output(_MVN)
    warns = [d for d in r.diagnostics if d.severity == "warning"]
    assert len(warns) == 1 and warns[0].line == 3
    assert "network-core" in r.modules_with_errors
    assert r.error_count == 2  # Foo + Bar, before any soft-fail tagging


def test_soft_fail_downgrades_only_the_configured_module():
    r = parse_maven_output(_MVN, soft_fail_modules={"IGW"})
    bar = next(d for d in r.diagnostics if d.file.endswith("Bar.java"))
    assert bar.severity == "soft"                  # IGW error downgraded
    assert r.soft_count == 1 and r.error_count == 1  # only Foo remains a hard error
    assert "IGW" not in r.modules_with_errors      # soft errors don't gate


def test_plain_javac_form():
    r = parse_maven_output("/ws/Foo.java:12: error: cannot find symbol")
    assert len(r.diagnostics) == 1
    d = r.diagnostics[0]
    assert (d.severity, d.file, d.line) == ("error", "/ws/Foo.java", 12)


def test_build_success_when_no_failure_line():
    r = parse_maven_output("[INFO] BUILD SUCCESS")
    assert r.build_success is True
    assert r.error_count == 0


def test_empty_output_is_inconclusive():
    r = parse_maven_output("")
    assert r.build_success is None and r.diagnostics == []


def test_plugin_resolution_failure_is_environment():
    # Offline `mvn -o` with a cold ~/.m2 cache missing maven-source-plugin: this is
    # an ENVIRONMENT failure (warm the cache), NOT a code defect — must not drive code_change.
    out = ("[ERROR] Error resolving version for plugin "
           "'org.apache.maven.plugins:maven-source-plugin' from the repositories "
           "[local, nexus]: Plugin not found in any plugin repository -> [Help 1]")
    r = parse_maven_output(out)
    assert r.is_environment_failure and r.error_count == 0
    assert r.environment_errors
