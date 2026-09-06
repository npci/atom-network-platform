# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""SIM-2: pack store, chain resolution, and the binding rule.

THE rule under test: `?pack=` present-but-unknown (or withdrawn) REFUSES —
never a silent fallback to baseline. A silent fallback certifies a bank
against the old contract while the report says the new one.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.sim_packs.contract import SimPack, stamp
from app.services.simulator import resolver, store
from app.services.simulator.engine import CAPABILITIES, ENGINE_VERSION


@pytest.fixture
def db_session():
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


def _pack(ref="chg-1@1", base="baseline@1", apis=("ReqTransfer",), rc="00",
          scenario_variant=None, **extra) -> SimPack:
    scenarios = []
    if scenario_variant is not None:
        scenarios = [{"when": {"variant_id": scenario_variant},
                      "respond": {"rc": rc}}]
    return stamp(SimPack.model_validate({
        "pack_ref": ref, "base_pack": base,
        "apis": [{"api": a, "fields": [{"path": f"{a}/Head/@ver",
                                        "mandatory": "Y"}]} for a in apis],
        "scenarios": scenarios,
        **extra,
    }))


def _baseline(db, ref="baseline@1", **kw):
    """A root pack declares itself its own base (the root marker)."""
    row = store.save_draft(db, _pack(ref=ref, base=ref, **kw))
    return store.publish(db, ref)


# ── store: immutability at the edge ──────────────────────────────────────────

def test_a_ref_is_stored_once_a_change_is_a_new_revision(db_session):
    _baseline(db_session)
    store.save_draft(db_session, _pack())
    with pytest.raises(store.StoreError, match="immutable"):
        store.save_draft(db_session, _pack(apis=("ReqTransfer", "ReqDispute")))


def test_a_chain_needs_its_root_stored_first(db_session):
    with pytest.raises(store.StoreError, match="chain root"):
        store.save_draft(db_session, _pack(base="baseline@nope"))


def test_publish_gate_names_the_missing_capability(db_session):
    _baseline(db_session)
    store.save_draft(db_session, _pack(requires=["sig.v9"]))
    with pytest.raises(store.StoreError, match="sig.v9"):
        store.publish(db_session, "chg-1@1")


def test_publish_gate_refuses_a_future_engine_min(db_session):
    _baseline(db_session)
    store.save_draft(db_session, _pack(engine_min="99.0"))
    with pytest.raises(store.StoreError, match="99.0"):
        store.publish(db_session, "chg-1@1")


def test_publish_is_idempotent_and_recorded(db_session):
    from app.models.sim_pack import SimPackPublication

    _baseline(db_session)
    row = store.save_draft(db_session, _pack(apis=("ReqTransfer", "ReqRefund")))
    store.publish(db_session, "chg-1@1")
    store.publish(db_session, "chg-1@1")
    pubs = db_session.query(SimPackPublication).filter_by(pack_ref="chg-1@1").all()
    assert len(pubs) == 1 and pubs[0].echoed_pack_id == row.pack_id


# ── NET-F24: a pack identical to its base certifies nothing ──────────────────

def test_publish_refuses_a_pack_identical_to_its_base(db_session):
    """Found live against the partner platform: a change pack whose contract
    hashes identically to its base was accepted and publishable. A round on it
    grades the partner against BASELINE content under a change's label — the
    "certified against baseline" failure, reached through the builder rather
    than through query-string normalisation.
    """
    base = _baseline(db_session)
    # Same apis, same scenarios => same canonical contract => same pack_id.
    # pack_ref / base_pack / generated_at are excluded from the hash by design,
    # which is exactly why a metadata-only revision must land here.
    twin = store.save_draft(db_session, _pack(ref="chg-1@1", base="baseline@1"))
    assert twin.pack_id == base.pack_id, "precondition: the contracts must match"

    with pytest.raises(store.StoreError, match="certifies nothing"):
        store.publish(db_session, "chg-1@1")
    assert store.get(db_session, "chg-1@1").status == "draft", \
        "a refused publish must not leave the pack published"


def test_publish_still_accepts_a_pack_that_genuinely_differs(db_session):
    """The guard must not block real change packs — the failure mode of an
    over-eager fix is a platform that can certify nothing at all."""
    _baseline(db_session)
    real = store.save_draft(
        db_session, _pack(ref="chg-2@1", base="baseline@1", apis=("ReqTransfer", "ReqRefund")))
    assert real.pack_id != store.get(db_session, "baseline@1").pack_id
    assert store.publish(db_session, "chg-2@1").status == "published"


def test_a_root_pack_is_its_own_base_and_still_publishes(db_session):
    """A root declares itself its own base (`_baseline`'s root marker), so a
    naive `base_pack_ref == pack_ref` comparison would refuse every baseline
    and leave nothing to layer on. The guard compares CONTENT via pack_id and
    resolves the base row, so the root is unaffected."""
    root = _baseline(db_session, ref="baseline@solo")
    assert root.status == "published"


# ── resolution: the rule that must never soften ──────────────────────────────

def test_an_unknown_pack_refuses_never_falls_back(db_session):
    _baseline(db_session)
    with pytest.raises(resolver.UnknownPackError, match="unknown pack"):
        resolver.resolve(db_session, "CHG-9999@1")


def test_a_withdrawn_pack_refuses_even_when_previously_resolved(db_session):
    _baseline(db_session)
    store.save_draft(db_session, _pack(apis=("ReqTransfer", "ReqRefund")))
    store.publish(db_session, "chg-1@1")
    resolver.resolve(db_session, "chg-1@1")          # warm the cache
    store.withdraw(db_session, "chg-1@1")
    with pytest.raises(resolver.UnknownPackError, match="withdrawn"):
        resolver.resolve(db_session, "chg-1@1")


def test_a_draft_resolves_only_for_the_review_view(db_session):
    _baseline(db_session)
    store.save_draft(db_session, _pack())
    with pytest.raises(resolver.UnknownPackError, match="not published"):
        resolver.resolve(db_session, "chg-1@1")
    assert resolver.resolve(db_session, "chg-1@1",
                            include_draft=True).pack_ref == "chg-1@1"


def test_two_refs_with_identical_content_each_resolve_to_their_own_ref(db_session):
    """Found live: a round bound to `@r10` was graded — correctly — but every
    record of it named `@r5`.

    `pack_ref` is excluded from the content hash on purpose ("identical
    behaviour republished under a new revision keeps its address"), so a re-run
    round whose delta has not changed shares its predecessor's `pack_id`. When
    the resolve cache was keyed on content alone, the second ref hit the first
    one's entry and inherited its `pack_ref` — which is what `X-Sim-Pack`
    reports, what the rig logs, and what both platforms store as the contract
    that certified the partner. Worse, WHICH ref got named depended on cache
    warmth, so the same request could be evidenced two different ways.
    """
    _baseline(db_session)
    for ref in ("chg-1@r5", "chg-1@r10"):
        store.save_draft(db_session, _pack(ref=ref, apis=("ReqTransfer", "ReqRefund")))
        store.publish(db_session, ref)

    r5 = resolver.resolve(db_session, "chg-1@r5")       # warms the cache first
    r10 = resolver.resolve(db_session, "chg-1@r10")

    # Same behaviour, same address — that part is the design.
    assert r5.pack_id == r10.pack_id
    # ...but each names the contract ref its caller actually bound to.
    assert r5.pack_ref == "chg-1@r5"
    assert r10.pack_ref == "chg-1@r10"


def test_absent_pack_param_resolves_the_active_baseline(db_session):
    _baseline(db_session)
    assert resolver.resolve_request(db_session, None).pack_ref == "baseline@1"


def test_no_baseline_published_is_the_stated_pre_pack_world(db_session):
    assert resolver.resolve_request(db_session, None) is None


# ── the chain merge ──────────────────────────────────────────────────────────

def test_leaf_api_entry_replaces_the_parents_whole_entry(db_session):
    _baseline(db_session, apis=("ReqTransfer", "ReqBalance"))
    leaf = stamp(SimPack.model_validate({
        "pack_ref": "chg-1@1", "base_pack": "baseline@1",
        "apis": [{"api": "ReqTransfer",
                  "fields": [{"path": "ReqTransfer/NEW/@field", "mandatory": "Y"}]}],
    }))
    store.save_draft(db_session, leaf)
    store.publish(db_session, "chg-1@1")

    resolved = resolver.resolve(db_session, "chg-1@1")
    assert resolved.chain == ["baseline@1", "chg-1@1"]
    assert resolved.apis["reqtransfer"]["fields"][0]["path"] == "ReqTransfer/NEW/@field", \
        "whole-entry replacement — a half-merged field table is a third " \
        "contract nobody wrote"
    assert "reqbalance" in resolved.apis, "untouched parent APIs survive"


def test_leaf_scenarios_match_before_parent_scenarios(db_session):
    _baseline(db_session, scenario_variant="v-1", rc="00")
    store.save_draft(db_session, _pack(scenario_variant="v-1", rc="ZM"))
    store.publish(db_session, "chg-1@1")
    resolved = resolver.resolve(db_session, "chg-1@1")
    assert resolved.scenarios[0]["respond"]["rc"] == "ZM"
    assert resolved.scenarios[-1]["respond"]["rc"] == "00"


# ── the API edge ─────────────────────────────────────────────────────────────

def test_endpoints_store_publish_and_serve_the_effective_view(db_session):
    from fastapi import HTTPException

    from app.api import sim_packs as api

    user = SimpleNamespace(username="op")
    base = _pack(ref="baseline@1", base="baseline@1")
    api.store_pack(base.canonical_dict(), db_session, user)
    api.publish_pack("baseline@1", db_session, user)
    api.store_pack(_pack(requires=["sig.v9"]).canonical_dict(), db_session, user)
    with pytest.raises(HTTPException) as exc:
        api.publish_pack("chg-1@1", db_session, user)
    assert exc.value.status_code == 422 and "sig.v9" in exc.value.detail

    view = api.effective_pack("chg-1@1", db_session, user)
    assert view["chain"] == ["baseline@1", "chg-1@1"]

    caps = api.capabilities(user)
    assert caps["engine_version"] == ENGINE_VERSION
    assert set(caps["capabilities"]) == CAPABILITIES


# ── SIM-5: the build-from-change publish path ────────────────────────────────

def _registry_tables(db):
    from app.core.database import Base
    from app.models.api_registry import ApiField, ApiMessage
    from app.models.phase_c import CertRequestVariant

    Base.metadata.create_all(db.get_bind(), tables=[
        ApiMessage.__table__, ApiField.__table__, CertRequestVariant.__table__])


def test_build_endpoint_refuses_without_a_baseline(db_session):
    from fastapi import HTTPException

    from app.api import sim_packs as api

    _registry_tables(db_session)
    with pytest.raises(HTTPException) as exc:
        api.build_from_change({"change_id": "chg-9"}, db_session,
                              SimpleNamespace(username="op"))
    assert exc.value.status_code == 409


def test_build_endpoint_states_an_empty_delta(db_session):
    from fastapi import HTTPException

    from app.api import sim_packs as api

    _registry_tables(db_session)
    _baseline(db_session)
    with pytest.raises(HTTPException) as exc:
        api.build_from_change({"change_id": "chg-9"}, db_session,
                              SimpleNamespace(username="op"))
    assert exc.value.status_code == 422
    assert exc.value.detail["error"] == "empty_delta"


def test_build_endpoint_drafts_a_reviewable_pack(db_session):
    from app.api import sim_packs as api
    from app.models.api_registry import ApiMessage
    from app.models.base import generate_uuid

    _registry_tables(db_session)
    _baseline(db_session)
    db_session.add(ApiMessage(id=generate_uuid(), api_name="ReqDispute",
                              direction="request", sample_xml="<ReqDispute/>",
                              introduced_by_change_id="chg-9"))
    db_session.commit()

    out = api.build_from_change({"change_id": "chg-9", "revision": 2},
                                db_session, SimpleNamespace(username="op"))
    assert out["pack_ref"] == "chg-9@2" and out["status"] == "draft"
    assert out["base_pack_ref"] == "baseline@1"
    assert any("no route declared" in g for g in out["review"]["summary_gaps"]), \
        "the review leads with what is NOT covered"


# ── S-5: publishing is authenticated AND audited ─────────────────────────────

def test_publish_records_who_did_it(db_session):
    """A pack changes what 'certified' means, so the actor is evidence."""
    from app.models.sim_pack import SimPackPublication

    _baseline(db_session)
    store.save_draft(db_session, _pack(apis=("ReqTransfer", "ReqRefund")))
    store.publish(db_session, "chg-1@1", actor="alice")
    pub = db_session.query(SimPackPublication).filter_by(
        pack_ref="chg-1@1").one()
    assert pub.published_by == "alice"


def test_mutating_routes_are_admin_gated_reads_are_not():
    """The admin dependency is what AdminActionAuditMiddleware keys on —
    CurrentUser would leave publishing unaudited by construction."""
    import inspect

    from app.api import sim_packs as api

    def _dep(fn):
        return {str(p.annotation) for p in
                inspect.signature(fn).parameters.values()}

    for mutating in (api.store_pack, api.publish_pack, api.withdraw_pack,
                     api.build_from_change):
        assert any("AdminUser" in a for a in _dep(mutating)), mutating.__name__
    for read in (api.get_pack, api.effective_pack, api.list_packs):
        assert any("CurrentUser" in a for a in _dep(read)), read.__name__
