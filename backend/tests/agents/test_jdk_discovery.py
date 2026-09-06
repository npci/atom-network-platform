# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""JDK discovery + selection (Java-version awareness, §18.1)."""
from app.agents import jdk_discovery as J


def _fake_jdk(root, name, version_line):
    home = root / name
    home.mkdir(parents=True)
    (home / "release").write_text(version_line + "\n")
    return home


def test_jdk_major_parses_modern_and_legacy(tmp_path):
    assert J.jdk_major(_fake_jdk(tmp_path, "j25", 'JAVA_VERSION="25.0.1"')) == 25
    assert J.jdk_major(_fake_jdk(tmp_path, "j17", 'JAVA_VERSION="17.0.9"')) == 17
    assert J.jdk_major(_fake_jdk(tmp_path, "j8", 'JAVA_VERSION="1.8.0_392"')) == 8
    assert J.jdk_major(tmp_path / "not-a-jdk") is None


def test_discover_jdks_finds_homes_under_a_root(tmp_path):
    _fake_jdk(tmp_path, "temurin-17", 'JAVA_VERSION="17.0.9"')
    _fake_jdk(tmp_path, "temurin-25", 'JAVA_VERSION="25"')
    jdks = J.discover_jdks(extra_roots=(str(tmp_path),))
    assert jdks.get(17) and jdks.get(25)
    assert jdks[25].endswith("temurin-25")


def test_select_jdk_home_matches_required_major():
    jdks = {17: "/jvm/17", 25: "/jvm/25"}
    assert J.select_jdk_home(25, jdks=jdks) == "/jvm/25"
    assert J.select_jdk_home(17, jdks=jdks) == "/jvm/17"
    assert J.select_jdk_home(21, jdks=jdks) is None      # not installed → caller installs
    assert J.select_jdk_home(None, jdks=jdks) is None     # nothing required


def test_macos_contents_home_is_normalised(tmp_path):
    bundle = tmp_path / "jdk-25.jdk"
    inner = bundle / "Contents" / "Home"
    inner.mkdir(parents=True)
    (inner / "release").write_text('JAVA_VERSION="25"\n')
    jdks = J.discover_jdks(extra_roots=(str(tmp_path),))
    assert jdks.get(25) == str(inner)


def test_parse_update_alternatives():
    out = ("/usr/lib/jvm/java-17-openjdk-amd64/bin/java\n"
           "/usr/lib/jvm/java-25-openjdk-amd64/bin/java\n")
    homes = J._parse_update_alternatives(out)
    assert homes == ["/usr/lib/jvm/java-17-openjdk-amd64",
                     "/usr/lib/jvm/java-25-openjdk-amd64"]


def test_parse_update_java_alternatives():
    out = ("java-1.17.0-openjdk-amd64   1711   /usr/lib/jvm/java-1.17.0-openjdk-amd64\n"
           "java-1.25.0-openjdk-amd64   2511   /usr/lib/jvm/java-1.25.0-openjdk-amd64\n")
    homes = J._parse_update_java_alternatives(out)
    assert homes == ["/usr/lib/jvm/java-1.17.0-openjdk-amd64",
                     "/usr/lib/jvm/java-1.25.0-openjdk-amd64"]
