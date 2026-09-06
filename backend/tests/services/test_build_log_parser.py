# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Structured extraction from an operator build+deploy script's output.

The runner executes an operator-supplied script and slices its stdout into
build / deploy / startup sections plus structured artifact and service lists.
Every cue is optional — a script that emits none still passes on exit 0 — so
these pin the extraction that DOES happen, and in particular that it is not
coupled to any one stack's naming.
"""
from __future__ import annotations

import pytest

from app.services.build_runner import _LogParser


def _feed(*lines) -> _LogParser:
    p = _LogParser()
    for ln in lines:
        p.feed(ln)
    return p


# ── services: named by their jar, not by an allowlist ────────────────────────

@pytest.mark.parametrize("name", [
    "network-backend",      # the example in the source comment — used to NOT match
    "upi-gateway",          # the old allowlist's shape still works
    "billing",              # single word, no hyphen
    "acme_ledger",          # underscores
    "portal-api-1.2.3",     # versioned jar
])
def test_service_name_comes_from_the_jar_whatever_it_is_called(name):
    p = _feed(f"appuser  12345     1  0 10:00 ?  00:00:30 java -jar /opt/app/{name}.jar --spring.profiles=prod")
    assert p.services == [{"name": name, "pid": "12345"}]


def test_java_flags_before_the_jar_do_not_break_the_match():
    p = _feed("appuser 4242 1 0 10:00 ? 00:00:01 /usr/bin/java -Xmx2g -Dfoo=bar -jar /srv/ledger.jar")
    assert p.services == [{"name": "ledger", "pid": "4242"}]


def test_same_service_restarted_keeps_the_latest_pid():
    p = _feed("u 111 1 0 ? java -jar /o/svc.jar",
              "u 222 1 0 ? java -jar /o/svc.jar")
    assert p.services == [{"name": "svc", "pid": "222"}]


def test_multiple_distinct_services_are_all_reported():
    p = _feed("u 111 1 0 ? java -jar /o/alpha.jar",
              "u 222 1 0 ? java -jar /o/beta.jar")
    assert [s["name"] for s in p.services] == ["alpha", "beta"]


@pytest.mark.parametrize("line", [
    "cp /build/target/app.jar /opt/deploy/app.jar",     # a deploy line, not a process
    "[INFO] Building jar: /build/target/app.jar",       # maven output
    "Downloading java-17-openjdk (no -jar flag here)",
])
def test_non_process_lines_are_not_mistaken_for_services(line):
    assert _feed(line).services == []


# ── artifacts + sections: the rest of the contract ───────────────────────────

def test_artifact_and_deploy_destination_are_extracted():
    p = _feed("[INFO] Building jar: /build/target/app.jar",
              "cp /build/target/app.jar /opt/deploy/app.jar")
    assert p.first_artifact_path == "/build/target/app.jar"
    assert p.artifacts == [{"path": "/build/target/app.jar", "dest": "/opt/deploy/app.jar"}]


def test_sections_split_on_cues_and_are_sticky():
    p = _feed("compiling...", "== Deploy ==", "copying", "== Startup ==", "booting")
    assert p.build == ["compiling..."]
    assert "copying" in p.deploy
    assert "booting" in p.startup


def test_build_failure_is_detected_anywhere_in_the_output():
    """This is the one cue that can override an exit code of 0."""
    assert _feed("[INFO] BUILD FAILURE").saw_build_failure is True
    assert _feed("[INFO] BUILD SUCCESS").saw_build_failure is False
