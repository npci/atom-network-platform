# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""C3 static safety checks — unit tests over synthetic ``_render_diff`` blobs.

Each blob mimics ``agentic_review._render_diff`` output: manifest lines ("#  "),
NEW FILE blocks with raw content, and "(changes vs base)" sections with unified
git-diff hunks.
"""

from app.agents import contract_gate as CG

MANIFEST = (
    "# CHANGE MANIFEST — every file in this change (ground truth, never truncated).\n"
    "#  modify  [switch] src/main/java/com/example/handler/RespTransferHandler.java  (10 bytes)\n"
)


def _new_file(path: str, body: str, repo: str = "switch") -> str:
    return f"# repo {repo} NEW FILE {path}\n{body}"


def _tracked(path: str, hunk_body: str, repo: str = "switch") -> str:
    return (f"# repo {repo} (changes vs base)\n"
            f"diff --git a/{path} b/{path}\n"
            f"index 1111111..2222222 100644\n"
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            f"@@ -1,6 +1,6 @@\n"
            f"{hunk_body}")


# ── check_publish_before_persist ──────────────────────────────────────────────

PUBLISH_THEN_PERSIST = _new_file(
    "src/main/java/com/example/split/SplitService.java",
    "public class SplitService {\n"
    "    public Mono<Void> handle(Txn txn) {\n"
    "        eventPublisher.publishEvent(new SplitCompleted(txn));\n"
    "        return hashLogService.appendLog(txn.getId(), stage);\n"
    "    }\n"
    "}\n")

PERSIST_THEN_PUBLISH = _new_file(
    "src/main/java/com/example/split/SplitService.java",
    "public class SplitService {\n"
    "    public Mono<Void> handle(Txn txn) {\n"
    "        return hashLogService.appendLog(txn.getId(), stage)\n"
    "            .doOnSuccess(v -> eventPublisher.publishEvent(new SplitCompleted(txn)));\n"
    "    }\n"
    "}\n")


def test_publish_before_persist_flags_inversion():
    findings = CG.check_publish_before_persist(MANIFEST + "\n" + PUBLISH_THEN_PERSIST)
    assert len(findings) == 1
    f = findings[0]
    assert f.check == "publish_before_persist"
    assert f.severity == "warning"
    assert f.file.endswith("SplitService.java")
    assert "publishEvent" in f.detail


def test_publish_inside_persistence_chain_not_flagged():
    assert CG.check_publish_before_persist(MANIFEST + "\n" + PERSIST_THEN_PUBLISH) == []


def test_publish_with_no_persist_after_not_flagged():
    blob = _new_file(
        "src/main/java/com/example/split/Notifier.java",
        "public class Notifier {\n"
        "    public void notify(Txn txn) {\n"
        "        applicationEventPublisher.publishEvent(new SplitCompleted(txn));\n"
        "    }\n"
        "}\n")
    assert CG.check_publish_before_persist(blob) == []


def test_publish_before_persist_ignores_non_java():
    blob = _new_file(
        "src/main/resources/notes.txt",
        "eventPublisher.publishEvent(x);\n"
        "hashLogService.appendLog(a, b);\n")
    assert CG.check_publish_before_persist(blob) == []


def test_publish_before_persist_in_tracked_hunk():
    blob = MANIFEST + "\n" + _tracked(
        "src/main/java/com/example/split/SplitService.java",
        " context line\n"
        "+        eventPublisher.publishEvent(new SplitCompleted(txn));\n"
        "+        sessionStore.putAll(sessionKey, fields);\n"
        " more context\n")
    findings = CG.check_publish_before_persist(blob)
    assert len(findings) == 1


# ── check_shared_file_behavior_edits ──────────────────────────────────────────

PLANNED = {"src/main/java/com/example/split/SplitService.java"}

UNPLANNED_DELETION = _tracked(
    "src/main/java/com/example/handler/RespTransferHandler.java",
    " context line\n"
    "-        eventPublisher.publishEvent(new ReqTxnConfirmation(txn));\n"
    " more context\n")


def test_unplanned_behavior_deletion_is_blocker():
    findings = CG.check_shared_file_behavior_edits(
        MANIFEST + "\n" + UNPLANNED_DELETION, PLANNED, "")
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "blocker"
    assert "file the plan never named" in f.detail
    assert "ReqTxnConfirmation" in f.detail
    assert f.file.endswith("RespTransferHandler.java")
    assert "revert" in f.suggested_fix.lower() or "amend" in f.suggested_fix.lower()


def test_planned_file_edit_not_flagged():
    blob = _tracked(
        "src/main/java/com/example/split/SplitService.java",
        "-        old.behaviour(line);\n"
        "+        new.behaviour(line);\n")
    assert CG.check_shared_file_behavior_edits(blob, PLANNED, "") == []


def test_planned_path_matches_by_last_two_segments_case_insensitive():
    # Plan names the file with a different repo-root prefix and casing.
    findings = CG.check_shared_file_behavior_edits(
        UNPLANNED_DELETION, {"Handler/RespTransferHandler.JAVA"}, "")
    assert findings == []


def test_directive_named_file_is_sanctioned():
    findings = CG.check_shared_file_behavior_edits(
        UNPLANNED_DELETION, PLANNED,
        "[D1] shared handler: RespTransferHandler.java must stop publishing confirmation")
    assert findings == []


def test_moved_line_readded_identically_not_flagged():
    blob = _tracked(
        "src/main/java/com/example/handler/RespTransferHandler.java",
        "-        eventPublisher.publishEvent(new ReqTxnConfirmation(txn));\n"
        "+        eventPublisher.publishEvent(new ReqTxnConfirmation(txn));\n")
    assert CG.check_shared_file_behavior_edits(blob, PLANNED, "") == []


def test_comment_only_deletion_not_flagged():
    blob = _tracked(
        "src/main/java/com/example/handler/RespTransferHandler.java",
        "-        // legacy note about confirmation ordering\n"
        "+        int x = 1;\n")
    assert CG.check_shared_file_behavior_edits(blob, PLANNED, "") == []


def test_empty_planned_paths_disables_check():
    assert CG.check_shared_file_behavior_edits(UNPLANNED_DELETION, set(), "") == []


def test_new_files_never_flagged():
    assert CG.check_shared_file_behavior_edits(PUBLISH_THEN_PERSIST, PLANNED, "") == []


# ── check_money_leg_declarations ──────────────────────────────────────────────

CREDIT_CALL = _new_file(
    "src/main/java/com/example/split/DisputeService.java",
    "public class DisputeService {\n"
    "    void settle(Txn txn) {\n"
    "        reqPayBuilder.buildMerchantCreditReqTransfer(txn);\n"
    "    }\n"
    "}\n")


def test_undeclared_credit_leg_warns():
    findings = CG.check_money_leg_declarations(CREDIT_CALL, "[D1] atomicity: all-or-nothing")
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "warning"
    assert f.key == "buildMerchantCreditReqTransfer"
    assert "not traceable to a declared money leg" in f.detail
    assert "DisputeService.java" in f.detail


def test_declared_credit_leg_not_flagged():
    directives = "[D2] money legs: one merchantCredit ReqTransfer per participant on expiry"
    assert CG.check_money_leg_declarations(CREDIT_CALL, directives) == []


def test_generic_credit_token_passes_on_credit_mention():
    blob = _new_file("src/A.java", "svc.createReqTransferCredit(txn);\n")
    assert CG.check_money_leg_declarations(blob, "[D1] credit the payee bank") == []
    assert len(CG.check_money_leg_declarations(blob, "[D1] debit only")) == 1


def test_debit_legs_never_flagged():
    blob = _new_file(
        "src/A.java",
        "svc.createReqpayDebit(txn);\n"
        "type = REQPAY_DEBIT_REVERSAL;\n"
        "kind = BANK_REQPAY_DEBIT;\n")
    assert CG.check_money_leg_declarations(blob, "") == []


def test_duplicate_credit_sites_deduped_per_file():
    blob = _new_file(
        "src/A.java",
        "svc.createReqTransferCredit(a);\n"
        "svc.createReqTransferCredit(b);\n")
    assert len(CG.check_money_leg_declarations(blob, "")) == 1


# ── check_config_keys_declared ────────────────────────────────────────────────

PLAN_WITH_KEY = ("Sessions expire per split.session.ttl.seconds; classes live in "
                 "com.example.split.service and edit SplitService.java")


def test_promised_key_unbound_warns():
    blob = _new_file("src/A.java", "int ttl = 600;\n")
    findings = CG.check_config_keys_declared(blob, PLAN_WITH_KEY)
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "warning"
    assert f.key == "split.session.ttl.seconds"
    assert "no @Value binds it" in f.detail
    # Package name and filename were not mistaken for config keys.
    assert all("com.example" not in x.key and ".java" not in x.key for x in findings)


def test_value_bound_key_not_flagged():
    blob = _new_file(
        "src/A.java",
        '@Value("${split.session.ttl.seconds:600}")\n'
        "private long ttlSeconds;\n")
    assert CG.check_config_keys_declared(blob, PLAN_WITH_KEY) == []


def test_key_present_in_added_yaml_counts_as_bound():
    blob = _new_file("src/main/resources/application.yml",
                     "split.session.ttl.seconds: 600\n")
    assert CG.check_config_keys_declared(blob, PLAN_WITH_KEY) == []


def test_plan_without_dotted_keys_skips():
    blob = _new_file("src/A.java", "int ttl = 600;\n")
    assert CG.check_config_keys_declared(blob, "Add a session TTL of 600 seconds") == []


# ── run_contract_gate wiring ──────────────────────────────────────────────────

def test_gate_aggregates_c3_findings_and_blocker_flag():
    blob = (MANIFEST + "\n" + PUBLISH_THEN_PERSIST + "\n\n" + CREDIT_CALL
            + "\n\n" + UNPLANNED_DELETION)
    res = CG.run_contract_gate(blob, None, planned_paths=PLANNED,
                               directives_text="[D1] atomicity: all-or-nothing",
                               plan_text=PLAN_WITH_KEY)
    checks = {f.check for f in res.findings}
    assert {"publish_before_persist", "shared_file_behavior_edit",
            "money_leg_declaration", "config_key_declared"} <= checks
    assert res.has_blocker  # only the shared-file edit blocks
    assert all(f.severity == "warning" for f in res.findings
               if f.check != "shared_file_behavior_edit")


def test_gate_defaults_disable_plan_dependent_checks():
    # No planned_paths/directives/plan_text: shared-file and config checks are inert
    # and warnings-only checks still run — nothing raises, no blockers.
    res = CG.run_contract_gate(MANIFEST + "\n" + UNPLANNED_DELETION, None)
    assert not res.has_blocker
    assert all(f.check not in ("shared_file_behavior_edit", "config_key_declared")
               for f in res.findings)


def test_gate_fail_open_on_garbage_input():
    res = CG.run_contract_gate("", None, planned_paths={"x/y.java"},
                               directives_text=None, plan_text=None)  # type: ignore[arg-type]
    assert res.findings == [] or not res.has_blocker


# ── check_error_code_emission (corpus-aware) ──────────────────────────────────

_U16 = [{"code": "U16", "entity": "NPCI", "td_bd": "TD", "description": "txn not found"}]


def test_error_code_direct_literal_in_diff_not_flagged():
    diff = _new_file("src/main/java/com/example/x/H.java",
                     'return buildError(req, "U16", "not found");\n')
    assert CG.check_error_code_emission(diff, _U16) == []


def test_error_code_via_named_constant_needs_corpus():
    # The change emits correctly via a NAMED CONSTANT; the raw literal lives only in
    # the (unchanged) constants file. Diff-only scope FALSELY flags it; corpus clears it.
    diff = _new_file("src/main/java/com/example/x/H.java",
                     "return buildError(req, UdirErrorCodes.TXN_NOT_FOUND, msg);\n")
    assert [f.key for f in CG.check_error_code_emission(diff, _U16)] == ["U16"]  # FP without corpus
    corpus = 'public static final String TXN_NOT_FOUND = "U16";'
    assert CG.check_error_code_emission(diff, _U16, corpus_text=corpus) == []     # fixed with corpus


def test_error_code_never_emitted_still_flagged_even_with_corpus():
    # The real cbabbf9c bug: code computed via substring, no literal anywhere. Must
    # still flag even when a (different) corpus is supplied.
    diff = _new_file("src/main/java/com/example/x/H.java",
                     "String cd = msg.substring(0, 2);\n")
    corpus = 'public static final String OTHER = "ZZ";'
    assert [f.key for f in CG.check_error_code_emission(diff, _U16, corpus_text=corpus)] == ["U16"]


def test_error_code_corpus_threaded_through_run_gate():
    diff = _new_file("src/main/java/com/example/x/H.java",
                     "return buildError(req, UdirErrorCodes.TXN_NOT_FOUND, msg);\n")
    corpus = 'public static final String TXN_NOT_FOUND = "U16";'
    res = CG.run_contract_gate(diff, _U16, corpus_text=corpus)
    assert not any(f.check == "error_code_emission" for f in res.findings)
