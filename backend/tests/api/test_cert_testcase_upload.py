# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Upload / apply / revert for cert_test_cases xlsx.

The whole feature relies on ONE invariant: the newest SUCCEEDED cert_test_cases
AgentJob wins as the source of truth for both:
  - GET /changes/{id}/product-kit/cert_test_cases/xlsx  (download)
  - build_kit_envelope in services/change_dispatch.py   (partner shipping)

These tests exercise the DB-side mechanic + the pure helpers (xlsx probe,
version bump). The FastAPI route wiring itself is covered by an end-to-end
smoke elsewhere; here we lock down the invariants.
"""

from __future__ import annotations

import io

import pytest


@pytest.fixture
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import app.models  # noqa: F401 — register models

    from app.core.database import Base
    from app.models.agent_job import AgentJob

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[AgentJob.__table__])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _make_xlsx_bytes(sheet_names=("Index", "Payer PSP")) -> bytes:
    """Emit a minimal but valid xlsx with the given sheet names."""
    import openpyxl

    wb = openpyxl.Workbook()
    # First sheet is auto-created; rename it and add the rest.
    wb.active.title = sheet_names[0]
    for n in sheet_names[1:]:
        wb.create_sheet(n)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ── _validate_xlsx_bytes ───────────────────────────────────────────────────


class TestValidateXlsxBytes:
    def test_accepts_npci_shaped_xlsx_with_zero_warnings(self):
        from app.excel_testcase_engine.api import _validate_xlsx_bytes

        data = _make_xlsx_bytes(("Index", "Payer PSP", "Payee PSP"))
        assert _validate_xlsx_bytes(data) == []

    def test_warns_on_non_npci_shape_but_does_not_reject(self):
        """A user uploading a workbook whose sheets don't look NPCI-shaped
        gets a warning — but the upload SUCCEEDS. Warn-not-reject is the
        product decision (see AskUserQuestion in the plan)."""
        from app.excel_testcase_engine.api import _validate_xlsx_bytes

        warnings = _validate_xlsx_bytes(_make_xlsx_bytes(("Data", "Meta")))
        assert warnings, "expected a shape-drift warning"
        # The warning text is domain-neutral now (the sheet-name allowlist is
        # pack-derived); assert the invariant phrase, not one domain's name.
        assert any("pack layout" in w for w in warnings)

    def test_rejects_garbage_bytes_with_415(self):
        from fastapi import HTTPException

        from app.excel_testcase_engine.api import _validate_xlsx_bytes

        with pytest.raises(HTTPException) as excinfo:
            _validate_xlsx_bytes(b"this is definitely not a workbook")
        assert excinfo.value.status_code == 415


# ── _bump_version ──────────────────────────────────────────────────────────


class TestBumpVersion:
    def test_no_prior_starts_at_two(self):
        """The engine-generated pack is implicitly v1, so the first override
        is v2. Guards against off-by-one when a reviewer relies on 'v2 ==
        first user edit'."""
        from app.excel_testcase_engine.api import _bump_version

        assert _bump_version(None) == 2
        assert _bump_version({}) == 2

    def test_monotonic(self):
        from app.excel_testcase_engine.api import _bump_version

        assert _bump_version({"version": 2}) == 3
        assert _bump_version({"version": 7}) == 8

    def test_ignores_garbage_prior(self):
        from app.excel_testcase_engine.api import _bump_version

        assert _bump_version({"version": "not-a-number"}) == 2
        assert _bump_version({"version": None}) == 2


# ── _insert_superseding_cert_job + _latest_succeeded_cert_job ─────────────


def _insert_engine_job(db, change_id, xlsx_path, version=1):
    """Simulate an engine-generated cert_test_cases job. Uses the raw
    AgentJob constructor to keep the fixture independent of the endpoint
    helper we're actually trying to test."""
    import uuid
    from datetime import datetime, timezone

    from app.models.agent_job import AgentJob, AgentJobStatus

    now = datetime.now(timezone.utc)
    job = AgentJob(
        id=uuid.uuid4().hex,
        change_request_id=change_id,
        module="product_kit",
        subtype="cert_test_cases",
        status=AgentJobStatus.SUCCEEDED,
        started_at=now, completed_at=now, updated_at=now,
        started_by_user_id="engine",
        current_stage="Completed",
        progress_pct=100,
        result_payload={"files": {"xlsx": xlsx_path}, "version": version},
        metadata_={},
    )
    db.add(job)
    db.commit()
    return job


class TestSupersedingJob:
    def test_upload_becomes_the_latest(self, db_session):
        """After _insert_superseding_cert_job runs, _latest_succeeded_cert_job
        returns the NEW row (with source=user_upload) — this is the whole
        feature in one assertion."""
        from app.excel_testcase_engine.api import (
            _insert_superseding_cert_job, _latest_succeeded_cert_job,
        )

        change_id = "cr_abc"
        original = _insert_engine_job(db_session, change_id, "/tmp/generated.xlsx")
        _insert_superseding_cert_job(
            db_session,
            change_id=change_id,
            user_id="u_pm",
            prev_job_id=original.id,
            result_payload={
                "files": {"xlsx": "/tmp/uploaded.xlsx"},
                "source": "user_upload",
                "version": 2,
            },
        )
        latest = _latest_succeeded_cert_job(db_session, change_id)
        assert latest is not None
        assert latest.result_payload["source"] == "user_upload"
        assert latest.result_payload["files"]["xlsx"] == "/tmp/uploaded.xlsx"
        assert latest.id != original.id

    def test_upload_is_scoped_per_change_request(self, db_session):
        """An upload on change A must NOT become the latest for change B."""
        from app.excel_testcase_engine.api import (
            _insert_superseding_cert_job, _latest_succeeded_cert_job,
        )

        job_a = _insert_engine_job(db_session, "cr_A", "/tmp/A.xlsx")
        job_b = _insert_engine_job(db_session, "cr_B", "/tmp/B.xlsx")
        _insert_superseding_cert_job(
            db_session, change_id="cr_A", user_id="u", prev_job_id=job_a.id,
            result_payload={
                "files": {"xlsx": "/tmp/A_edited.xlsx"},
                "source": "user_upload", "version": 2,
            },
        )
        # Change B is unaffected — still points at the original engine job.
        latest_b = _latest_succeeded_cert_job(db_session, "cr_B")
        assert latest_b is not None
        assert latest_b.id == job_b.id
        assert latest_b.result_payload["files"]["xlsx"] == "/tmp/B.xlsx"


# ── Revert walk (last non-user-upload wins) ────────────────────────────────


class TestRevertHistoryWalk:
    def test_revert_finds_last_engine_generated_across_multiple_uploads(self, db_session):
        """The revert route walks newest→oldest, skipping user_upload rows,
        and takes the first non-upload as the revert target. This test
        exercises the walk logic without hitting the actual FastAPI route."""
        from app.excel_testcase_engine.api import (
            _insert_superseding_cert_job, _latest_succeeded_cert_job,
        )
        from app.models.agent_job import AgentJob, AgentJobStatus

        change_id = "cr_multi"
        original = _insert_engine_job(
            db_session, change_id, "/tmp/original.xlsx", version=1,
        )
        _insert_superseding_cert_job(
            db_session, change_id=change_id, user_id="u",
            prev_job_id=original.id,
            result_payload={
                "files": {"xlsx": "/tmp/upload_v2.xlsx"},
                "source": "user_upload", "version": 2,
            },
        )
        _insert_superseding_cert_job(
            db_session, change_id=change_id, user_id="u",
            prev_job_id=None,
            result_payload={
                "files": {"xlsx": "/tmp/upload_v3.xlsx"},
                "source": "user_upload", "version": 3,
            },
        )
        # Emulate the revert route's walk: newest → oldest, skip user_upload.
        history = (
            db_session.query(AgentJob)
            .filter(
                AgentJob.change_request_id == change_id,
                AgentJob.module == "product_kit",
                AgentJob.subtype == "cert_test_cases",
                AgentJob.status == AgentJobStatus.SUCCEEDED,
            )
            .order_by(
                AgentJob.completed_at.desc().nullslast(),
                AgentJob.updated_at.desc(),
            )
            .all()
        )
        target = None
        for j in history:
            if (j.result_payload.get("source") or "") != "user_upload":
                target = j
                break
        assert target is not None
        assert target.id == original.id
        assert target.result_payload["files"]["xlsx"] == "/tmp/original.xlsx"

    def test_revert_returns_none_when_only_uploads_exist(self, db_session):
        """If somehow every cert_test_cases job is a user_upload (i.e. the
        engine has never generated one), the walk finds nothing — the route
        surfaces this as HTTP 409."""
        from app.excel_testcase_engine.api import _insert_superseding_cert_job
        from app.models.agent_job import AgentJob, AgentJobStatus

        change_id = "cr_all_uploads"
        # Two uploads back-to-back, no engine-generated predecessor.
        _insert_superseding_cert_job(
            db_session, change_id=change_id, user_id="u",
            prev_job_id=None,
            result_payload={
                "files": {"xlsx": "/tmp/u1.xlsx"},
                "source": "user_upload", "version": 2,
            },
        )
        _insert_superseding_cert_job(
            db_session, change_id=change_id, user_id="u",
            prev_job_id=None,
            result_payload={
                "files": {"xlsx": "/tmp/u2.xlsx"},
                "source": "user_upload", "version": 3,
            },
        )
        history = (
            db_session.query(AgentJob)
            .filter(
                AgentJob.change_request_id == change_id,
                AgentJob.module == "product_kit",
                AgentJob.subtype == "cert_test_cases",
                AgentJob.status == AgentJobStatus.SUCCEEDED,
            )
            .all()
        )
        non_upload = [
            j for j in history
            if (j.result_payload.get("source") or "") != "user_upload"
        ]
        assert non_upload == []


# ── M3: download endpoints enforce access control (not just JWT) ─────────────


class TestDownloadAccessControl:
    """The xlsx/json cert-workbook downloads must be gated to
    creator/admin/BRD-approver, the same rule upload/revert enforce — a cert
    pack can carry sensitive UPI flow detail, so any authenticated user must
    NOT be able to pull another change's workbook by change_id (IDOR)."""

    @pytest.fixture
    def acdb(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import app.models  # noqa: F401 — register models
        from app.core.database import Base
        from app.models.change_request import ChangeRequest
        from app.models.agent_job import AgentJob
        from app.models.approval import Approval
        from app.models.brd import BRD

        eng = create_engine("sqlite://")
        # Approval + BRD tables must exist: _access_check's approver-path query
        # counts approvals over the change's BRDs; without the tables it errors
        # instead of returning 0 (stranger → 403).
        Base.metadata.create_all(eng, tables=[
            ChangeRequest.__table__, AgentJob.__table__,
            Approval.__table__, BRD.__table__,
        ])
        s = sessionmaker(bind=eng)()
        s.add(ChangeRequest(id="cr1", initial_prompt="p", created_by="u_owner"))
        s.commit()
        yield s
        s.close()

    @staticmethod
    def _user(uid, role=None):
        from types import SimpleNamespace
        from app.models.user import UserRole
        return SimpleNamespace(id=uid, role=role or UserRole.PRODUCT_MANAGER)

    def test_owner_allowed(self, acdb):
        from app.excel_testcase_engine.api import _access_check
        assert _access_check(acdb, "cr1", self._user("u_owner")).id == "cr1"

    def test_admin_allowed(self, acdb):
        from app.excel_testcase_engine.api import _access_check
        from app.models.user import UserRole
        assert _access_check(acdb, "cr1", self._user("u_other", UserRole.ADMIN)).id == "cr1"

    def test_stranger_denied_403(self, acdb):
        from app.excel_testcase_engine.api import _access_check
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as e:
            _access_check(acdb, "cr1", self._user("u_stranger"))
        assert e.value.status_code == 403

    def test_serve_companion_denies_stranger_before_serving(self, acdb):
        # The whole point of M3: the download path must _access_check FIRST, so a
        # stranger is rejected with 403 rather than handed another change's xlsx.
        from app.excel_testcase_engine.api import _serve_companion, XLSX_MIME
        from fastapi import HTTPException
        _insert_engine_job(acdb, "cr1", "/tmp/x.xlsx")
        with pytest.raises(HTTPException) as e:
            _serve_companion(acdb, "cr1", self._user("u_stranger"),
                             kind="xlsx", mime=XLSX_MIME, ext="xlsx")
        assert e.value.status_code == 403

    def test_serve_companion_allows_owner(self, acdb):
        # Owner passes the gate; the 410/missing-file path past it is fine (the
        # file isn't on disk in the test) — we only assert access isn't the blocker.
        from app.excel_testcase_engine.api import _serve_companion, XLSX_MIME
        from fastapi import HTTPException
        _insert_engine_job(acdb, "cr1", "/tmp/does-not-exist.xlsx")
        with pytest.raises(HTTPException) as e:
            _serve_companion(acdb, "cr1", self._user("u_owner"),
                             kind="xlsx", mime=XLSX_MIME, ext="xlsx")
        assert e.value.status_code != 403   # passed access; fails later on missing file
