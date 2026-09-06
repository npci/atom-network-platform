# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""`AgentJob.error_message` is client-visible — it must not carry SQL text.

Found during the adversarial re-review of the Checkmarx SCR triage. The original
triage classified exception handlers by where the exception appears *lexically*,
so it only saw `HTTPException(detail=...)`-shaped leaks. It could not see the
store-then-serve shape, which is the higher-volume one::

    except Exception as exc:
        job_registry.fail_job(db, job_id, error=str(exc))   # ~20 call sites
            -> AgentJob.error_message          (DB column)
                -> AgentJob.to_dict()          ("error_message": ...)
                    -> GET /api/jobs/{job_id}  (api/jobs.py)
                        -> frontend ProductKit.jsx renders it

`fail_job` now scrubs at that chokepoint, so a new caller cannot reintroduce the
leak by forgetting to sanitise. These tests pin both directions: operator-written
progress text survives, machine-generated internals do not.
"""
import pytest

from app.core.error_taxonomy import client_safe_message


# ── the scrubber's contract ──────────────────────────────────────────────────

@pytest.mark.parametrize("keep", [
    "Indexing failed",
    "Workbook generation failed",
    "bank has not declared this case ready",
    "Section 'Overview' not found in document",
    "pipeline failed",
])
def test_human_written_failure_text_survives(keep):
    """If scrubbing were indiscriminate every failed job would read the same
    thing, the UI would become useless, and someone would revert the fix."""
    assert client_safe_message(keep) == keep


@pytest.mark.parametrize("leaky", [
    '(psycopg2.errors.ForeignKeyViolation) update or delete on table '
    '"change_requests" violates foreign key constraint '
    '"agent_jobs_change_request_id_fkey"\n[SQL: DELETE FROM change_requests]',
    "[SQL: SELECT * FROM users WHERE id = %(id)s]",
    "[parameters: {'id': 'abc-123'}]",
    "Traceback (most recent call last):\n  File \"/srv/app/x.py\", line 1",
    "sqlalchemy.exc.OperationalError: could not connect",
    "/usr/lib/python3.11/site-packages/sqlalchemy/engine/base.py",
])
def test_machine_generated_text_is_replaced(leaky):
    out = client_safe_message(leaky)
    assert out == "an internal processing error occurred"
    for marker in ("SQL:", "psycopg2", "sqlalchemy", "Traceback", "site-packages",
                   "change_requests", "parameters:"):
        assert marker not in out


def test_scrub_happens_before_truncation():
    """A leak marker sitting past the 4096-char truncation point must still be
    detected — scrubbing after truncation would slice it out of view and store
    the leading 4096 characters of a traceback."""
    payload = "x" * 5000 + " [SQL: DELETE FROM change_requests]"
    assert client_safe_message(payload) == "an internal processing error occurred"


# ── the chokepoint actually applies it ───────────────────────────────────────

def _job_registry():
    """Import `app.services.job_registry`, skipping when its optional runtime
    dependencies are absent.

    `pytest.importorskip` only skips on ModuleNotFoundError for the named
    module. This import chain also pulls in `pydantic[email]`, which re-raises
    its missing dependency as a plain ImportError naming a different package —
    and whether the chain dies on the DB driver or on that depends on which
    other tests have already run. Catching ImportError as a family keeps the
    outcome the same in every ordering.
    """
    try:
        import app.services.job_registry as job_registry
    except ImportError as exc:
        pytest.skip(f"job_registry needs a dependency not installed here: {exc}")
    return job_registry


def test_fail_job_scrubs_before_persisting(monkeypatch):
    """Exercises `fail_job` itself, not just the helper, so the wiring is pinned.

    The real function needs a Session and the ORM; a light fake stands in for the
    row so the test states the property (what lands in `error_message`) without a
    database.
    """
    job_registry = _job_registry()

    class _Job:
        id = "job-1"
        module = "docgen"
        status = None
        error_message = None
        current_stage = None
        completed_at = None
        updated_at = None

    class _Query:
        def __init__(self, job): self._job = job
        def filter(self, *_a): return self
        def first(self): return self._job

    class _DB:
        def __init__(self, job): self._job = job
        def query(self, *_a): return _Query(self._job)
        def commit(self): pass
        def rollback(self): pass

    job = _Job()
    monkeypatch.setattr(job_registry, "_get_redis", lambda: None)

    job_registry.fail_job(
        _DB(job), "job-1",
        error='(psycopg2.errors.ForeignKeyViolation) on table "change_requests" '
              '[SQL: DELETE FROM change_requests WHERE id = %(id)s]',
        final_stage="Indexing failed",
    )

    assert job.error_message == "an internal processing error occurred"
    for marker in ("SQL:", "psycopg2", "change_requests", "DELETE FROM"):
        assert marker not in (job.error_message or "")
    # The operator-facing stage label is unrelated to the exception text and
    # must not be collateral damage.
    assert job.current_stage == "Indexing failed"


def test_fail_job_keeps_human_error_text(monkeypatch):
    job_registry = _job_registry()

    class _Job:
        id = "job-2"
        module = "docgen"
        status = None
        error_message = None
        current_stage = None
        completed_at = None
        updated_at = None

    class _Query:
        def __init__(self, job): self._job = job
        def filter(self, *_a): return self
        def first(self): return self._job

    class _DB:
        def __init__(self, job): self._job = job
        def query(self, *_a): return _Query(self._job)
        def commit(self): pass
        def rollback(self): pass

    job = _Job()
    monkeypatch.setattr(job_registry, "_get_redis", lambda: None)
    job_registry.fail_job(_DB(job), "job-2", error="no approved Phase-A run")
    assert job.error_message == "no approved Phase-A run"
