# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase 6 policy governance tests (reason, prod confirm, audit write)."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
from tests._optional_stubs import stub_jwt, stub_pgvector

stub_jwt()
stub_pgvector()

from app.api import eval as eval_api
from app.schemas.eval import EvalPolicyUpdateRequest
from app.services.evaluation.checkpoints import PolicyMode


class _FakeDb:
    def __init__(self):
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class TestPolicyGovernance:
    def test_policy_update_rejects_blank_reason(self):
        db = _FakeDb()
        body = EvalPolicyUpdateRequest(
            policies={"brd_to_tech_spec": "hard_gate"},
            reason="        ",
        )
        with pytest.raises(HTTPException) as exc:
            eval_api.update_eval_policies(
                body,
                db=db,
                current_user=SimpleNamespace(id="u-1", username="admin"),
            )
        assert exc.value.status_code == 400
        assert "reason" in str(exc.value.detail).lower()

    def test_policy_update_requires_strict_confirm_in_production(self, monkeypatch):
        db = _FakeDb()
        monkeypatch.setattr(eval_api.settings, "app_env", "production")

        body = EvalPolicyUpdateRequest(
            policies={"brd_to_tech_spec": "hard_gate"},
            reason="Enable hard gate for production drill.",
            confirm_production=False,
            confirm_text=None,
        )
        with pytest.raises(HTTPException) as exc:
            eval_api.update_eval_policies(
                body,
                db=db,
                current_user=SimpleNamespace(id="u-1", username="admin"),
            )
        assert exc.value.status_code == 400
        assert "confirm_production" in str(exc.value.detail)

        body2 = EvalPolicyUpdateRequest(
            policies={"brd_to_tech_spec": "hard_gate"},
            reason="Enable hard gate for production drill.",
            confirm_production=True,
            confirm_text="wrong text",
        )
        with pytest.raises(HTTPException) as exc2:
            eval_api.update_eval_policies(
                body2,
                db=db,
                current_user=SimpleNamespace(id="u-1", username="admin"),
            )
        assert exc2.value.status_code == 400
        assert "confirm_text mismatch" in str(exc2.value.detail)

    def test_policy_update_writes_audit_row(self, monkeypatch):
        db = _FakeDb()
        monkeypatch.setattr(eval_api.settings, "app_env", "development")
        monkeypatch.setattr(eval_api, "get_policy_mode", lambda *_a, **_k: PolicyMode.ADVISORY)
        monkeypatch.setattr(eval_api, "set_policy_mode", lambda *_a, **_k: None)

        body = EvalPolicyUpdateRequest(
            policies={"brd_to_tech_spec": "hard_gate"},
            reason="Switching to hard gate after advisory calibration passed.",
        )
        result = eval_api.update_eval_policies(
            body,
            db=db,
            current_user=SimpleNamespace(id="u-1", username="admin"),
        )

        assert db.commits == 1
        assert db.rollbacks == 0
        assert len(db.added) == 1
        row = db.added[0]
        assert row.checkpoint_id == "brd_to_tech_spec"
        assert row.old_policy_mode == "advisory"
        assert row.new_policy_mode == "hard_gate"
        assert row.actor_user_id == "u-1"
        assert row.actor_username == "admin"
        assert row.reason.startswith("Switching to hard gate")
        assert row.app_env == "development"
        assert result["updated"] == [{
            "checkpoint_id": "brd_to_tech_spec",
            "policy_mode": "hard_gate",
            "source": "config",
        }]
