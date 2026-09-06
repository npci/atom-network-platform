# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Plan v+1 versioning when a gate decision diverges from the ratified plan.

When the human picks an approach the agent flagged as diverging from the plan, the plan is
rolled forward to v+1 recording the chosen approach + WHY — so the plan stops contradicting
what gets built. Best-effort: a no-prior-plan or a failure returns None, never raises.
"""
from app.agents.plan_versioning import record_approach_decision_version
from app.models.change_analysis import ChangeAnalysis


class _Query:
    def __init__(self, result):
        self._r = result

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def first(self):
        return self._r


class _FakeDB:
    def __init__(self, prior):
        self._prior = prior
        self.added = []
        self.committed = False
        self.rolledback = False

    def query(self, *a, **k):
        return _Query(self._prior)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolledback = True


def _prior(**kw):
    base = dict(change_request_id="cr", version=3, status="ratified",
                technical_analysis={"data_model_changes": "RECOMMEND new ReqSplitPay schema",
                                    "schema_inventory": [{"repo": "core", "path": "ReqTransfer.xsd"}]},
                functional_plan={"overview": "split pay"}, flow_spec={"steps": ["a"]},
                analysis_sha={"core": "abc"}, pm_ratified_by="pm1", tech_ratified_by="tl1")
    base.update(kw)
    return ChangeAnalysis(**base)


def test_creates_v_plus_1_with_decision_and_supersedes_prior():
    prior = _prior()
    db = _FakeDB(prior)
    chosen = {"id": "extend-reqtransfer", "title": "Extend ReqTransfer", "approach": "extend",
              "target_api": "payTrans",
              "divergence_note": "plan said new schema; extend payTrans instead"}
    v = record_approach_decision_version(db, change_request_id="cr", run_id="run1",
                                         chosen=chosen, decided_by="user-9")
    assert v == 4                                   # 3 → 4
    assert db.committed is True
    assert prior.status == "superseded"             # old version retired
    new = db.added[0]
    assert new.version == 4 and new.status == "ratified"   # carried the body's ratification state
    ad = new.technical_analysis["approach_decision"]
    assert ad["approach"] == "extend" and ad["diverges_from_plan"] is True
    assert "extend payTrans" in ad["why"] and ad["decided_by"] == "user-9"
    assert ad["supersedes_version"] == 3
    # changelog appended; body carried forward; prior NOT mutated in place.
    assert new.technical_analysis["plan_revisions"][-1]["version"] == 4
    assert new.functional_plan == {"overview": "split pay"}
    assert new.pm_ratified_by == "pm1" and new.tech_ratified_by == "tl1"
    assert "approach_decision" not in prior.technical_analysis   # copied, not mutated


def test_no_prior_plan_is_a_noop():
    db = _FakeDB(None)
    v = record_approach_decision_version(db, change_request_id="cr", run_id="run1",
                                         chosen={"id": "x", "approach": "new"}, decided_by="u")
    assert v is None and db.added == [] and db.committed is False


def test_kind_labels_the_changelog_entry():
    # A refine-approved supersession rolls the plan through the same machinery but its
    # changelog entry must say so — reviewers distinguish a gate choice from a review-time
    # supersession the human approved with the XSD manifest.
    db = _FakeDB(_prior())
    v = record_approach_decision_version(
        db, change_request_id="cr", run_id="run1",
        chosen={"id": "refine-supersession", "approach": "new",
                "title": "New schema approved at XSD review"},
        decided_by="u", kind="refine_supersession")
    assert v == 4
    assert db.added[0].technical_analysis["plan_revisions"][-1]["kind"] == "refine_supersession"


def test_carries_prior_status_when_not_yet_ratified():
    prior = _prior(status="awaiting_ratification", pm_ratified_by=None, tech_ratified_by=None)
    db = _FakeDB(prior)
    v = record_approach_decision_version(db, change_request_id="cr", run_id="r",
                                         chosen={"id": "x", "approach": "extend",
                                                 "divergence_note": "n"}, decided_by="u")
    assert v == 4 and db.added[0].status == "awaiting_ratification"
    assert prior.status == "superseded"


def test_why_falls_back_to_how_it_fits_when_no_note():
    prior = _prior()
    db = _FakeDB(prior)
    chosen = {"id": "x", "title": "T", "approach": "reuse", "how_it_fits": "rides ReqChkTxn"}
    v = record_approach_decision_version(db, change_request_id="cr", run_id="r",
                                         chosen=chosen, decided_by=None)
    assert v == 4
    assert db.added[0].technical_analysis["approach_decision"]["why"] == "rides ReqChkTxn"


def test_failure_returns_none_and_rolls_back():
    class _BoomDB(_FakeDB):
        def commit(self):
            raise RuntimeError("db down")

    db = _BoomDB(_prior())
    v = record_approach_decision_version(db, change_request_id="cr", run_id="r",
                                         chosen={"id": "x", "approach": "extend",
                                                 "divergence_note": "n"}, decided_by="u")
    assert v is None and db.rolledback is True
