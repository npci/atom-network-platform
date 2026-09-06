# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Unit tests for the API Registry endpoints + hardening fixes.

Direct function-call style with a mocked DB (matches tests/api/test_remove_repo.py).
Covers the fixes from the adversarial QA pass:
- NUL bytes rejected at the request boundary (422, not a psycopg 500).
- Audit from/to values clipped so a large edit can't bloat constraint_sources.
- patch_message no-op guard (+ changed flag).
- patch_field pattern_rule audit + clear + pristine-no-lock.
- production-source per-repo baseline selection (clear same-repo only, 404 unknown).
- eval-check API-name regex covers Ack and stays in sync with the docgen sweep.

The DB-level concurrency guarantee (advisory lock + partial unique index) is a
Postgres integration concern verified by migration 0113 + the barrier test, not
reachable through this mock-DB unit harness.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.api_registry import (
    FieldPatch, MessagePatch, ProductionSourcePut,
    patch_message, patch_field, set_production_source,
    _clip_audit, _AUDIT_VALUE_CAP,
)

ADMIN = SimpleNamespace(email="admin@npci", id="admin-id")


def _fake_field(**over):
    base = dict(
        id="f1", parent_field_id=None, position=1, depth=1, tag_num="2.1.1",
        xml_tag="ver", is_attribute=True, xpath="ReqTransfer/Head/@ver",
        message_item=None, occurrence="1..1", datatype="Alphanumeric",
        length_rule=None, mandatory="Y", condition_text=None, rules_ref=None,
        pattern_rule=None, enum_values=None,
        constraint_sources={"xsd": {"pattern": None}},
        source="xsd_parse", status="active", updated_by=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _db_with(row):
    db = MagicMock()
    db.get.return_value = row
    return db


# ── NUL rejection (the 500 -> 422 fix) ───────────────────────────────────────
class TestNulRejection:
    @pytest.mark.parametrize("kwargs", [
        {"pattern_rule": "a\x00b"},
        {"message_item": "x\x00y"},
        {"rules_ref": "r\x00"},
        {"enum_values": ["ok", "bad\x00"]},
    ])
    def test_field_patch_rejects_nul(self, kwargs):
        with pytest.raises(ValidationError):
            FieldPatch(**kwargs)

    def test_message_patch_rejects_nul(self):
        with pytest.raises(ValidationError):
            MessagePatch(description="d\x00e")

    def test_production_source_rejects_nul(self):
        with pytest.raises(ValidationError):
            ProductionSourcePut(repo_id="id\x00")

    def test_clean_values_pass(self):
        FieldPatch(pattern_rule="^[A-Z]{1,6}$", enum_values=["A", "B"])  # no raise

    def test_over_width_still_rejected(self):
        with pytest.raises(ValidationError):
            FieldPatch(pattern_rule="x" * 501)


# ── audit value clipping (payload-growth fix) ────────────────────────────────
class TestClipAudit:
    def test_long_string_capped(self):
        out = _clip_audit("A" * 5000)
        assert out.startswith("A" * _AUDIT_VALUE_CAP)
        assert "+4500 chars" in out
        assert len(out) < 600  # far smaller than the 5000-char input

    def test_short_string_untouched(self):
        assert _clip_audit("short") == "short"

    def test_non_string_untouched(self):
        assert _clip_audit(None) is None
        assert _clip_audit(["a", "b"]) == ["a", "b"]


# ── message patch no-op guard (M4) ───────────────────────────────────────────
class TestPatchMessage:
    def test_noop_does_not_stamp(self):
        m = SimpleNamespace(description="D", updated_by=None)
        r = patch_message("id", MessagePatch(description="D"), _db_with(m), ADMIN)
        assert r == {"ok": True, "changed": False}
        assert m.updated_by is None

    def test_real_change_stamps(self):
        m = SimpleNamespace(description="D", updated_by=None)
        r = patch_message("id", MessagePatch(description="NEW"), _db_with(m), ADMIN)
        assert r["changed"] is True
        assert m.description == "NEW" and m.updated_by == ADMIN.email

    def test_empty_clears(self):
        m = SimpleNamespace(description="D", updated_by=None)
        patch_message("id", MessagePatch(description=""), _db_with(m), ADMIN)
        assert m.description is None

    def test_unknown_404(self):
        with pytest.raises(HTTPException) as e:
            patch_message("x", MessagePatch(description="d"), _db_with(None), ADMIN)
        assert e.value.status_code == 404


# ── field patch: pattern_rule audit + clip + pristine-no-lock (R1/R8) ─────────
class TestPatchField:
    def test_pattern_rule_sets_and_audits(self):
        f = _fake_field()
        out = patch_field("f1", FieldPatch(pattern_rule="^[0-9]{6}$"), _db_with(f), ADMIN)
        assert f.pattern_rule == "^[0-9]{6}$"
        assert f.updated_by == ADMIN.email
        last = f.constraint_sources["manual"][-1]
        assert last["changes"]["pattern_rule"] == {"from": None, "to": "^[0-9]{6}$"}
        assert out["pattern_rule"] == "^[0-9]{6}$" and out["edited"] is True

    def test_noop_does_not_lock(self):
        f = _fake_field(pattern_rule="X")
        patch_field("f1", FieldPatch(pattern_rule="X"), _db_with(f), ADMIN)
        assert f.updated_by is None
        assert "manual" not in (f.constraint_sources or {})

    def test_empty_clears_cell(self):
        f = _fake_field(rules_ref="R")
        patch_field("f1", FieldPatch(rules_ref=""), _db_with(f), ADMIN)
        assert f.rules_ref is None

    def test_audit_value_clipped_canonical_full(self):
        f = _fake_field()
        patch_field("f1", FieldPatch(message_item="A" * 5000), _db_with(f), ADMIN)
        assert len(f.message_item) == 5000  # canonical cell keeps the full value
        to = f.constraint_sources["manual"][-1]["changes"]["message_item"]["to"]
        assert len(to) < 600 and "+4500 chars" in to  # audit copy is clipped

    def test_unknown_404(self):
        with pytest.raises(HTTPException) as e:
            patch_field("x", FieldPatch(pattern_rule="a"), _db_with(None), ADMIN)
        assert e.value.status_code == 404


# ── production source: per-repo baseline selection ────────────────────────────
class _FakeQuery:
    """Evaluates the endpoint's real column criteria against namespace rows — the
    per-repo clear scope is a second .filter(), which a MagicMock chain can't
    exercise (it would return an unconfigured mock)."""

    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, crit):
        key = crit.left.name
        val = getattr(crit.right, "value", True)  # .is_(True) carries no bound value
        self._rows = [r for r in self._rows if getattr(r, key) == val]
        return self

    def all(self):
        return self._rows


class TestProductionSource:
    def _db(self, target, rows):
        db = MagicMock()
        db.bind.dialect.name = "sqlite"  # skip the Postgres advisory-lock branch
        db.get.return_value = target
        db.query.return_value = _FakeQuery(rows)
        return db

    def test_select_clears_previous_of_same_repo(self):
        prev = SimpleNamespace(is_registry_baseline=True, gitlab_repo="g/core", gitlab_branch="old")
        target = SimpleNamespace(id="t", is_registry_baseline=False, gitlab_repo="g/core", gitlab_branch="main")
        r = set_production_source(ProductionSourcePut(repo_id="t"), self._db(target, [prev, target]), ADMIN)
        assert prev.is_registry_baseline is False    # previous cleared
        assert target.is_registry_baseline is True   # new one set
        assert r["selected_id"] == "t"

    def test_select_keeps_other_repos_baseline(self):
        other = SimpleNamespace(id="o", is_registry_baseline=True, gitlab_repo="g/app", gitlab_branch="main")
        target = SimpleNamespace(id="t", is_registry_baseline=False, gitlab_repo="g/core", gitlab_branch="main")
        set_production_source(ProductionSourcePut(repo_id="t"), self._db(target, [other, target]), ADMIN)
        assert other.is_registry_baseline is True    # app baseline survives core selection
        assert target.is_registry_baseline is True

    def test_clear_scoped_to_repo(self):
        core = SimpleNamespace(id="c", is_registry_baseline=True, gitlab_repo="g/core", gitlab_branch="main")
        app = SimpleNamespace(id="a", is_registry_baseline=True, gitlab_repo="g/app", gitlab_branch="main")
        r = set_production_source(ProductionSourcePut(repo_id=None, gitlab_repo="g/core"),
                                  self._db(None, [core, app]), ADMIN)
        assert core.is_registry_baseline is False
        assert app.is_registry_baseline is True
        assert r["selected_id"] is None

    def test_clear_all_without_repo_scope(self):
        core = SimpleNamespace(id="c", is_registry_baseline=True, gitlab_repo="g/core", gitlab_branch="main")
        app = SimpleNamespace(id="a", is_registry_baseline=True, gitlab_repo="g/app", gitlab_branch="main")
        r = set_production_source(ProductionSourcePut(repo_id=None), self._db(None, [core, app]), ADMIN)
        assert core.is_registry_baseline is False
        assert app.is_registry_baseline is False
        assert r["selected_id"] is None

    def test_unknown_repo_404(self):
        with pytest.raises(HTTPException) as e:
            set_production_source(ProductionSourcePut(repo_id="nope"), self._db(None, []), ADMIN)
        assert e.value.status_code == 404


# ── harvest: empty workspace → 400 that names the root it searched ────────────
class TestHarvestDiscovery:
    def test_400_names_workspace_root(self, tmp_path, monkeypatch):
        from app.core.config import settings
        from app.api.api_registry import HarvestRequest, harvest_code

        monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path / "empty"))
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []  # no baselines
        with pytest.raises(HTTPException) as e:
            harvest_code(HarvestRequest(), db, ADMIN)
        assert e.value.status_code == 400
        assert str(tmp_path / "empty") in e.value.detail


# ── eval-check API-name regex covers Ack + parity with the docgen sweep ───────
class TestEvalRegexParity:
    """The doc-mention scan and the ingest sweep both read the ACTIVE pack's
    `message_name_pattern`; the UPI-shape assertions pin the UPI pack."""

    @staticmethod
    def _upi_pattern(monkeypatch):
        import pathlib
        monkeypatch.setenv("DOMAIN_PACK", str(
            pathlib.Path(__file__).resolve().parents[2] / "app" / "packs" / "network" / "network.yaml"))
        from app.core.domain import registry
        registry._load.cache_clear()
        from app.services.evaluation.deterministic import _api_name_in_doc_pattern
        return _api_name_in_doc_pattern()

    def test_ack_matched(self, monkeypatch):
        pat = self._upi_pattern(monkeypatch)
        assert pat.findall("errors ride on the Ack envelope") == ["Ack"]

    def test_word_boundary_excludes_longer_words(self, monkeypatch):
        pat = self._upi_pattern(monkeypatch)
        assert pat.findall("an Acknowledgement is sent and Requestor noted") == []

    def test_req_resp_still_matched(self, monkeypatch):
        pat = self._upi_pattern(monkeypatch)
        assert set(pat.findall("ReqChkTxn and RespTransfer")) == {"ReqChkTxn", "RespTransfer"}

    def test_parity_with_ingest_sweep(self, monkeypatch):
        """Both sides read the same pack key, so parity holds by construction;
        assert it anyway so a future hand-rolled regex on either side fails."""
        pat = self._upi_pattern(monkeypatch)
        from app.services.api_registry_ingest import derive_involved_api_names
        assert derive_involved_api_names("ReqChkTxn, RespTransfer and the Ack") == \
            pat.findall("ReqChkTxn, RespTransfer and the Ack")

    def test_no_pattern_means_no_mentions(self, monkeypatch, tmp_path):
        pack = tmp_path / "bare.yaml"
        pack.write_text("key: bare\n")
        monkeypatch.setenv("DOMAIN_PACK", str(pack))
        from app.core.domain import registry
        registry._load.cache_clear()
        try:
            from app.services.evaluation.deterministic import _api_name_in_doc_pattern
            from app.services.api_registry_ingest import derive_involved_api_names
            assert _api_name_in_doc_pattern() is None
            assert derive_involved_api_names("ReqChkTxn and RespTransfer") == []
        finally:
            registry._load.cache_clear()
