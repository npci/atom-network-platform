# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Sandbox runner — the three exec-contract traps, containment, prove-it-runs.

Forced onto the subprocess backend (the operative one in the stack: celery has
the docker CLI but no socket). The docker path shares every contract-parsing
line, differing only in process spawning.
"""
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.agents.governance_sandbox import _normalize, run_script, smoke_bundle

SCAN = '''
import json, sys, os, re
target = sys.argv[1]
items = []
for root, _, fs in os.walk(target):
    for f in fs:
        p = os.path.join(root, f)
        t = open(p, encoding="utf-8", errors="replace").read()
        for m in re.finditer(r"AKIA[A-Z0-9]{16}|password\\s*=", t):
            items.append({"file": os.path.relpath(p, target), "why": "secret-shaped"})
print(json.dumps({"total_findings": len(items), "items": items}))
sys.exit(0)   # exits 0 EVEN WITH findings — the probe's real trap
'''

CONTRACT = {"path": "scripts/scan.py", "role": "validator",
            "invocation": "python3 scripts/scan.py {target}", "timeout_seconds": 60,
            "output_format": "json_stdout", "findings_parse": "stdout.json.total_findings",
            "exit_semantics": "0 always", "normalize": []}


@pytest.fixture(autouse=True)
def _subprocess_backend(monkeypatch):
    monkeypatch.setattr(settings, "governance_sandbox_backend", "subprocess", raising=False)


@pytest.fixture()
def bundle(tmp_path):
    bd = tmp_path / "bundle"
    (bd / "scripts").mkdir(parents=True)
    (bd / "scripts" / "scan.py").write_text(SCAN)
    (bd / "scripts" / "hang.py").write_text("import time\ntime.sleep(120)\n")
    td = tmp_path / "target"
    td.mkdir()
    (td / "a.py").write_text('AWS = "AKIAIOSFODNN7EXAMPLE"\npassword = "x"\n')
    return bd, td


def test_trap1_exit_zero_validator_findings_surface(bundle):
    bd, td = bundle
    r = run_script(CONTRACT, bundle_dir=bd, target_dir=td)
    assert r.ran and r.exit_code == 0
    assert r.findings_count == 2 and len(r.gate_findings) == 2   # NOT read from the exit code


def test_trap3_generator_never_gates(bundle):
    bd, td = bundle
    r = run_script(dict(CONTRACT, role="generator"), bundle_dir=bd, target_dir=td)
    assert r.findings_count == 2 and r.gate_findings == []


def test_timeout_contained(bundle):
    bd, td = bundle
    r = run_script({**CONTRACT, "path": "scripts/hang.py",
                    "invocation": "python3 scripts/hang.py {target}", "timeout_seconds": 2,
                    "output_format": "exit_code", "findings_parse": None},
                   bundle_dir=bd, target_dir=td)
    assert not r.ran and "timed out" in (r.error or "")


def test_unparseable_output_is_an_error_never_a_pass(bundle):
    bd, td = bundle
    (bd / "scripts" / "junk.py").write_text('print("not json at all")')
    r = run_script({**CONTRACT, "path": "scripts/junk.py",
                    "invocation": "python3 scripts/junk.py {target}"},
                   bundle_dir=bd, target_dir=td)
    assert r.ran and r.error and "parse" in r.error
    assert r.findings_count is None                      # unknown ≠ zero


def test_trap2_normalize_preserves_keys():
    assert _normalize({"uuid": "abc", "total": 3}, ["uuid"]) == {"uuid": "<normalized>", "total": 3}
    n = _normalize({"timestamp": "2026-08-06T10:00:00", "items": [1]}, [])
    assert n["timestamp"] == "<normalized>" and n["items"] == [1]
    assert _normalize({"id": "RULE-7"}, [])["id"] == "RULE-7"    # finding ids untouched


def _row(entries, exec_manifest):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in entries:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return SimpleNamespace(bundle_bytes=buf.getvalue(), bundle_filename="b.tar.gz",
                           exec_manifest_json=exec_manifest)


def test_smoke_green_for_working_validator(tmp_path):
    row = _row([("SKILL.md", b"---\nname: is\n---\nrun the scanner"),
                ("scripts/scan.py", SCAN.encode())],
               {"scripts": [CONTRACT]})
    out = smoke_bundle(row, tmp_path / "w")
    assert out["status"] == "green"
    bad = [s for s in out["scripts"] if s["fixture"] == "bad"][0]
    assert bad["findings_count"] > 0


def test_smoke_fails_a_validator_that_misses_the_planted_secret(tmp_path):
    row = _row([("SKILL.md", b"x"),
                ("scripts/noop.py",
                 b'import json\nprint(json.dumps({"total_findings":0,"items":[]}))')],
               {"scripts": [{**CONTRACT, "path": "scripts/noop.py",
                             "invocation": "python3 scripts/noop.py {target}"}]})
    out = smoke_bundle(row, tmp_path / "w")
    assert out["status"] == "failed"
    assert any("known-bad" in (s.get("verdict") or "") for s in out["scripts"])


def test_smoke_fails_a_crashing_script(tmp_path):
    row = _row([("SKILL.md", b"x"), ("scripts/boom.py", b"raise SystemExit(3)")],
               {"scripts": [{"path": "scripts/boom.py", "role": "generator",
                             "invocation": "python3 scripts/boom.py {target}",
                             "timeout_seconds": 30, "output_format": "exit_code",
                             "findings_parse": None, "exit_semantics": "0=ok",
                             "normalize": [], "network": False}]})
    out = smoke_bundle(row, tmp_path / "w")
    # A generator exiting nonzero is a BROKEN script — the prove-it-runs gate fails it.
    assert out["status"] == "failed"
    assert any("generator exited 3" in (s.get("verdict") or "") for s in out["scripts"])


# ── XML scanner output (Checkmarx CxXMLResults, generic issue XML) ─────────────

_CX_EMITTER = '''
import sys
# Emit a Checkmarx-shaped report to the {output} file path passed as argv[2],
# falling back to stdout so both source paths are exercised.
xml = """<?xml version="1.0"?>
<CxXMLResults>
  <Query name="SQL_Injection">
    <Result Severity="High" FileName="A.java" Line="10" QueryID="1"/>
    <Result Severity="High" FileName="B.java" Line="20" QueryID="1"/>
  </Query>
  <Query name="Weak_Hash">
    <Result Severity="Medium" FileName="C.java" Line="30" QueryID="2"/>
  </Query>
</CxXMLResults>"""
if len(sys.argv) > 2:
    open(sys.argv[2], "w").write(xml)
else:
    print(xml)
sys.exit(0)
'''


def _xml_contract(**over):
    c = {"path": "cx.py", "role": "validator", "output_format": "xml",
         "invocation": "python3 cx.py {target} {output}",
         "findings_parse": ".//Result", "timeout_seconds": 30}
    c.update(over)
    return c


def test_xml_output_counts_result_elements(tmp_path):
    bundle = tmp_path / "b"; bundle.mkdir()
    (bundle / "cx.py").write_text(_CX_EMITTER)
    target = tmp_path / "t"; target.mkdir()
    r = run_script(_xml_contract(), bundle_dir=bundle, target_dir=target,
                   scratch_dir=tmp_path / "s")
    assert r.ran and r.error is None
    assert r.findings_count == 3               # all Result elements
    assert {f["file"] for f in r.findings} == {"A.java", "B.java", "C.java"}
    assert r.findings[0]["severity"] == "high"


def test_xml_findings_parse_can_filter_by_attribute(tmp_path):
    bundle = tmp_path / "b"; bundle.mkdir()
    (bundle / "cx.py").write_text(_CX_EMITTER)
    r = run_script(_xml_contract(findings_parse='.//Result[@Severity="High"]'),
                   bundle_dir=bundle, target_dir=tmp_path / "t", scratch_dir=tmp_path / "s")
    assert r.findings_count == 2               # High only


def test_xml_entity_expansion_is_rejected_not_parsed(tmp_path):
    # XXE guard: a script emitting a DTD/entity must error, never silently pass.
    evil = ('import sys\nprint("""<?xml version="1.0"?>'
            '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
            '<CxXMLResults><Result>&e;</Result></CxXMLResults>""")\n')
    bundle = tmp_path / "b"; bundle.mkdir()
    (bundle / "cx.py").write_text(evil)
    r = run_script(_xml_contract(invocation="python3 cx.py {target}"),
                   bundle_dir=bundle, target_dir=tmp_path / "t", scratch_dir=tmp_path / "s")
    assert r.findings_count is None and r.error   # rejected, not a pass


def test_xml_empty_output_is_error_not_pass(tmp_path):
    bundle = tmp_path / "b"; bundle.mkdir()
    (bundle / "cx.py").write_text("import sys; sys.exit(0)\n")   # writes nothing
    r = run_script(_xml_contract(invocation="python3 cx.py {target}"),
                   bundle_dir=bundle, target_dir=tmp_path / "t", scratch_dir=tmp_path / "s")
    assert r.findings_count is None and "empty" in (r.error or "")


# ── Skill-shipped smoke fixtures (design: skills carry evals/) ─────────────────

def _bundle_row(entries, exec_manifest):
    """A GovernanceSkill-like row with a real bundle for smoke_bundle()."""
    from types import SimpleNamespace
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in entries:
            ti = tarfile.TarInfo(name); ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return SimpleNamespace(bundle_bytes=buf.getvalue(), bundle_filename="b.tar.gz",
                           exec_manifest_json=exec_manifest)


# A validator that scans a DIRECTORY (walks files, counts AKIA/password hits).
_DIR_SCANNER = b'''import json, os, sys
t = sys.argv[1]; n = 0
for root, _, fs in os.walk(t):
    for f in fs:
        try: txt = open(os.path.join(root, f), encoding="utf-8", errors="replace").read()
        except OSError: continue
        n += txt.count("AKIA") + txt.lower().count("password")
print(json.dumps({"total_findings": n, "items": [{"why": "hit"}] * n})); sys.exit(0)
'''

# A report-GRADER validator that takes --in <file.json> (EA/sast architecture).
_GRADER = b'''import argparse, json, sys
ap = argparse.ArgumentParser(); ap.add_argument("--in", dest="inp"); ap.add_argument("--json", action="store_true")
a = ap.parse_args()
try: doc = json.load(open(a.inp))
except Exception: print(json.dumps({"errors": 1, "ok": False})); sys.exit(2)
errs = 1 if doc.get("bad") else 0
print(json.dumps({"errors": errs, "ok": errs == 0})); sys.exit(1 if errs else 0)
'''


def test_smoke_uses_skill_shipped_directory_fixtures(tmp_path, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "governance_sandbox_backend", "subprocess", raising=False)
    row = _bundle_row(
        [("SKILL.md", b"---\nname: x\n---\nb"),
         ("scripts/scan.py", _DIR_SCANNER),
         ("evals/fixtures/bad/leak.py", b'AWS="AKIAEXAMPLE00000000"\npassword="p"\n'),
         ("evals/fixtures/good/ok.py", b"def add(a,b):\n    return a+b\n")],
        {"scripts": [{"path": "scripts/scan.py", "role": "validator",
                      "invocation": "python3 scripts/scan.py {target}",
                      "output_format": "json_stdout",
                      "findings_parse": "stdout.json.total_findings", "timeout_seconds": 30,
                      "smoke": {"bad": "evals/fixtures/bad", "good": "evals/fixtures/good",
                                "expect_bad_min": 2}}]})
    out = smoke_bundle(row, tmp_path)
    assert out["status"] == "green", out
    bad = next(s for s in out["scripts"] if s["fixture"] == "bad")
    assert bad["fixture_src"] == "skill:evals/fixtures/bad" and bad["findings_count"] >= 2


def test_smoke_report_grader_uses_per_script_file_fixture(tmp_path, monkeypatch):
    """A report-grader (--in report.json) can't scan a code dir — it points its smoke
    at shipped report FILES. This is exactly the EA/sast architecture."""
    from app.core.config import settings
    monkeypatch.setattr(settings, "governance_sandbox_backend", "subprocess", raising=False)
    row = _bundle_row(
        [("SKILL.md", b"---\nname: x\n---\nb"),
         ("scripts/validate.py", _GRADER),
         ("evals/fixtures/bad-report.json", b'{"bad": true}'),
         ("evals/fixtures/good-report.json", b'{"bad": false}')],
        {"scripts": [{"path": "scripts/validate.py", "role": "validator",
                      "invocation": "python3 scripts/validate.py --in {target} --json",
                      "output_format": "json_stdout", "findings_parse": "stdout.json.errors",
                      "timeout_seconds": 30,
                      "smoke": {"bad": "evals/fixtures/bad-report.json",
                                "good": "evals/fixtures/good-report.json"}}]})
    out = smoke_bundle(row, tmp_path)
    assert out["status"] == "green", out
    bad = next(s for s in out["scripts"] if s["fixture"] == "bad")
    assert bad["findings_count"] == 1 and bad["fixture_src"].startswith("skill:")


def test_smoke_expect_bad_min_not_met_fails(tmp_path, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "governance_sandbox_backend", "subprocess", raising=False)
    row = _bundle_row(
        [("SKILL.md", b"---\nname: x\n---\nb"),
         ("scripts/scan.py", _DIR_SCANNER),
         ("evals/fixtures/bad/one.py", b'password="p"\n'),   # only 1 hit
         ("evals/fixtures/good/ok.py", b"clean\n")],
        {"scripts": [{"path": "scripts/scan.py", "role": "validator",
                      "invocation": "python3 scripts/scan.py {target}",
                      "output_format": "json_stdout",
                      "findings_parse": "stdout.json.total_findings", "timeout_seconds": 30,
                      "smoke": {"bad": "evals/fixtures/bad", "good": "evals/fixtures/good",
                                "expect_bad_min": 5}}]})           # demand 5, get 1
    out = smoke_bundle(row, tmp_path)
    assert out["status"] == "failed"
    assert any("expected >= 5" in s.get("verdict", "") for s in out["scripts"])
