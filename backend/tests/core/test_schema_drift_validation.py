# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""The database schema must not silently fall behind the ORM models.

A column rename that lands in the models and in a revision that is never applied
leaves the two out of step. SQLAlchemy puts every mapped column in its SELECT
list, so every read of an affected table raises UndefinedColumn — which the 500
handler collapses to "An internal error occurred". The backend still reports
itself healthy at boot: the A7 partner check notices it cannot query
PartnerAgent and, by design, only warns and skips.

`validate_schema_up_to_date` is the escalation that was missing. These tests
pin both halves of its contract — it must REFUSE THE BOOT when the schema is
behind the code, and it must STAY SILENT in the several legitimate states that
look superficially similar (a fresh database, a create_all test database, a
database that is briefly unreachable, a deliberate rollback). A validator that
blocks startup on any of those would be worse than the bug it replaces.
"""
from __future__ import annotations

import pytest

from app.core import startup_validation as sv


class _FakeRevision:
    def __init__(self, revision):
        self.revision = revision


class _FakeScript:
    """Stands in for alembic's ScriptDirectory.

    `known` is the linear revision history, oldest first. iterate_revisions
    mirrors alembic's contract: walk DOWN from upper, stopping before lower,
    and raise when a bound is not part of this checkout's history.
    """

    def __init__(self, known, heads):
        self.known, self.heads = list(known), list(heads)

    def get_heads(self):
        return list(self.heads)

    def iterate_revisions(self, upper, lower):
        for bound in (*upper, *lower):
            if bound not in self.known:
                raise ValueError(f"Can't locate revision identified by '{bound}'")
        hi = max(self.known.index(r) for r in upper)
        lo = max(self.known.index(r) for r in lower)
        return [_FakeRevision(r) for r in reversed(self.known[lo + 1:hi + 1])]


class _FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeEngine:
    def __init__(self, error=None):
        self._error = error

    def connect(self):
        if self._error:
            raise self._error
        return _FakeConnection()


@pytest.fixture
def scenario(monkeypatch):
    """Wire the validator to a synthetic schema state.

    db=None models a database with no alembic_version row at all.
    """
    def _apply(*, db, heads, known=("0139", "0140", "0141"), engine_error=None):
        monkeypatch.setattr(
            sv, "_alembic_script_directory", lambda: _FakeScript(known, heads),
        )
        import app.core.database as db_mod
        monkeypatch.setattr(db_mod, "engine", _FakeEngine(engine_error), raising=False)

        class _Ctx:
            @staticmethod
            def configure(conn):
                return type("C", (), {"get_current_heads": lambda self: tuple(db or ())})()

        import alembic.runtime.migration as mig
        monkeypatch.setattr(mig, "MigrationContext", _Ctx)
    return _apply


def test_silent_when_schema_matches_code(scenario):
    scenario(db=["0141"], heads=["0141"])
    assert sv.validate_schema_up_to_date() == []


def test_refuses_boot_when_db_is_behind_the_code(scenario):
    """The 0141 case: models renamed, migration written, never applied."""
    scenario(db=["0140"], heads=["0141"])
    issues = sv.validate_schema_up_to_date()

    assert len(issues) == 1
    assert issues[0].check == "db_schema_behind_code"
    assert issues[0].severity == "critical"
    # The message has to be actionable on its own — an operator reading it in a
    # crash log should not need to go and diff the versions directory.
    assert "0141" in issues[0].detail
    assert "alembic upgrade head" in issues[0].detail


def test_behind_by_several_revisions_lists_them_all(scenario):
    scenario(db=["0139"], heads=["0141"])
    detail = sv.validate_schema_up_to_date()[0].detail
    assert "0140" in detail and "0141" in detail


def test_critical_drift_aborts_run_all(scenario, monkeypatch):
    """Severity alone proves nothing — what matters is that it stops the boot."""
    scenario(db=["0140"], heads=["0141"])
    for name in (
        "_check_active_partners_have_hmac_secret", "validate_hostility_tier_config",
        "validate_precert_engine_tls", "validate_jwt_key_strength",
        "validate_reranker_backend", "validate_encryption_keys",
        "validate_hmac_fail_open", "validate_http_defaults",
    ):
        monkeypatch.setattr(sv, name, list)

    with pytest.raises(sv.StartupValidationError, match="db_schema_behind_code"):
        sv.run_all(fail_fast=True)


def test_fresh_database_is_not_a_failure(scenario):
    """No alembic_version row: a new DB, or one built by Base.metadata.create_all
    (how the suite makes its DBs). Blocking here would fail every such boot."""
    scenario(db=None, heads=["0141"])
    assert sv.validate_schema_up_to_date() == []


def test_unreachable_database_warns_but_does_not_block(scenario):
    """A container that starts before Postgres accepts connections is routine;
    this check cannot tell that apart from a real fault, so it must not be fatal."""
    scenario(db=["0141"], heads=["0141"], engine_error=OSError("connection refused"))
    issues = sv.validate_schema_up_to_date()

    assert [i.severity for i in issues] == ["warning"]
    assert issues[0].check == "schema_revision_unreadable"


def test_rollback_is_high_not_critical(scenario):
    """DB ahead of code is what a deliberate app rollback looks like. Refusing
    to boot would leave the operator unable to complete the rollback."""
    scenario(db=["0141"], heads=["0140"])
    issues = sv.validate_schema_up_to_date()

    assert [i.check for i in issues] == ["db_schema_ahead_of_code"]
    assert issues[0].severity == "high"


def test_revision_unknown_to_this_checkout_is_high(scenario):
    scenario(db=["0199"], heads=["0141"])
    issues = sv.validate_schema_up_to_date()

    assert [i.check for i in issues] == ["db_schema_revision_unknown"]
    assert issues[0].severity == "high"


def test_skips_cleanly_when_migration_scripts_are_absent(monkeypatch):
    """Packaged/native layouts that ship app/ without alembic/ must still boot."""
    monkeypatch.setattr(sv, "_alembic_script_directory", lambda: None)
    assert sv.validate_schema_up_to_date() == []


def test_locates_the_real_migration_scripts():
    """Guards the CWD-independent path derivation in _alembic_script_directory:
    if parents[2] ever stops being backend/, every check above silently turns
    into a no-op and this is the only thing that would notice."""
    script = sv._alembic_script_directory()

    assert script is not None, "alembic.ini/alembic must resolve from app/core/"
    heads = script.get_heads()
    assert heads and all(h.isdigit() for h in heads), heads
