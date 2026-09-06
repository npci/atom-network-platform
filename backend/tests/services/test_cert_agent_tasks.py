# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Spec-shape contract tests for all 14 cert task payloads (protocol Part B).

Ported from the standalone cert-agent prototype alongside the builders they
cover — see ``app/services/cert_agent/tasks.py`` for why the shapes were
vendored into the platform rather than the prototype service being wired up.

These are contract tests, not behaviour tests: they pin the field names the
spec's Appendix B requires, so a later refactor of the cert path cannot quietly
drop one. `test_overall_states_match_the_engine_state_machine` is the one
addition — it binds this layer to the state machine already in the tree.
"""
from __future__ import annotations

from app.services.cert_agent import tasks as T


def test_all_14_task_types_present():
    assert len(T.ALL_CERT_TASKS) == 14
    assert T.AUTHORITY_TO_PARTNER and T.PARTNER_TO_AUTHORITY and T.EITHER
    # no overlap between the three direction sets
    assert not (T.AUTHORITY_TO_PARTNER & T.PARTNER_TO_AUTHORITY)


def test_cert_config_request_shape():
    p = T.cert_config_request()
    assert p["mode"] == "initial"
    assert p["schema_version"] == "v1.0"
    assert "bank_identity.nbin" in p["required_fields"]
    assert "supported_features" in p["optional_fields"]
    assert p["respond_by"] and p["instructions"]


def test_cert_config_submission_shape():
    p = T.cert_config_submission(
        bank_identity={"nbin": "BKID0X"}, network={"host": "10.0.0.1"},
        security={"tls_tier": "mtls"}, roles=["payer_psp"], requested_subset="Subset-D",
    )
    for k in ("renews_cflow_id", "bank_identity", "network", "security", "roles",
              "supported_protocol_versions", "supported_features", "requested_subset",
              "contacts", "preferred_window"):
        assert k in p
    assert p["requested_subset"] == "Subset-D"


def test_cert_setup_notification_shape():
    p = T.cert_setup_notification(
        simulator={"endpoint": "https://sim", "protocol_version": "NET-2.x", "credentials_ref": "vault://x"},
        suite_version="v2.7_v28", subset="Subset-D",
        case_list=[{"case_id": "PR_1", "initiator": "bank", "api": "ReqTransfer", "expected_status": "Success"}],
    )
    assert p["simulator"]["endpoint"].startswith("https://")
    assert p["case_list"][0]["case_id"] == "PR_1"


def test_cert_test_preparation_shape():
    p = T.cert_test_preparation(case_data={"RE_1": {"ready": True}})
    assert p["case_data"]["RE_1"]["ready"] is True


def test_cert_case_result_shape():
    p = T.cert_case_result(case_id="PR_1", attempt=1, reporter="bank", status="passed",
                           details={"latency_ms": 242})
    assert p["reporter"] == "bank" and p["status"] == "passed"


def test_cert_verdict_notification_shape():
    p = T.cert_verdict_notification(case_id="RE_5", attempt=1, verdict="real_defect",
                                    reasoning="missing CredAcc")
    assert p["verdict"] == "real_defect"
    assert p["human_approved"] is True
    assert p["evidence_refs"] == [] and p["spec_references"] == []


def test_cert_verdict_dispute_shape():
    p = T.cert_verdict_dispute(case_id="RE_5", attempt=1, disputed_verdict="real_defect",
                               bank_position="simulator issue")
    assert p["requested_action"] == "re_triage"


def test_cert_waiver_request_shape():
    p = T.cert_waiver_request(case_id="PR_22", category="non_applicable", reason="not offered")
    assert p["category"] == "non_applicable"


def test_cert_waiver_decision_shape():
    p = T.cert_waiver_decision(case_id="PR_22", decision="granted", valid_until="2027-06-04")
    assert p["decision"] == "granted"
    assert p["approvers"] == []


def test_cert_fix_notification_shape():
    p = T.cert_fix_notification(fixed_case_ids=["RE_5"], fix_summary="added CredAcc")
    assert p["fixed_case_ids"] == ["RE_5"] and p["ready_for_rerun"] is True


def test_cert_signoff_notification_shape():
    p = T.cert_signoff_notification(
        documents=[{"type": "certificate", "doc_ref": "doc://x"}],
        suite_version="v2.7_v28", subset="Subset-D",
        case_outcomes={"total": 223, "passed": 218, "waived": 5, "failed": 0},
    )
    assert p["case_outcomes"]["total"] == 223
    assert p["issued_at"]


def test_cert_status_request_shape():
    p = T.cert_status_request()
    assert p["scope"] == "full"
    assert "case_ids" in p["filters"]


def test_cert_status_report_shape():
    p = T.cert_status_report(overall_state="RUNNING", stage="execution",
                             counts={"total": 3, "passed": 1})
    assert p["overall_state"] == "RUNNING"
    assert p["snapshot_at"]


def test_cert_run_abort_shape():
    p = T.cert_run_abort(reason="wrong subset", category="subset_mismatch")
    assert p["category"] == "subset_mismatch"


# ── the binding this vendoring exists to make explicit ────────────────────────

def test_overall_states_match_the_engine_state_machine():
    """`cert_status_report.overall_state` must be a phase the engine can be in.

    Two separate lists of cert phases now live in the tree, and they are NOT
    identical:

      * `tasks.OVERALL_STATES` — the 11 values the spec's §8.2 cert lifecycle
        names, which is the vocabulary a `cert_status_report` may carry.
      * `precert_engine.state_machine.Phase` — 13, adding BLOCKED and CERTIFIED.

    Both extras are legitimate engine states (a rejected waiver blocks a run;
    sign-off certifies it) but neither is in the spec's enum. So reporting a run
    that sits in either one would put an off-vocabulary value on the wire.

    This test pins the relationship rather than hiding it: the spec's states
    must all be real phases, and the surplus must be exactly those two. If a
    phase is added or the spec list changes, this fails and forces the decision
    (extend the spec, or map the phase down before reporting).
    """
    from app.services.precert_engine.state_machine import Phase

    engine_phases = {p.value for p in Phase}
    spec_states = set(T.OVERALL_STATES)

    unknown_to_engine = spec_states - engine_phases
    assert not unknown_to_engine, f"spec states with no engine phase: {sorted(unknown_to_engine)}"

    assert engine_phases - spec_states == {"BLOCKED", "CERTIFIED"}
