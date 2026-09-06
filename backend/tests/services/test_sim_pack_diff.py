# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""S-5: the pack diff an operator reads before publishing.

"ReqTransfer changed" is not reviewable. The diff is FIELD-level and names the
constraint cell that moved, because those cells are what the simulator
enforces and the grader asserts — so the diff predicts what will start
failing.
"""
from __future__ import annotations

import pytest

from app.services.sim_packs.diff import diff_packs


def _pack(fields=(), scenarios=(), api="ReqTransfer", coverage=None):
    return {
        "pack_ref": "p@1",
        "apis": [{"api": api, "wire_format": "xml", "fields": list(fields)}],
        "scenarios": list(scenarios),
        "provenance": {"coverage": coverage} if coverage else {},
    }


F_BEFORE = {"path": "ReqTransfer/Txn/@type", "mandatory": "Y",
            "enum_values": ["PAY", "COLLECT"]}


def test_a_changed_constraint_cell_is_named_with_before_and_after():
    after = {**F_BEFORE, "enum_values": ["PAY"]}
    d = diff_packs(_pack([F_BEFORE]), _pack([after]))
    cells = d["apis_changed"]["ReqTransfer"]["fields_changed"]["ReqTransfer/Txn/@type"]
    assert cells["enum_values"] == {"from": ["PAY", "COLLECT"], "to": ["PAY"]}
    assert "mandatory" not in cells, "unchanged cells stay out of the diff"


def test_added_and_removed_fields_are_listed():
    d = diff_packs(_pack([F_BEFORE]),
                   _pack([{"path": "ReqTransfer/Txn/@newthing"}]))
    api = d["apis_changed"]["ReqTransfer"]
    assert api["fields_added"] == ["ReqTransfer/Txn/@newthing"]
    assert api["fields_removed"] == ["ReqTransfer/Txn/@type"]


def test_an_unchanged_api_is_absent_from_the_diff():
    d = diff_packs(_pack([F_BEFORE]), _pack([F_BEFORE]))
    assert d["apis_changed"] == {}
    assert d["apis_added"] == [] and d["apis_removed"] == []


def test_added_and_removed_apis():
    d = diff_packs(_pack([F_BEFORE], api="ReqTransfer"),
                   _pack([F_BEFORE], api="ReqDispute"))
    assert d["apis_added"] == ["ReqDispute"] and d["apis_removed"] == ["ReqTransfer"]


def test_scenarios_compare_by_identity_not_position():
    """Reordering is not a behaviour change; the same identity answering a
    different code is."""
    a = {"when": {"tc_id": "PR_1"}, "respond": {"rc": "00"}}
    b = {"when": {"variant_id": "v-1"}, "respond": {"rc": "ZM"}}
    d = diff_packs(_pack(scenarios=[a, b]), _pack(scenarios=[b, a]))
    assert d["scenarios_changed"] == {}
    assert d["scenarios_added"] == [] and d["scenarios_removed"] == []

    moved = diff_packs(_pack(scenarios=[a]),
                       _pack(scenarios=[{**a, "respond": {"rc": "ZF"}}]))
    assert moved["scenarios_changed"]["tc_id:PR_1"]["to"] == {"rc": "ZF"}


def test_a_first_publication_says_so_rather_than_faking_a_diff():
    d = diff_packs(None, _pack([F_BEFORE], scenarios=[
        {"when": {"tc_id": "PR_1"}, "respond": {"rc": "00"}}]))
    assert d["first_publication"] is True and d["baseline"] is None
    assert d["apis_added"] == ["ReqTransfer"], "API names as written, not lowercased keys"
    assert d["scenarios_added"] == ["tc_id:PR_1"]


def test_the_coverage_delta_travels_with_the_diff():
    d = diff_packs(_pack([F_BEFORE], coverage={"apis": 1, "gaps": []}),
                   _pack([F_BEFORE], coverage={"apis": 2, "gaps": ["x"]}))
    assert d["coverage"]["from"]["apis"] == 1
    assert d["coverage"]["to"]["gaps"] == ["x"]


def test_template_and_route_changes_surface():
    d = diff_packs(_pack([F_BEFORE]),
                   {**_pack([F_BEFORE]),
                    "apis": [{"api": "ReqTransfer", "wire_format": "xml",
                              "fields": [F_BEFORE],
                              "request_template": "<ReqTransfer/>"}]})
    assert d["apis_changed"]["ReqTransfer"]["other"]["request_template"]["to"] \
        == "<ReqTransfer/>"


def test_the_endpoint_diffs_the_chain_parent_by_default(db_session_packs):
    """Effective contracts, not layer fragments — a field the baseline
    supplies and the leaf does not override must not read as missing."""
    from types import SimpleNamespace

    from app.api import sim_packs as api
    from app.services.sim_packs.contract import SimPack, stamp
    from app.services.simulator import store

    db = db_session_packs
    base = stamp(SimPack.model_validate({
        "pack_ref": "baseline@1", "base_pack": "baseline@1",
        "apis": [{"api": "ReqTransfer", "fields": [F_BEFORE]}]}))
    store.save_draft(db, base)
    store.publish(db, "baseline@1", actor="op")

    leaf = stamp(SimPack.model_validate({
        "pack_ref": "chg@1", "base_pack": "baseline@1",
        "apis": [{"api": "ReqDispute", "fields": [{"path": "ReqDispute/@x"}]}]}))
    store.save_draft(db, leaf)

    out = api.diff_pack("chg@1", db, SimpleNamespace(username="op"))
    assert out["diff"]["baseline"] == "baseline@1"
    assert out["diff"]["apis_added"] == ["ReqDispute"]
    assert out["diff"]["apis_removed"] == [], \
        "the baseline's ReqTransfer survives the merge — not a removal"


@pytest.fixture
def db_session_packs():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401
    from app.core.database import Base
    from app.models.sim_pack import SimPackPublication, SimPackRecord

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[SimPackRecord.__table__,
                                             SimPackPublication.__table__])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
