# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Immutable manifest hash + push preflight (§11) and push actions (§12). Pure."""
from types import SimpleNamespace

from app.agents.manifest import build_manifest, manifest_hash, push_preflight
from app.agents.agentic_push import branch_name, gitlab_actions

_PER = [{"repo_id": "r1", "base_commit_sha": "BASE", "shared_branch_name": "feature/xsd-x"}]


def _cs(ops):
    return SimpleNamespace(operations=[
        SimpleNamespace(op=o, repo_id=r, path=p, content=c, content_hash=None) for (o, r, p, c) in ops])


def _build(cs):
    return build_manifest(selected_repo_ids=["r1"], per_repo=_PER, change_set=cs,
                          verification={"status": "verified"}, review={"blocking": False})


# ── manifest hash ─────────────────────────────────────────────────────────────

def test_manifest_hash_is_stable_and_order_independent():
    m1 = _build(_cs([("modify", "r1", "a.java", "X"), ("add", "r1", "b.java", "Y")]))
    m2 = _build(_cs([("add", "r1", "b.java", "Y"), ("modify", "r1", "a.java", "X")]))  # reversed
    assert m1["manifest_hash"] == m2["manifest_hash"]      # operation order doesn't matter
    assert manifest_hash(m1) == m1["manifest_hash"]        # self-consistent (excludes the hash field)


def test_manifest_hash_changes_when_content_changes():
    m_a = _build(_cs([("modify", "r1", "a.java", "X")]))
    m_b = _build(_cs([("modify", "r1", "a.java", "DIFFERENT")]))
    assert m_a["manifest_hash"] != m_b["manifest_hash"]


def test_tampering_the_frozen_manifest_breaks_the_hash():
    m = _build(_cs([("modify", "r1", "a.java", "X")]))
    h = m["manifest_hash"]
    m["operations"][0]["content_hash"] = "tampered"
    assert manifest_hash(m) != h


# ── P2a: the ratified plan is folded into the hash ────────────────────────────

def _build_plan(cs, plan):
    return build_manifest(selected_repo_ids=["r1"], per_repo=_PER, change_set=cs,
                          verification={"status": "verified"}, review={"blocking": False}, plan=plan)


def test_ratified_plan_change_forces_new_hash_even_with_identical_diff():
    cs = _cs([("modify", "r1", "a.java", "X")])
    m_a = _build_plan(cs, "RATIFIED PLAN A")
    m_b = _build_plan(cs, "RATIFIED PLAN B")
    assert m_a["manifest_hash"] != m_b["manifest_hash"]    # same diff, changed SPEC → re-approval
    assert _build_plan(cs, "RATIFIED PLAN A")["manifest_hash"] == m_a["manifest_hash"]  # deterministic
    assert m_a["plan"] == "RATIFIED PLAN A"
    assert manifest_hash(m_a) == m_a["manifest_hash"]      # plan IS covered by the canonical hash


def test_plan_less_run_keeps_its_legacy_hash():
    cs = _cs([("modify", "r1", "a.java", "X")])
    legacy = _build(cs)                                    # pre-feature shape (no plan)
    with_none = _build_plan(cs, None)                      # falsy plan → key omitted
    assert "plan" not in with_none
    assert with_none["manifest_hash"] == legacy["manifest_hash"]   # back-compat: unchanged


# ── push preflight ────────────────────────────────────────────────────────────

def test_push_preflight_passes_when_workspace_matches():
    m = _build(_cs([("modify", "r1", "a.java", "HELLO"), ("add", "r1", "b.java", "NEW"),
                    ("delete", "r1", "c.java", None)]))
    content = {"a.java": "HELLO", "b.java": "NEW"}
    ok, reasons = push_preflight(m, current_base_sha={"r1": "BASE"},
                                 read_content=lambda r, p: content.get(p))
    assert ok and reasons == []


def test_push_preflight_rejects_base_sha_drift():
    m = _build(_cs([("modify", "r1", "a.java", "HELLO")]))
    ok, reasons = push_preflight(m, current_base_sha={"r1": "DRIFTED"},
                                 read_content=lambda r, p: "HELLO")
    assert not ok and any("base SHA" in x for x in reasons)


def test_push_preflight_rejects_content_change_since_approval():
    m = _build(_cs([("modify", "r1", "a.java", "HELLO")]))
    ok, reasons = push_preflight(m, current_base_sha={"r1": "BASE"},
                                 read_content=lambda r, p: "EDITED AFTER APPROVAL")
    assert not ok and any("content changed" in x for x in reasons)


# ── push actions + branch ─────────────────────────────────────────────────────

def test_branch_name_slug_and_collision_suffix():
    assert branch_name("Refund Status Feature!") == "atom/xsd-refund-status-feature"
    assert branch_name("Refund", suffix="2") == "atom/xsd-refund-2"
    assert branch_name("") == "atom/xsd-change"


def test_gitlab_actions_map_add_modify_delete():
    cs = _cs([("add", "r1", "a", "X"), ("modify", "r1", "b", "Y"), ("delete", "r1", "c", None)])
    acts = gitlab_actions(cs)
    by = {a["action"]: a for a in acts}
    assert set(by) == {"create", "update", "delete"}
    assert by["create"]["file_path"] == "a" and by["create"]["content"] == "X"
    assert "content" not in by["delete"] and by["delete"]["file_path"] == "c"


def test_gitlab_actions_can_filter_by_repo():
    cs = _cs([("add", "r1", "a", "X"), ("add", "r2", "b", "Y")])
    assert [a["file_path"] for a in gitlab_actions(cs, repo_id="r1")] == ["a"]
