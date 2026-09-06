# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the deterministic acceptance-predicate checker (R4).

Pure functions, no I/O. Covers diff parsing (standard hunks + _render_diff NEW FILE
blocks), file matching (incl. the cross-basename false-match guard), every predicate
kind, and the fail-safe direction (a checker that can't evaluate returns `unknown`,
never a false `satisfied`/`unmet`).
"""
from app.agents.acceptance_predicates import (
    parse_diff, _match_files, check_predicate, check_predicates, _basename,
    _is_test_path, unmet, feedback_block, summarize,
)


# ── parse_diff ────────────────────────────────────────────────────────────────

def test_parse_diff_standard_hunk():
    diff = (
        "diff --git a/svc/Foo.java b/svc/Foo.java\n"
        "--- a/svc/Foo.java\n"
        "+++ b/svc/Foo.java\n"
        "@@ -1,3 +1,4 @@\n"
        " context line\n"
        "+added line one\n"
        "-removed line\n"
        "+added.setPurposeRemark(x)\n"
    )
    parsed = parse_diff(diff)
    assert "svc/Foo.java" in parsed
    assert parsed["svc/Foo.java"]["added"] == ["added line one", "added.setPurposeRemark(x)"]
    assert parsed["svc/Foo.java"]["removed"] == ["removed line"]


def test_parse_diff_new_file_block_counts_all_lines_added():
    # _render_diff renders untracked new files as raw content (no '+' prefixes).
    diff = (
        "# repo r1 NEW FILE svc/NewSvc.java\n"
        "package x;\n"
        "class NewSvc { void run() {} }\n"
        "# repo r1 (changes vs base)\n"
        "diff --git a/svc/Old.java b/svc/Old.java\n"
        "+++ b/svc/Old.java\n"
        "+touched\n"
    )
    parsed = parse_diff(diff)
    assert parsed["svc/NewSvc.java"]["added"] == ["package x;", "class NewSvc { void run() {} }"]
    # the section-header line must NOT leak into the new-file content
    assert "svc/Old.java" in parsed and parsed["svc/Old.java"]["added"] == ["touched"]


def test_parse_diff_dev_null_and_empty():
    assert parse_diff("") == {}
    # +++ /dev/null (a deletion target) must not create a file entry
    parsed = parse_diff("+++ b/real.py\n+x\n+++ /dev/null\n")
    assert "real.py" in parsed and "/dev/null" not in parsed


# ── _match_files (the cross-basename guard) ──────────────────────────────────

def test_match_files_exact_basename_and_anchored_suffix():
    parsed = {"a/b/Assembler.java": {}, "x/y/Other.java": {}}
    assert _match_files("assembler.java", parsed) == ["a/b/Assembler.java"]
    assert _match_files("b/Assembler.java", parsed) == ["a/b/Assembler.java"]  # anchored suffix
    assert _match_files("nope.java", parsed) == []


def test_match_files_does_not_match_across_basename():
    # THE BUG GUARD: target "assembler.java" must NOT satisfy against "ReqTransferAssembler.java"
    parsed = {"svc/ReqTransferAssembler.java": {"added": [], "removed": []}}
    assert _match_files("assembler.java", parsed) == []
    # but the real basename still matches
    assert _match_files("reqtransferassembler.java", parsed) == ["svc/ReqTransferAssembler.java"]


# ── check_predicate: every kind ──────────────────────────────────────────────

def _diff_with(path, added_lines):
    body = "".join(f"+{ln}\n" for ln in added_lines)
    return f"+++ b/{path}\n@@ -0,0 +1 @@\n{body}"


def test_file_touched_satisfied_and_unmet():
    parsed = parse_diff(_diff_with("svc/Foo.java", ["x"]))
    assert check_predicate({"kind": "file_touched", "file": "Foo.java"}, parsed).status == "satisfied"
    assert check_predicate({"kind": "file_touched", "file": "Missing.java"}, parsed).status == "unmet"


def test_added_in_file_scopes_to_the_file():
    parsed = parse_diff(
        _diff_with("A.java", ["a.setPurposeRemark(v)"]) + _diff_with("B.java", ["unrelated"])
    )
    ok = check_predicate({"kind": "added_in_file", "file": "A.java", "contains": "setPurposeRemark"}, parsed)
    assert ok.status == "satisfied"
    # present in the diff, but NOT in the scoped file → unmet (not satisfied)
    scoped = check_predicate({"kind": "added_in_file", "file": "B.java", "contains": "setPurposeRemark"}, parsed)
    assert scoped.status == "unmet"
    # file not touched at all
    absent = check_predicate({"kind": "added_in_file", "file": "Z.java", "contains": "x"}, parsed)
    assert absent.status == "unmet" and "not touched" in absent.evidence


def test_added_anywhere_and_regex():
    parsed = parse_diff(_diff_with("A.java", ['errors.add("UT01")']))
    assert check_predicate({"kind": "added_anywhere", "regex": r'errors\.add\("UT\d+"\)'}, parsed).status == "satisfied"
    assert check_predicate({"kind": "added_anywhere", "contains": "notpresent"}, parsed).status == "unmet"


def test_is_test_path():
    assert _is_test_path("network-core/src/test/java/com/example/FooTest.java")
    assert _is_test_path("src/test/resources/x.xml")
    assert _is_test_path("a\\b\\src\\test\\java\\X.java")  # windows separators
    # production sources and lookalikes are NOT test paths
    assert not _is_test_path("network-core/src/main/java/com/example/Foo.java")
    assert not _is_test_path("src/test-utils/Helper.java")   # not the test source root
    assert not _is_test_path("contract-tests/Foo.java")      # not under src/test/
    assert not _is_test_path("")


def test_added_anywhere_excludes_test_sources():
    # THE PRODUCER-IN-TEST GUARD: a production token that exists ONLY under src/test/** must NOT
    # satisfy an `added_anywhere` predicate — the production behaviour is genuinely absent.
    test_only = parse_diff(_diff_with("network-core/src/test/java/PayIT.java", ["kafkaTemplate.send(topic, evt)"]))
    r = check_predicate({"kind": "added_anywhere", "contains": "kafkaTemplate.send("}, test_only)
    assert r.status == "unmet"

    # the SAME token in a src/main file still satisfies
    in_main = parse_diff(_diff_with("network-core/src/main/java/PayService.java", ["kafkaTemplate.send(topic, evt)"]))
    assert check_predicate({"kind": "added_anywhere", "contains": "kafkaTemplate.send("}, in_main).status == "satisfied"

    # token in BOTH (added to main + a test) → satisfied via the main occurrence
    both = parse_diff(
        _diff_with("network-core/src/main/java/PayService.java", ["kafkaTemplate.send(topic, evt)"])
        + _diff_with("network-core/src/test/java/PayIT.java", ["kafkaTemplate.send(topic, evt)"])
    )
    ok = check_predicate({"kind": "added_anywhere", "contains": "kafkaTemplate.send("}, both)
    assert ok.status == "satisfied" and "PayService.java".lower() in ok.evidence.lower()


def test_added_in_file_still_matches_named_test_file():
    # A file-scoped predicate the extractor explicitly aimed at a test file is unaffected by the
    # src/test exclusion (only `added_anywhere` is scoped to production).
    parsed = parse_diff(_diff_with("src/test/java/PayIT.java", ["assertThat(x).isEqualTo(1)"]))
    r = check_predicate({"kind": "added_in_file", "file": "PayIT.java", "contains": "assertThat"}, parsed)
    assert r.status == "satisfied"


def test_no_stub_detects_placeholder():
    clean = parse_diff(_diff_with("A.java", ["real logic here"]))
    assert check_predicate({"kind": "no_stub"}, clean).status == "satisfied"
    stubbed = parse_diff(_diff_with("A.java", ["// TODO implement later"]))
    r = check_predicate({"kind": "no_stub"}, stubbed)
    assert r.status == "unmet" and "TODO" in r.evidence


# ── fail-safe direction: unknown, never a false satisfied/unmet ──────────────

def test_unknown_kind_and_bad_regex_are_unknown_not_satisfied():
    parsed = parse_diff(_diff_with("A.java", ["x"]))
    assert check_predicate({"kind": "totally_made_up"}, parsed).status == "unknown"
    # invalid regex must not raise, must not satisfy — it's a checker gap → unknown
    assert check_predicate({"kind": "added_anywhere", "regex": "("}, parsed).status == "unknown"
    # predicate with neither contains nor regex → unknown
    assert check_predicate({"kind": "added_anywhere"}, parsed).status == "unknown"


def test_check_predicates_filters_non_dicts_and_summarizes():
    parsed_diff = _diff_with("A.java", ["a.setX(1)"])
    results = check_predicates(
        [{"kind": "added_anywhere", "contains": "setX"},
         {"kind": "file_touched", "file": "Nope.java"},
         "not-a-dict"],
        parsed_diff,
    )
    assert len(results) == 2  # the string was filtered
    s = summarize(results)
    assert s["satisfied"] == 1 and s["unmet"] == 1 and s["total"] == 2
    assert unmet(results)[0].predicate["file"] == "Nope.java"
    fb = feedback_block(results)
    assert "DEFINITION OF DONE" in fb and "Nope.java" in fb
    assert feedback_block([]) == ""  # nothing unmet → empty
