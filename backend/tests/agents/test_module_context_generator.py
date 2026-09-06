# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Index-time module_context generation (§19). pom parsing + key-type scan are pure."""
from app.agents.module_context_generator import parse_pom_modules, _key_types

_NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'


def _pom(repo, rel, body):
    d = repo if rel == "." else repo / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "pom.xml").write_text(f'<project {_NS}>{body}</project>')


def test_parse_pom_modules_tree(tmp_path):
    _pom(tmp_path, ".", "<artifactId>network-parent</artifactId><modules>"
                        "<module>core</module><module>app</module></modules>")
    _pom(tmp_path, "core", "<artifactId>network-core</artifactId>"
                          "<properties><maven.compiler.release>17</maven.compiler.release></properties>")
    _pom(tmp_path, "app", "<artifactId>network-app</artifactId>"
                         "<properties><java.version>21</java.version></properties>"
                         "<dependencies><dependency><groupId>g</groupId>"
                         "<artifactId>network-core</artifactId></dependency></dependencies>")
    mods = {m["module_path"]: m for m in parse_pom_modules(tmp_path)}

    assert set(mods) == {".", "core", "app"}
    assert mods["."]["depth"] == 0 and mods["."]["parent_module_path"] is None
    assert mods["."]["artifact_id"] == "network-parent"
    assert mods["core"]["depth"] == 1 and mods["core"]["parent_module_path"] == "."
    assert mods["core"]["java_version"] == "17"
    assert mods["app"]["java_version"] == "21"
    assert mods["app"]["depends_on"] == ["network-core"]


def test_parse_handles_nested_submodules(tmp_path):
    _pom(tmp_path, ".", "<artifactId>root</artifactId><modules><module>core</module></modules>")
    _pom(tmp_path, "core", "<artifactId>core</artifactId><modules><module>sub</module></modules>")
    _pom(tmp_path, "core/sub", "<artifactId>core-sub</artifactId>")
    mods = {m["module_path"]: m for m in parse_pom_modules(tmp_path)}
    assert mods["core/sub"]["depth"] == 2 and mods["core/sub"]["parent_module_path"] == "core"


def test_key_types_from_module_src_only(tmp_path):
    _pom(tmp_path, ".", "<artifactId>root</artifactId><modules><module>core</module></modules>")
    _pom(tmp_path, "core", "<artifactId>core</artifactId>")
    src = tmp_path / "core" / "src" / "main" / "java"
    src.mkdir(parents=True)
    (src / "Refund.java").write_text("package x;\npublic class Refund {}\n")
    (src / "Money.java").write_text("public final class Money {}\n")
    kt = _key_types(tmp_path, "core")
    assert {t["name"] for t in kt} == {"Refund", "Money"}
    assert all(t["file"].endswith(".java") for t in kt)   # symbol → file: a jump target
    assert _key_types(tmp_path, ".") == []          # root module has no own src


def test_non_maven_repo_yields_no_modules(tmp_path):
    assert parse_pom_modules(tmp_path) == []
