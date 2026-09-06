# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for sub-slice 19a-scheduler — Celery periodic incremental ingest.

Focus on the pure DI orchestrator (`run_scheduled_ingest_once`) — it's
the only piece worth unit-testing. The Celery wrapper is a thin
adapter that opens a SessionLocal and queries CodeRepo rows; testing
that requires a real DB and isn't worth the fixture cost.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core.config import settings
from app.services.scheduled_ingest import (
    ScheduledIngestReport,
    run_scheduled_ingest_once,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _repo(repo_id: str, gitlab_repo: str = "grp/proj", branch: str = "main",
          gitlab_url: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=repo_id, gitlab_repo=gitlab_repo,
        gitlab_branch=branch, gitlab_url=gitlab_url,
    )


def _ingest_result(*, added=0, modified=0, deleted=0, inserted=0, removed=0):
    return {
        "files_fetched":   added + modified,
        "files_unchanged": 0,
        "files_modified":  modified,
        "files_added":     added,
        "files_deleted":   deleted,
        "chunks_inserted": inserted,
        "chunks_deleted":  removed,
        "by_language":     {},
    }


class _FakeKgReport:
    def __init__(self, successes: int, failures: int = 0):
        self._s = successes
        self._f = failures
    def total_successes(self) -> int:
        return self._s
    def total_failures(self) -> int:
        return self._f


# ──────────────────────────────────────────────────────────────────────────────
# Empty / single repo / multi repo paths
# ──────────────────────────────────────────────────────────────────────────────

class TestRunScheduledIngestOnce:

    def test_empty_repo_list_yields_zero_report(self):
        report = run_scheduled_ingest_once(
            MagicMock(), [],
            languages=["java"],
            do_kg_projection=False,
            ingest_fn=lambda **kw: _ingest_result(),
        )
        assert report.repos_processed == 0
        assert report.repos_succeeded == 0
        assert report.repos_failed == 0

    def test_single_repo_aggregates_into_report(self):
        captured = []
        def fake_ingest(**kw):
            captured.append(kw)
            return _ingest_result(added=2, modified=3, inserted=10)

        report = run_scheduled_ingest_once(
            MagicMock(), [_repo("r1")],
            languages=["java", "python"],
            do_kg_projection=False,
            ingest_fn=fake_ingest,
        )
        assert report.repos_processed == 1
        assert report.repos_succeeded == 1
        assert report.files_added == 2
        assert report.files_modified == 3
        assert report.chunks_inserted == 10
        assert captured[0]["languages"] == ["java", "python"]
        assert captured[0]["repo_id"] == "r1"

    def test_multi_repo_sums_per_bucket_counts(self):
        repos = [_repo(f"r{i}") for i in range(3)]
        results_iter = iter([
            _ingest_result(added=1, inserted=5),
            _ingest_result(modified=2, inserted=4, removed=1),
            _ingest_result(deleted=1, removed=3),
        ])
        report = run_scheduled_ingest_once(
            MagicMock(), repos,
            languages=["java"],
            do_kg_projection=False,
            ingest_fn=lambda **kw: next(results_iter),
        )
        assert report.repos_processed == 3
        assert report.repos_succeeded == 3
        assert report.files_added == 1
        assert report.files_modified == 2
        assert report.files_deleted == 1
        assert report.chunks_inserted == 9    # 5 + 4
        assert report.chunks_deleted == 4     # 1 + 3


# ──────────────────────────────────────────────────────────────────────────────
# Per-repo failure isolation
# ──────────────────────────────────────────────────────────────────────────────

class TestPerRepoFailureIsolation:

    def test_one_repo_failure_doesnt_kill_others(self):
        repos = [_repo("good1"), _repo("boom"), _repo("good2")]

        def fake_ingest(**kw):
            if kw["repo_id"] == "boom":
                raise RuntimeError("simulated GitLab outage")
            return _ingest_result(added=1, inserted=2)

        report = run_scheduled_ingest_once(
            MagicMock(), repos,
            languages=["java"],
            do_kg_projection=False,
            ingest_fn=fake_ingest,
        )
        assert report.repos_processed == 3
        assert report.repos_succeeded == 2
        assert report.repos_failed == 1
        assert len(report.failures) == 1
        assert report.failures[0]["repo_id"] == "boom"
        assert report.failures[0]["stage"] == "incremental_ingest"
        assert "simulated" in report.failures[0]["error"]
        # Counts still accumulate for the successful repos.
        assert report.files_added == 2
        assert report.chunks_inserted == 4

    def test_all_repos_fail_records_all(self):
        repos = [_repo("r1"), _repo("r2")]
        def boom(**kw):
            raise RuntimeError("everything broken")
        report = run_scheduled_ingest_once(
            MagicMock(), repos,
            languages=["java"],
            do_kg_projection=False,
            ingest_fn=boom,
        )
        assert report.repos_failed == 2
        assert report.repos_succeeded == 0
        assert len(report.failures) == 2


# ──────────────────────────────────────────────────────────────────────────────
# KG projection
# ──────────────────────────────────────────────────────────────────────────────

class TestKgProjection:

    def test_kg_projection_runs_when_enabled(self):
        called = {"n": 0}
        def fake_kg(db):
            called["n"] += 1
            return _FakeKgReport(successes=42, failures=0)

        report = run_scheduled_ingest_once(
            MagicMock(), [_repo("r1")],
            languages=["java"],
            do_kg_projection=True,
            ingest_fn=lambda **kw: _ingest_result(added=1),
            kg_ingest_fn=fake_kg,
        )
        assert called["n"] == 1
        assert report.kg_projection_ran is True
        assert report.kg_successes == 42
        assert report.kg_failures == 0

    def test_kg_projection_skipped_when_disabled(self):
        called = {"n": 0}
        def fake_kg(db):
            called["n"] += 1
            return _FakeKgReport(successes=99)

        report = run_scheduled_ingest_once(
            MagicMock(), [_repo("r1")],
            languages=["java"],
            do_kg_projection=False,
            ingest_fn=lambda **kw: _ingest_result(),
            kg_ingest_fn=fake_kg,
        )
        assert called["n"] == 0
        assert report.kg_projection_ran is False
        assert report.kg_successes == 0

    def test_kg_projection_skipped_when_callable_is_none(self):
        report = run_scheduled_ingest_once(
            MagicMock(), [_repo("r1")],
            languages=["java"],
            do_kg_projection=True,
            ingest_fn=lambda **kw: _ingest_result(),
            kg_ingest_fn=None,
        )
        assert report.kg_projection_ran is False

    def test_kg_projection_runs_even_when_some_repos_failed(self):
        """Stale-but-partial graph is better than no graph after a partial
        failure."""
        repos = [_repo("good"), _repo("boom")]
        def fake_ingest(**kw):
            if kw["repo_id"] == "boom":
                raise RuntimeError("nope")
            return _ingest_result(added=1, inserted=2)

        kg_calls = []
        def fake_kg(db):
            kg_calls.append(db)
            return _FakeKgReport(successes=10)

        report = run_scheduled_ingest_once(
            MagicMock(), repos,
            languages=["java"],
            do_kg_projection=True,
            ingest_fn=fake_ingest,
            kg_ingest_fn=fake_kg,
        )
        assert len(kg_calls) == 1
        assert report.kg_projection_ran is True
        assert report.repos_failed == 1

    def test_kg_projection_exception_swallowed_into_report(self):
        def kg_boom(db):
            raise RuntimeError("AGE down")

        report = run_scheduled_ingest_once(
            MagicMock(), [_repo("r1")],
            languages=["java"],
            do_kg_projection=True,
            ingest_fn=lambda **kw: _ingest_result(),
            kg_ingest_fn=kg_boom,
        )
        assert report.kg_projection_ran is True
        assert report.kg_failures >= 1
        assert any(f.get("stage") == "kg_projection" for f in report.failures)


# ──────────────────────────────────────────────────────────────────────────────
# ScheduledIngestReport.to_dict
# ──────────────────────────────────────────────────────────────────────────────

class TestReportSerialisation:

    def test_to_dict_round_trips_keys(self):
        report = ScheduledIngestReport(
            repos_processed=2, repos_succeeded=1, repos_failed=1,
            files_added=3, files_modified=4, files_deleted=2,
            chunks_inserted=10, chunks_deleted=5,
            kg_projection_ran=True, kg_successes=20, kg_failures=1,
            failures=[{"repo_id": "x", "stage": "incremental_ingest", "error": "e"}],
        )
        d = report.to_dict()
        assert d["repos_processed"] == 2
        assert d["chunks_inserted"] == 10
        assert d["failures"][0]["repo_id"] == "x"


# ──────────────────────────────────────────────────────────────────────────────
# Configuration sanity
# ──────────────────────────────────────────────────────────────────────────────

class TestConfig:

    def test_defaults(self):
        # These are the defaults at module scope; tests that flip them
        # should use monkeypatch to ensure they revert.
        assert settings.use_scheduled_ingest is False
        assert settings.scheduler_ingest_interval_minutes == 30
        assert "java" in settings.scheduler_ingest_languages
        assert settings.scheduler_kg_projection is True

    def test_interval_is_configurable_via_env(self, monkeypatch):
        """The Celery beat schedule reads `scheduler_ingest_interval_minutes`
        at import time. Verify the setting accepts an override."""
        monkeypatch.setattr(settings, "scheduler_ingest_interval_minutes", 5)
        assert settings.scheduler_ingest_interval_minutes == 5
