# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Agentic tools (§8) against a real local git workspace. In-process, no DB."""
import subprocess
from pathlib import Path

import pytest

from app.core.config import settings
from app.agents import agentic_tools as T

RID = "repo-1"
RUN = "run-1"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    rd = tmp_path / RUN / RID
    (rd / "src").mkdir(parents=True)
    (rd / "src" / "A.java").write_text("class A {\n    int x = 1;\n}\n")
    (rd / "MODULE_NOTES.md").write_text("This module does refunds.")
    subprocess.run(["git", "init", "-q"], cwd=rd, check=True)
    subprocess.run(["git", "add", "-A"], cwd=rd, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
                   cwd=rd, check=True)
    return rd


def _ctx():
    return T.RunContext(run_id=RUN, selected_repo_ids=[RID])


def test_read_file_records_read_and_injects_module_notes(ws):
    ctx = _ctx()
    out = T.read_file(ctx, RID, "src/A.java")
    assert "int x = 1;" in out
    assert "MODULE_NOTES" in out and "refunds" in out          # injected once
    assert (RID, "src/A.java") in ctx.read_files
    # second read under the same notes dir does not re-inject
    assert "MODULE_NOTES" not in T.read_file(ctx, RID, "src/A.java")


def test_path_escape_is_refused(ws):
    out, is_error = T.execute_tool(_ctx(), "read_file", {"repo_id": RID, "path": "../../etc/passwd"})
    assert is_error and "escape" in out


def test_unselected_repo_is_refused(ws):
    out, is_error = T.execute_tool(_ctx(), "read_file", {"repo_id": "other", "path": "src/A.java"})
    assert is_error and "not in the selected set" in out


def test_edit_requires_prior_read(ws):
    out, is_error = T.execute_tool(
        _ctx(), "edit_file",
        {"repo_id": RID, "path": "src/A.java", "old_string": "int x = 1;", "new_string": "int x = 2;"})
    assert is_error and "read-before-edit" in out


def test_edit_after_read_applies_and_records_fileop(ws):
    ctx = _ctx()
    T.read_file(ctx, RID, "src/A.java")
    msg = T.edit_file(ctx, RID, "src/A.java", "int x = 1;", "int x = 2;")
    assert "match level 1" in msg
    assert (ws / "src" / "A.java").read_text().__contains__("int x = 2;")
    op = ctx.file_ops[(RID, "src/A.java")]
    assert op.op == "modify" and op.content_hash


def test_create_then_edit_keeps_add_op(ws):
    ctx = _ctx()
    T.create_file(ctx, RID, "src/B.java", "class B {\n  int q = 1;\n}\n")
    assert ctx.file_ops[(RID, "src/B.java")].op == "add"
    # editing a freshly-created file (already "read") keeps op=add
    T.edit_file(ctx, RID, "src/B.java", "int q = 1;", "int q = 9;")
    assert ctx.file_ops[(RID, "src/B.java")].op == "add"
    assert "int q = 9;" in (ws / "src" / "B.java").read_text()


def test_delete_records_delete_op(ws):
    ctx = _ctx()
    T.delete_file(ctx, RID, "src/A.java")
    assert ctx.file_ops[(RID, "src/A.java")].op == "delete"
    assert not (ws / "src" / "A.java").exists()


def test_grep_and_glob(ws):
    ctx = _ctx()
    assert "A.java" in T.grep(ctx, RID, "int x")
    assert T.grep(ctx, RID, "zzz-no-match") == "(no matches)"
    assert "src/A.java" in T.glob(ctx, RID, "**/*.java")


def test_glob_prunes_real_git_keeps_lookalike(ws):
    ctx = _ctx()
    (ws / "data.git").mkdir()
    (ws / "data.git" / "keep.txt").write_text("x")
    lines = T.glob(ctx, RID, "**/*").splitlines()
    assert "data.git/keep.txt" in lines                 # ".git"-in-name dir is NOT the git dir
    assert not any(l.startswith(".git/") for l in lines)  # real .git object store pruned


def test_submit_plan_and_unknown_tool(ws):
    ctx = _ctx()
    T.submit_plan(ctx, "do the thing", reuse_decisions=[{"x": "extend"}])
    assert ctx.plan["summary"] == "do the thing"
    out, is_error = T.execute_tool(ctx, "nope", {})
    assert is_error and "unknown tool" in out
    assert "read_file" in out                      # corrective: lists the available tools


def test_bad_arguments_echo_what_was_received(ws):
    out, is_error = T.execute_tool(_ctx(), "read_file",
                                   {"repo_id": RID, "path": "src/A.java", "bogus_arg": 1})
    assert is_error and "bogus_arg" in out          # the model can see its own mistake


# ── read_output: paged retrieval of truncated run_command output (P1) ─────────

def test_read_output_pages_a_stashed_output(ws):
    ctx = _ctx()
    oid = T._stash_output(ctx, "\n".join(f"line{i}" for i in range(1, 101)))
    out = T.read_output(ctx, oid, start_line=40, end_line=42)
    assert "line40" in out and "line42" in out and "line43" not in out
    assert "lines 40-42 of 100" in out


def test_read_output_unknown_id_lists_available(ws):
    ctx = _ctx()
    T._stash_output(ctx, "x")
    with pytest.raises(T.ToolError, match="out1"):
        T.read_output(ctx, "out99")


def test_stash_output_is_fifo_bounded(ws):
    ctx = _ctx()
    for i in range(T._OUTPUT_STASH_MAX + 3):
        T._stash_output(ctx, f"payload{i}")
    assert len(ctx.command_outputs) == T._OUTPUT_STASH_MAX
    assert "out1" not in ctx.command_outputs        # oldest evicted


def test_run_command_truncation_carries_retrieval_pointer(ws, monkeypatch):
    from types import SimpleNamespace
    big = "\n".join(f"[INFO] noise {i}" for i in range(2000)) + "\n[ERROR] the real problem\n" \
          + "\n".join(f"tail {i}" for i in range(200))
    monkeypatch.setattr(T.adapter, "run_command",
                        lambda *a, **k: SimpleNamespace(stdout=big, stderr="", exit_code=1,
                                                        timed_out=False, duration_ms=10))
    ctx = _ctx()
    out = T.run_command(ctx, RID, ["git", "status"])
    assert "read_output" in out and '"out1"' in out          # pointer to the full log
    assert "[ERROR] the real problem" in out                  # error lines still inline
    full = T.read_output(ctx, "out1")
    assert "[INFO] noise 0" in full                           # the part the excerpt dropped


# ── Cross-repo discovery (two-repo production topology: core + app) ────────────

RID2 = "repo-2"


@pytest.fixture
def ws2(ws, tmp_path):
    """A second repo in the same run workspace — like network-core next to network-2.0."""
    rd = tmp_path / RUN / RID2
    (rd / "schemas").mkdir(parents=True)
    (rd / "schemas" / "Pay.xsd").write_text('<xs:element name="retryFlag"/>\n')
    subprocess.run(["git", "init", "-q"], cwd=rd, check=True)
    subprocess.run(["git", "add", "-A"], cwd=rd, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
                   cwd=rd, check=True)
    return rd


def _ctx2():
    return T.RunContext(run_id=RUN, selected_repo_ids=[RID, RID2])


def test_grep_all_repos_when_repo_id_omitted(ws, ws2):
    out = T.grep(_ctx2(), pattern="retryFlag")
    # Both repos searched, grouped by header; the match is only in repo-2.
    assert f"## repo {RID}" in out and f"## repo {RID2}" in out
    assert "Pay.xsd" in out and "retryFlag" in out
    # Java content from repo-1 only matches in its own section.
    out2 = T.grep(_ctx2(), pattern="int x")
    assert "A.java" in out2


def test_glob_all_repos_when_repo_id_omitted(ws, ws2):
    out = T.glob(_ctx2(), pattern="**/*.xsd")
    assert f"## repo {RID2}" in out and "schemas/Pay.xsd" in out


def test_single_repo_scoping_still_works(ws, ws2):
    # Explicit repo_id keeps the old single-repo behaviour (no headers).
    out = T.grep(_ctx2(), RID2, "retryFlag")
    assert "## repo" not in out and "Pay.xsd" in out
    assert T.grep(_ctx2(), RID, "retryFlag") == "(no matches)"


def test_find_existing_xsd_all_repos_without_db(ws, ws2):
    # Omitted repo_id must not crash repo-scoping; degrades to the no-index hint.
    out = T.find_existing_xsd(_ctx2(), query="Pay")
    assert "unavailable" in out


# ── repo_id resilience: AiNxt's tool-call shim sometimes DROPS arguments, so a
#    well-formed model call arrives with repo_id missing. Recover when unambiguous;
#    give a clear, retryable error otherwise — never a raw TypeError that burns a turn.

def test_read_file_without_repo_id_single_repo_resolves(ws):
    # One selected repo → repo_id is unambiguous, the call must just work.
    out, is_error = T.execute_tool(_ctx(), "read_file", {"path": "src/A.java"})
    assert not is_error and "int x = 1;" in out


def test_read_file_without_repo_id_infers_from_path_multi_repo(ws, ws2):
    # Two repos, but the path exists in exactly one → infer it.
    out, is_error = T.execute_tool(_ctx2(), "read_file", {"path": "schemas/Pay.xsd"})
    assert not is_error and "retryFlag" in out


def test_read_file_without_repo_id_ambiguous_gives_clear_error(ws, ws2):
    # Path in neither repo + multiple repos → actionable error, not a TypeError.
    out, is_error = T.execute_tool(_ctx2(), "read_file", {"path": "nope/missing.java"})
    assert is_error and "repo_id" in out and "bad arguments" not in out


def test_flow_context_without_repo_id_single_repo_no_typeerror(ws):
    # Missing repo_id used to crash with TypeError; now it resolves the one repo
    # and degrades to the no-index hint (db is None here).
    out, is_error = T.execute_tool(_ctx(), "flow_context", {})
    assert not is_error and "unavailable" in out


def test_module_context_without_repo_id_single_repo_no_typeerror(ws):
    out, is_error = T.execute_tool(_ctx(), "module_context", {})
    assert not is_error and "unavailable" in out


def test_run_command_without_repo_id_single_repo_resolves(ws):
    out, is_error = T.execute_tool(_ctx(), "run_command", {"argv": ["git", "status", "--short"]})
    assert not is_error                                       # ran in the one repo, no TypeError


# ── build-env hardening: jansi temp-dir fix + JDK switch plumbing (platform_adapter)

def test_build_env_pins_writable_tmpdir_for_maven(tmp_path, monkeypatch):
    # The jansi ".../libjansi.so.lck (No such file or directory)" failure is a missing
    # java.io.tmpdir. For build commands the adapter must pin TMPDIR + java.io.tmpdir
    # to a dir it CREATES, so the native load can't fail on a missing dir.
    from app.agents.platform_adapter import adapter
    monkeypatch.setenv("HOME", str(tmp_path))
    env = adapter._augment_build_env({"PATH": "/usr/bin"}, "mvn")
    assert "TMPDIR" in env
    from pathlib import Path
    assert Path(env["TMPDIR"]).is_dir()                  # actually created
    assert "-Djava.io.tmpdir=" in env["MAVEN_OPTS"]
    assert "-Djansi.tmpdir=" in env["MAVEN_OPTS"]


def test_build_env_untouched_for_non_build_commands():
    from app.agents.platform_adapter import adapter
    base = {"PATH": "/usr/bin"}
    assert adapter._augment_build_env(dict(base), "git") == base   # no MAVEN_OPTS/TMPDIR
    assert adapter._augment_build_env(dict(base), "grep") == base


# ── P1a: code-intelligence honesty — blind-index coverage guard ──

class _EmptyQuery:
    """Query whose .all()/.first() are always empty — drives the 'no hits' branch."""
    def filter(self, *a, **k): return self
    def limit(self, n): return self
    def all(self): return []
    def first(self): return None


class _EmptyDB:
    def query(self, *a, **k): return _EmptyQuery()


def test_blind_index_note_present_only_when_blind(monkeypatch):
    ctx = T.RunContext(run_id=RUN, selected_repo_ids=[RID], db=_EmptyDB())
    monkeypatch.setattr(T, "_calls_index_ok", lambda c: False)
    assert "cannot determine" in T._blind_index_note(ctx)
    monkeypatch.setattr(T, "_calls_index_ok", lambda c: True)
    assert T._blind_index_note(ctx) == ""                       # populated index → no scary note


def test_callers_flags_unknown_blast_radius_when_index_blind(monkeypatch):
    ctx = T.RunContext(run_id=RUN, selected_repo_ids=[RID], db=_EmptyDB())
    monkeypatch.setattr(T, "_calls_index_ok", lambda c: False)
    out = T.callers(ctx, symbol="settle")
    assert "no indexed callers" in out
    assert "cannot determine" in out and "blast radius" in out.lower()   # empty ≠ 'safe'


def test_callers_no_warning_when_index_populated(monkeypatch):
    ctx = T.RunContext(run_id=RUN, selected_repo_ids=[RID], db=_EmptyDB())
    monkeypatch.setattr(T, "_calls_index_ok", lambda c: True)
    out = T.callers(ctx, symbol="settle")
    assert "no indexed callers" in out                          # genuine empty
    assert "cannot determine" not in out                        # ...with no false alarm


def test_impact_fallback_flags_blind_index(monkeypatch):
    ctx = T.RunContext(run_id=RUN, selected_repo_ids=[RID], db=_EmptyDB())
    monkeypatch.setattr(T, "_calls_index_ok", lambda c: False)
    out = T._impact_sql_fallback(ctx, "settle", None)
    assert "no indexed impact" in out and "cannot determine" in out


def test_calls_index_probe_fails_open_and_caches():
    # No DB → fail-open True (never over-warn).
    assert T._calls_index_ok(T.RunContext(run_id=RUN, selected_repo_ids=[RID], db=None)) is True

    class _BoomDB:
        def query(self, *a, **k): raise RuntimeError("db down")
    assert T._calls_index_ok(T.RunContext(run_id=RUN, selected_repo_ids=[RID], db=_BoomDB())) is True

    cached = T.RunContext(run_id=RUN, selected_repo_ids=[RID], db=_EmptyDB())
    cached.index_calls_ok = False                                # explicit cache respected (no re-probe)
    assert T._calls_index_ok(cached) is False


# ── P1b: guarded post-edit Java syntax validation ──

# Live only when the tree-sitter Java grammar is actually present (else _java_parses_ok fails open).
_PARSE_CHECK_LIVE = T._java_parses_ok("class A {}") and not T._java_parses_ok("class A {")


def test_java_parses_ok_behaviour():
    if _PARSE_CHECK_LIVE:
        assert T._java_parses_ok("class A { int x = 1; }")
        assert not T._java_parses_ok("class A { int x = ")     # truncated → ERROR node
    else:
        assert T._java_parses_ok("anything at all")            # grammar absent → fail-open True


@pytest.mark.skipif(not _PARSE_CHECK_LIVE, reason="tree-sitter-java grammar not available")
def test_edit_file_rejects_syntax_breaking_java_edit_when_guard_on(ws, monkeypatch):
    monkeypatch.setattr(settings, "use_ast_editor", True)
    ctx = T.RunContext(run_id=RUN, selected_repo_ids=[RID])
    ctx.read_files.add((RID, "src/A.java"))                    # satisfy read-before-edit
    before = (ws / "src" / "A.java").read_text()
    with pytest.raises(T.ToolError) as e:
        T.edit_file(ctx, RID, "src/A.java", "}\n", "")         # delete the closing brace → broken
    assert "syntax error" in str(e.value).lower()
    assert (ws / "src" / "A.java").read_text() == before        # disk untouched (rejected pre-write)


@pytest.mark.skipif(not _PARSE_CHECK_LIVE, reason="tree-sitter-java grammar not available")
def test_edit_file_allows_valid_java_edit_when_guard_on(ws, monkeypatch):
    monkeypatch.setattr(settings, "use_ast_editor", True)
    ctx = T.RunContext(run_id=RUN, selected_repo_ids=[RID])
    ctx.read_files.add((RID, "src/A.java"))
    out = T.edit_file(ctx, RID, "src/A.java", "int x = 1;", "int x = 2;")
    assert "edited" in out
    assert "int x = 2;" in (ws / "src" / "A.java").read_text()   # valid edit applied


def test_edit_file_guard_off_is_backcompat(ws, monkeypatch):
    # Default (use_ast_editor=False): no parse gate — a breaking edit applies exactly as before.
    monkeypatch.setattr(settings, "use_ast_editor", False)
    ctx = T.RunContext(run_id=RUN, selected_repo_ids=[RID])
    ctx.read_files.add((RID, "src/A.java"))
    out = T.edit_file(ctx, RID, "src/A.java", "}\n", "")
    assert "edited" in out                                       # unchanged behaviour when flag off


def test_glob_cap_is_marked_not_silent(ws):
    for i in range(510):
        (ws / f"gen_{i:03}.txt").write_text("x")
    out = T.glob(_ctx(), RID, "gen_*.txt")
    assert "more files matched — narrow the pattern" in out


def test_all_repo_grep_failure_is_unmissable(ws):
    # A repo whose search FAILED must not skim as "searched, no hits" between the
    # other repos' results — and must not kill the whole fan-out either.
    ctx = T.RunContext(run_id=RUN, selected_repo_ids=[RID, "ghost"])
    out = T.grep(ctx, "all", "class A")
    assert "## repo repo-1" in out and "class A" in out          # real repo still searched
    assert "FAILED in this repo" in out and "UNSEARCHED" in out  # ghost repo loud, not silent


def test_git_history_failure_is_not_no_history(ws):
    # A FAILED blame (out-of-range -L) must never read as "no history / may be new" —
    # that is a false fact about the code the agent will act on.
    out = T.git_history(_ctx(), path="src/A.java", repo_id=RID, start_line=999, end_line=1000)
    assert "FAILED" in out and "UNKNOWN" in out
    assert "may be new" not in out


def test_git_history_genuinely_empty_still_reads_as_no_history(ws):
    (ws / "src" / "New.java").write_text("class New {}\n")     # untracked → log succeeds, empty
    out = T.git_history(_ctx(), path="src/New.java", repo_id=RID)
    assert "no git history" in out and "FAILED" not in out


def test_schema_guardian_without_index_says_could_not_check(ws):
    # db=None means the sibling comparison never RAN — the old footer said "new is
    # plausible", inviting a fork of an existing shared schema.
    (ws / "schema.xsd").write_text(
        '<?xml version="1.0"?>\n'
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'targetNamespace="http://npci/network/test">'
        '<xs:element name="Foo" type="xs:string"/></xs:schema>')
    out = T.schema_guardian(_ctx(), RID, "schema.xsd")
    assert "COULD NOT RUN" in out
    assert "new is plausible" not in out


def test_internal_tool_error_labels_infra_and_echoes_args(monkeypatch):
    # An unexpected exception inside a tool is the HARNESS/backend failing, not the
    # agent's call — the feedback must say so and echo the received args, or the agent
    # burns rounds "fixing" a call that was fine (the 215ead25 wrong-addressee class).
    def _boom(ctx, **kw):
        raise RuntimeError("db connection lost")

    monkeypatch.setitem(T._DISPATCH, "grep", _boom)
    out, is_error = T.execute_tool(_ctx(), "grep", {"pattern": "PayService", "repo_id": RID})
    assert is_error
    assert "INTERNAL error" in out and "NOT a problem with your call" in out
    assert "db connection lost" in out           # the real cause, not a generic label
    assert "PayService" in out                   # received args echoed — reproducible


def _grounded(ctx):
    """Seed ≥2 verified citations so propose_plan reaches the party-flow gate."""
    ctx.read_files.add((RID, "src/A.java"))
    ctx.read_files.add((RID, "MODULE_NOTES.md"))
    return [{"file": "src/A.java", "why": "flow"}, {"file": "MODULE_NOTES.md", "why": "consumer"}]


def test_party_flow_gate_ignores_java_artefacts_and_accepts_hop_coverage(ws):
    # ReqTransferProcessor is a Java artefact named after a message, NOT a wire message —
    # it must not demand a phantom party_flows entry. And a business-named flow
    # ("Balance Enquiry") whose HOPS route the wire token is fully grounded coverage.
    ctx = _ctx()
    ev = _grounded(ctx)
    ta = {"files_to_modify": [{"path": "x/ReqTransferProcessor.java", "intent": "modify routing"},
                              {"path": "x/RespBalEnq.xsd", "intent": "add odLimit"}]}
    fs = {"party_flows": [{"api": "Balance Enquiry", "classification": "existing_modified",
                           "hops": [{"from": "NPCI", "to": "Remitter Bank", "message": "RespBalEnq"}]}]}
    out = T.propose_plan(ctx, "s", {"overview": "o"}, ta, fs, evidence=ev)
    assert "plan recorded" in out


def test_party_flow_gate_still_rejects_a_genuinely_unrouted_message(ws):
    ctx = _ctx()
    ev = _grounded(ctx)
    ta = {"files_to_modify": [{"path": "x/RespBalEnq.xsd", "intent": "add odLimit"}]}
    out = T.propose_plan(ctx, "s", {"overview": "o"},
                         ta, {"party_flows": [{"api": "Balance Enquiry", "hops": []}]}, evidence=ev)
    assert "NOT recorded" in out and "RespBalEnq" in out


# --- XSD facet guard (the prod fb7aeb07 defect) --------------------------------------
# A pattern crosses two escaping layers on its way into the file (JSON tool arg → XML
# attribute). One backslash too many turns `\.` into `\\` — a LITERAL BACKSLASH — which
# WIDENS the allowed character set. Valid XSD, valid XML, clean Maven build: nothing else
# in the XSD phase reads facet content, so it shipped silently.
_SHIPPED = '<xs:pattern value="[A-Za-z0-9 .,\\-/()\\\\.@#&amp;]{0,50}"/>'
_INTENDED = '<xs:pattern value="[A-Za-z0-9 .,\\-/()\\.@#&amp;]{0,50}"/>'


def test_xsd_facet_check_flags_a_literal_backslash():
    out = T._xsd_facet_warnings("network-common.xsd", _SHIPPED)
    assert "LITERAL BACKSLASH" in out


def test_xsd_facet_check_passes_the_intended_pattern():
    assert T._xsd_facet_warnings("network-common.xsd", _INTENDED) == ""


def test_xsd_facet_check_flags_an_uncompilable_pattern():
    assert "does not compile" in T._xsd_facet_warnings("a.xsd", '<xsd:pattern value="[A-Za-z0-9"/>')


def test_xsd_facet_check_allows_xsd_only_unicode_escapes():
    # \p{L} is valid XSD and has no Python equivalent — warning here would be crying wolf.
    assert T._xsd_facet_warnings("a.xsd", '<xs:pattern value="[\\p{L}\\p{N}]{1,50}"/>') == ""


def test_xsd_facet_check_ignores_non_schema_files():
    assert T._xsd_facet_warnings("Foo.java", _SHIPPED) == ""


def test_edit_file_surfaces_the_facet_warning(ws):
    ctx = _ctx()
    (ws / "s.xsd").write_text('<xs:schema>\n<xs:pattern value="[0-9]+"/>\n</xs:schema>\n')
    T.read_file(ctx, RID, "s.xsd")
    out = T.edit_file(ctx, RID, "s.xsd", '<xs:pattern value="[0-9]+"/>',
                      '<xs:pattern value="[0-9\\\\.]+"/>')
    assert "edited s.xsd" in out and "LITERAL BACKSLASH" in out
