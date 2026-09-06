# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Code-grounded feasibility (B1-v3) — validated against the ACTUAL repo checkout
(the clarification-stage pull), falling back to the cached index. Semantics:
absent + frozen wire → RED · absent + unfrozen → WARN (new unscoped build) ·
exists → informative reuse note · truly can't tell → silent (never red on a guess).
DB helpers are monkeypatched; _wire_entity_in_code is tested against a real tmp
git repo; the pure entity extraction is tested directly."""
import subprocess

import app.agents.upload_reconciler as UR
from app.agents.upload_reconciler import (assess_feasibility, _wire_names, _wire_entity_in_code,
                                          _wire_entity_paths, _checkout_heads)


class _CAQ:
    def __init__(self, ca): self._ca = ca
    def filter(self, *a, **k): return self
    def order_by(self, *a, **k): return self
    def first(self): return self._ca


class _DB:
    def __init__(self, ca): self._ca = ca
    def query(self, *a, **k): return _CAQ(self._ca)


def _patch(monkeypatch, *, frozen=None, repos=None, surface=None, in_code=False, indexed=None):
    monkeypatch.setattr(UR, "_frozen_schema_files", lambda db, cid: {"network-common"} if frozen is None else frozen)
    monkeypatch.setattr(UR, "_change_repo_ids", lambda db, cid: ["r1"] if repos is None else repos)
    monkeypatch.setattr(UR, "_plan_wire_surface", lambda db, cid: surface or set())
    monkeypatch.setattr(UR, "_analysis_checkouts", lambda db, cid, allow_clone=False: ["/fake/checkout"])
    monkeypatch.setattr(UR, "_wire_entity_in_code", lambda co, name: in_code)
    monkeypatch.setattr(UR, "_wire_entity_indexed", lambda db, rs, name: indexed)


_CONF = [{"id": "c1", "jurisdiction": "extends_plan",
          "evidence": {"item": "Doc introduces ReqSplitPay.xsd"}, "text": ""}]


# ── pure entity extraction ───────────────────────────────────────────────────
def test_wire_names_base_normalized():
    n = _wire_names({"evidence": {"item": "adds ReqSplitPay.xsd and RespCollect"}, "text": "new ReqFoo"})
    assert n == {"reqsplitpay", "respcollect", "reqfoo"}   # .xsd stripped; a schema + its message collapse


# ── _wire_entity_in_code against a REAL git repo ─────────────────────────────
def _mk_repo(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    (d / "ReqTransfer.xsd").write_text("<xs:schema/>", encoding="utf-8")
    (d / "Handler.java").write_text("case REQAUTHDETAILS: route();", encoding="utf-8")
    for cmd in (["git", "init", "-q"], ["git", "add", "."],
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"]):
        subprocess.run(cmd, cwd=str(d), check=True, capture_output=True)
    return d


def test_in_code_by_filename_and_content(tmp_path):
    d = _mk_repo(tmp_path)
    assert _wire_entity_in_code([d], "reqtransfer") is True            # filename match (ls-files)
    assert _wire_entity_in_code([d], "reqauthdetails") is True    # content match (git grep -i)
    assert _wire_entity_in_code([d], "reqtotallymadeup") is False # searched clean → definitively absent
    assert _wire_entity_in_code([], "reqtransfer") is None             # no checkout → can't tell
    assert _wire_entity_in_code([d], "") is None


# ── assess_feasibility orchestration ─────────────────────────────────────────
def test_reds_absent_from_code_and_frozen(monkeypatch):
    _patch(monkeypatch, in_code=False)         # definitively absent from the actual checkout
    out = assess_feasibility(_DB(object()), "cr", _CONF)
    assert out["c1"]["red"] == ["brd_wins"] and "reqsplitpay" in out["c1"]["evidence"]["absent"]
    assert out["c1"]["evidence"]["source"] == "checkout"


def test_warns_absent_pre_freeze(monkeypatch):
    # NOT conservative anymore: pre-freeze an absent entity is a WARN (new unscoped
    # build), not silence — buildable, so never red.
    _patch(monkeypatch, frozen=set(), in_code=False)
    out = assess_feasibility(_DB(object()), "cr", _CONF)
    assert out["c1"]["red"] == [] and out["c1"]["warn"] == ["brd_wins"]
    assert "new" in out["c1"]["reason"].lower()


def test_reuse_existing_notes_not_red(monkeypatch):
    _patch(monkeypatch, in_code=True)          # already in the code → reuse note, no red/warn
    out = assess_feasibility(_DB(object()), "cr", _CONF)
    assert out["c1"]["red"] == [] and out["c1"]["warn"] == []
    assert "reuse" in out["c1"]["reason"].lower()


def test_index_fallback_when_no_checkout(monkeypatch):
    # checkout unavailable → the cached graph decides; here it definitively lacks it.
    _patch(monkeypatch, in_code=None, indexed=False)
    out = assess_feasibility(_DB(object()), "cr", _CONF)
    assert out["c1"]["red"] == ["brd_wins"] and out["c1"]["evidence"]["source"] == "index"


def test_cant_tell_stays_silent(monkeypatch):
    _patch(monkeypatch, in_code=None, indexed=None)   # no checkout AND no graph → never red on a guess
    assert assess_feasibility(_DB(object()), "cr", _CONF) == {}


def test_in_plan_surface_not_flagged(monkeypatch):
    _patch(monkeypatch, surface={"reqsplitpay"}, in_code=False)   # plan already scopes it
    assert assess_feasibility(_DB(object()), "cr", _CONF) == {}


def test_no_plan_returns_empty(monkeypatch):
    _patch(monkeypatch, in_code=False)
    assert assess_feasibility(_DB(None), "cr", _CONF) == {}       # ca is None → nothing to ground on


def test_ignores_omissions(monkeypatch):
    _patch(monkeypatch, in_code=False)
    conf = [{"id": "c1", "jurisdiction": "drops_requirement",
             "evidence": {"item": "ReqX.xsd"}, "text": ""}]
    assert assess_feasibility(_DB(object()), "cr", conf) == {}


# ── S1 helpers: register schema surface + record the grounding commit ────────
def test_wire_entity_paths_schema_files_only(tmp_path):
    d = _mk_repo(tmp_path)                         # has ReqTransfer.xsd + Handler.java
    paths = _wire_entity_paths([d], "reqtransfer")
    assert (d.name, "ReqTransfer.xsd") in paths         # the .xsd is registered…
    assert not any(p.endswith(".java") for _, p in paths)   # …the .java is not (kind='xsd')
    assert _wire_entity_paths([d], "reqtotallyabsent") == []
    assert _wire_entity_paths([], "reqtransfer") == []  # no checkout → nothing


def test_checkout_heads_returns_sha(tmp_path):
    d = _mk_repo(tmp_path)
    heads = _checkout_heads([d])
    assert heads.get(d.name) and len(heads[d.name]) == 40   # a real HEAD sha
    assert _checkout_heads([]) == {}
