# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Architecture Finding #8 — coldstore read-through + source-column nulling.

Finding #8 ("No Data Tiering for Large Payloads") was previously only half
closed: compression wrote a gzip copy and a manifest row, but never nulled the
source column, so the database never actually shrank. These tests cover the two
pieces that finish it, and the invariant that makes it safe:

  * **Space is genuinely reclaimed.** Asserted with raw SQL, bypassing the ORM,
    so a passing test cannot be satisfied by rehydration hiding a non-NULL
    column.
  * **Reads are unaffected.** A nulled row read through the ORM returns
    byte-identical content, which is what lets all 114 audited read sites stay
    untouched.
  * **Nothing is destroyed without proof.** Nulling is refused when the cold copy
    mismatches, is missing, or is corrupt.

The last group is the important one. The failure mode being guarded against is
permanent data loss, so each guard is asserted by confirming the ORIGINAL
CONTENT IS STILL THERE, not merely that a counter reported zero.
"""
import gzip
import sys
import types

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine, text                      # noqa: E402
from sqlalchemy.orm import DeclarativeBase, sessionmaker         # noqa: E402


class _Base(DeclarativeBase):
    pass


def _import_models():
    """Import the models this suite needs, tolerating this environment's
    inability to import the real `app.core.database`.

    `app/models/*.py` do `from app.core.database import Base`, and the real
    `core/database.py` calls `create_engine` with Postgres-only pool kwargs
    (`max_overflow`, `pool_timeout`) at import time, which SQLite rejects. So a
    stub must exist BEFORE the model import — but only transiently.

    An earlier attempt installed the stub permanently at module scope. That made
    these tests pass, and simultaneously changed behaviour for ~19 UNRELATED
    tests elsewhere in the suite (e.g. tests/api/test_cert_testcase_upload.py),
    which had been erroring on the same limitation and suddenly began running
    against a stub engine. Leaking test scaffolding into other tests' behaviour
    is worse than the problem it solves, so the stub is now REMOVED again as
    soon as the models are bound.

    Returns `(Base, TechSpec, A2AMessage, ArtifactColdStorage)`.
    """
    real = sys.modules.get("app.core.database")
    installed_stub = False
    if real is None or not hasattr(real, "Base"):
        try:
            import app.core.database  # noqa: F401
        except Exception:  # noqa: BLE001 — the create_engine limitation above
            stub = types.ModuleType("app.core.database")
            stub.engine = None
            stub.SessionLocal = None
            stub.Base = _Base
            sys.modules["app.core.database"] = stub
            installed_stub = True
    try:
        from app.models.artifact_cold_storage import ArtifactColdStorage
        from app.models.phase_c import A2AMessage
        from app.models.tech_spec import TechSpec
        base = sys.modules["app.core.database"].Base
        return base, TechSpec, A2AMessage, ArtifactColdStorage
    finally:
        if installed_stub:
            # Restore the world exactly as it was, so no other test's import of
            # app.core.database is affected by this module having been collected.
            sys.modules.pop("app.core.database", None)


_MODEL_BASE, _TechSpec, _A2AMessage, _ArtifactColdStorage = _import_models()


def _install_db_stub(engine, Session, monkeypatch):
    """Make `app.core.database` usable in this environment, and return the
    declarative `Base` the models are actually mapped against.

    Why this is needed at all: this repo's `core/database.py` calls
    `create_engine` with Postgres-only pool kwargs (`max_overflow`,
    `pool_timeout`) at IMPORT time, which the SQLite dialect rejects. That is a
    pre-existing environment limitation (every `workspace_local`/DB-touching
    test here hits it), not something these tests introduce.

    Two cases, and getting them confused is what made these tests pass alone but
    error in the full suite:

    * The real module is ALREADY imported (some earlier test pulled it in). Then
      `app.models.*` are mapped against its `Base`, so we must reuse that Base
      and only redirect the engine/session.
    * It is NOT yet imported. A bare `monkeypatch.setitem` of a stub is not
      enough either, because importing `app.models.artifact_cold_storage` runs
      `from app.core.database import Base` — which, if our stub is a plain
      module lacking anything the models need, or if the import machinery
      reaches the real file first, executes the failing `create_engine`. So the
      stub is installed BEFORE any model import and carries a real declarative
      Base for the models to bind to.
    """
    dbmod = sys.modules.get("app.core.database")
    if dbmod is not None:
        monkeypatch.setattr(dbmod, "engine", engine, raising=False)
        monkeypatch.setattr(dbmod, "SessionLocal", Session, raising=False)
    return _MODEL_BASE


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A real in-memory SQLite DB plus a real coldstore directory."""
    engine = create_engine("sqlite:///:memory:")
    Session = sessionmaker(bind=engine)
    base = _install_db_stub(engine, Session, monkeypatch)

    from app.core.config import settings
    ArtifactColdStorage, TechSpec = _ArtifactColdStorage, _TechSpec

    cold = tmp_path / "coldstore"
    cold.mkdir()
    monkeypatch.setitem(settings.__dict__, "artifact_coldstore_dir", str(cold))
    monkeypatch.setitem(settings.__dict__, "artifact_coldstore_null_source", True)
    monkeypatch.setitem(settings.__dict__, "artifact_coldstore_read_through", True)

    base.metadata.create_all(
        engine, tables=[TechSpec.__table__, ArtifactColdStorage.__table__])

    from app.services.artifact_coldstore_read import register_coldstore_read_through
    register_coldstore_read_through()

    return types.SimpleNamespace(
        engine=engine, Session=Session, cold=cold,
        TechSpec=TechSpec, ArtifactColdStorage=ArtifactColdStorage,
    )


def _seed(env, *, spec_id, content, cold_bytes=None, cold_path=None,
          write_cold=True):
    """Insert a TechSpec plus its coldstore manifest, optionally writing a cold
    copy whose bytes differ from the live content (to exercise verification)."""
    from app.models.base import utcnow

    rel = cold_path or f"tech_specs/2026/08/{spec_id}.txt.gz"
    if write_cold:
        p = env.cold / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(p, "wb") as f:
            f.write(cold_bytes if cold_bytes is not None else content.encode())

    db = env.Session()
    db.add(env.TechSpec(id=spec_id, change_request_id="cr-1",
                        content=content, version=1))
    db.add(env.ArtifactColdStorage(
        id=f"man-{spec_id}", source_table="tech_specs", source_id=spec_id,
        change_request_id="cr-1", coldstore_path=rel,
        compressed_at=utcnow(), ready_for_archive=False))
    db.commit()
    db.close()
    return rel


def _raw_column(env, spec_id):
    """Read the column with raw SQL, bypassing the ORM rehydration hook — the
    only way to prove space was actually reclaimed."""
    with env.engine.connect() as conn:
        return conn.execute(
            text("SELECT content FROM tech_specs WHERE id = :i"), {"i": spec_id}
        ).scalar()


class TestSpaceIsActuallyReclaimed:
    def test_column_is_null_in_the_database_after_nulling(self, env):
        """The whole point of Finding #8 — verified with raw SQL, not the ORM."""
        from app.services.artifact_tiering import null_verified_source_columns

        content = "# TSD\nTimeout 30000 ms.\n" * 50
        _seed(env, spec_id="ts-1", content=content)

        db = env.Session()
        result = null_verified_source_columns(db)
        db.close()

        assert result["nulled"] == 1
        assert _raw_column(env, "ts-1") is None

    def test_orm_read_still_returns_identical_content(self, env):
        """What lets all 114 audited read sites stay unchanged."""
        from app.services.artifact_tiering import null_verified_source_columns

        content = "# TSD\nAccount No: 123456789012\n" * 40
        _seed(env, spec_id="ts-2", content=content)

        db = env.Session()
        null_verified_source_columns(db)
        db.close()

        db = env.Session()
        assert db.get(env.TechSpec, "ts-2").content == content
        db.close()

    def test_nulling_is_idempotent(self, env):
        """A second sweep must not re-report already-reclaimed rows. This is the
        case where reading through the hook would lie: `getattr` returns the
        rehydrated value, so a naive implementation sees a non-NULL column and
        tries to null it again forever."""
        from app.services.artifact_tiering import null_verified_source_columns

        _seed(env, spec_id="ts-3", content="content here")

        db = env.Session()
        first = null_verified_source_columns(db)
        db.close()
        db = env.Session()
        second = null_verified_source_columns(db)
        db.close()

        assert first["nulled"] == 1
        assert second["nulled"] == 0
        assert second["skipped"] >= 1


class TestNothingIsDestroyedWithoutProof:
    """Each test asserts the ORIGINAL CONTENT SURVIVES — the failure mode here
    is permanent data loss, so a zero counter alone is not sufficient evidence."""

    def test_refuses_when_cold_copy_does_not_match(self, env):
        from app.services.artifact_tiering import null_verified_source_columns

        _seed(env, spec_id="ts-4", content="REAL CONTENT",
              cold_bytes=b"SOMETHING ELSE ENTIRELY")

        db = env.Session()
        result = null_verified_source_columns(db)
        db.close()

        assert result["nulled"] == 0
        assert result["verify_failed"] == 1
        assert _raw_column(env, "ts-4") == "REAL CONTENT"

    def test_refuses_when_cold_file_is_missing(self, env):
        from app.services.artifact_tiering import null_verified_source_columns

        _seed(env, spec_id="ts-5", content="KEEP ME", write_cold=False)

        db = env.Session()
        null_verified_source_columns(db)
        db.close()

        assert _raw_column(env, "ts-5") == "KEEP ME"

    def test_refuses_when_cold_file_is_corrupt(self, env, tmp_path):
        from app.services.artifact_tiering import null_verified_source_columns

        rel = _seed(env, spec_id="ts-6", content="KEEP ME TOO")
        # Overwrite the gzip with non-gzip bytes.
        (env.cold / rel).write_bytes(b"not gzip at all")

        db = env.Session()
        null_verified_source_columns(db)
        db.close()

        assert _raw_column(env, "ts-6") == "KEEP ME TOO"

    def test_disabled_by_default_flag_nulls_nothing(self, env, monkeypatch):
        from app.core.config import settings
        from app.services.artifact_tiering import null_verified_source_columns

        monkeypatch.setitem(settings.__dict__, "artifact_coldstore_null_source", False)
        _seed(env, spec_id="ts-7", content="UNTOUCHED")

        db = env.Session()
        result = null_verified_source_columns(db)
        db.close()

        assert result == {"enabled": False, "nulled": 0}
        assert _raw_column(env, "ts-7") == "UNTOUCHED"


class TestRehydrationFailureIsSafe:
    def test_missing_cold_file_leaves_attribute_none_not_an_exception(self, env):
        """If the cold copy vanishes after nulling (e.g. an ops archive move),
        reads must degrade to "no content", exactly as a NULL column does today
        — never raise out of an ORM load and break unrelated queries."""
        from app.services.artifact_tiering import null_verified_source_columns

        rel = _seed(env, spec_id="ts-8", content="will vanish")
        db = env.Session()
        null_verified_source_columns(db)
        db.close()
        (env.cold / rel).unlink()

        db = env.Session()
        row = db.get(env.TechSpec, "ts-8")   # must not raise
        assert row.content is None
        db.close()

    def test_untiered_row_with_empty_content_is_left_alone(self, env):
        """A genuinely empty column (no manifest) must not trigger file IO or
        be mistaken for tiered content."""
        db = env.Session()
        db.add(env.TechSpec(id="ts-9", change_request_id="cr-1", content=None, version=1))
        db.commit()
        db.close()

        db = env.Session()
        assert db.get(env.TechSpec, "ts-9").content is None
        db.close()

    def test_read_through_can_be_disabled(self, env, monkeypatch):
        from app.core.config import settings
        from app.services.artifact_tiering import null_verified_source_columns

        _seed(env, spec_id="ts-10", content="hidden when off")
        db = env.Session()
        null_verified_source_columns(db)
        db.close()

        monkeypatch.setitem(settings.__dict__, "artifact_coldstore_read_through", False)
        db = env.Session()
        assert db.get(env.TechSpec, "ts-10").content is None
        db.close()


class TestJsonColumnsBecomeRealSqlNull:
    """`a2a_messages.payload` / `.response_body` are SQLAlchemy `JSON` columns,
    and they are the two largest tiering targets.

    Assigning Python `None` to a JSON column does NOT produce SQL NULL — it
    serialises to the JSON *string* `'null'`, so the row still occupies storage
    and the feature silently reclaims nothing while reporting success. This was
    a real bug during implementation; `_sql_null_for` uses `sqlalchemy.null()`
    for JSON columns instead. These tests pin that.
    """

    @pytest.fixture
    def a2a_env(self, tmp_path, monkeypatch):
        engine = create_engine("sqlite:///:memory:")
        Session = sessionmaker(bind=engine)
        base = _install_db_stub(engine, Session, monkeypatch)

        from app.core.config import settings
        ArtifactColdStorage, A2AMessage = _ArtifactColdStorage, _A2AMessage

        cold = tmp_path / "cold"
        cold.mkdir()
        monkeypatch.setitem(settings.__dict__, "artifact_coldstore_dir", str(cold))
        monkeypatch.setitem(settings.__dict__, "artifact_coldstore_null_source", True)
        monkeypatch.setitem(settings.__dict__, "artifact_coldstore_read_through", True)
        base.metadata.create_all(
            engine, tables=[A2AMessage.__table__, ArtifactColdStorage.__table__])

        from app.services.artifact_coldstore_read import register_coldstore_read_through
        register_coldstore_read_through()
        return types.SimpleNamespace(engine=engine, Session=Session, cold=cold,
                                     A2AMessage=A2AMessage,
                                     ArtifactColdStorage=ArtifactColdStorage)

    @staticmethod
    def _seed(env, payload, response_body):
        import json as _json

        from app.models.base import utcnow

        rel = "a2a_messages/2026/08/m1.json.gz"
        p = env.cold / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(p, "wb") as f:
            f.write(_json.dumps({"payload": payload,
                                 "response_body": response_body},
                                default=str).encode())
        db = env.Session()
        db.add(env.A2AMessage(id="m1", change_request_id="cr1", partner_id="p1",
                              task_type="cert_case_result", direction="outbound",
                              payload=payload, response_body=response_body,
                              status="delivered"))
        db.add(env.ArtifactColdStorage(
            id="man1", source_table="a2a_messages", source_id="m1",
            change_request_id="cr1", coldstore_path=rel,
            compressed_at=utcnow(), ready_for_archive=False))
        db.commit()
        db.close()

    def test_json_columns_are_true_sql_null_after_nulling(self, a2a_env):
        from app.services.artifact_tiering import null_verified_source_columns

        payload = {"payload": {"test_case_id": "TC-77"}, "extra": [1, 2, 3]}
        self._seed(a2a_env, payload, {"status": "ok"})

        db = a2a_env.Session()
        assert null_verified_source_columns(db)["nulled"] == 1
        db.close()

        with a2a_env.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT payload, typeof(payload), response_body "
                "FROM a2a_messages WHERE id='m1'")).first()
        assert row[0] is None, "payload is not SQL NULL — no space was reclaimed"
        assert row[1] == "null", f"expected sqlite typeof 'null', got {row[1]!r}"
        assert row[2] is None, "response_body is not SQL NULL"

    def test_both_json_columns_rehydrate(self, a2a_env):
        from app.services.artifact_tiering import null_verified_source_columns

        payload = {"payload": {"test_case_id": "TC-77"}, "extra": [1, 2, 3]}
        response = {"status": "ok", "code": 200}
        self._seed(a2a_env, payload, response)

        db = a2a_env.Session()
        null_verified_source_columns(db)
        db.close()

        db = a2a_env.Session()
        msg = db.get(a2a_env.A2AMessage, "m1")
        assert msg.payload == payload
        assert msg.response_body == response
        db.close()

    def test_column_level_select_path_rehydrates(self, a2a_env):
        """Mirrors `cert_txns`, the one tiered-column read that bypasses the
        ORM. Raw SQL returns JSON as text, so the helper must also decode."""
        from app.services.artifact_coldstore_read import rehydrate_payload_dict
        from app.services.artifact_tiering import null_verified_source_columns

        payload = {"payload": {"test_case_id": "TC-77"}}
        self._seed(a2a_env, payload, {"status": "ok"})
        db = a2a_env.Session()
        null_verified_source_columns(db)
        db.close()

        db = a2a_env.Session()
        rows = db.execute(text("SELECT id, payload FROM a2a_messages WHERE id='m1'")).all()
        for msg_id, raw_payload in rows:
            got = rehydrate_payload_dict(db, msg_id, raw_payload)
            assert ((got or {}).get("payload") or {}).get("test_case_id") == "TC-77"
        db.close()

    def test_helper_decodes_a_json_string_payload(self, a2a_env):
        """A column-level select on a JSON column may hand back raw text."""
        from app.services.artifact_coldstore_read import rehydrate_payload_dict

        db = a2a_env.Session()
        got = rehydrate_payload_dict(db, "irrelevant", '{"payload": {"x": 1}}')
        assert got == {"payload": {"x": 1}}
        db.close()


class TestExplicitRehydrationHelper:
    """`cert_txns` reads a tiered column via a column-level select(), which
    an ORM load event cannot cover. It calls this helper instead."""

    def test_passthrough_when_payload_is_present(self, env):
        from app.services.artifact_coldstore_read import rehydrate_payload_dict

        payload = {"payload": {"test_case_id": "TC-1"}}
        db = env.Session()
        assert rehydrate_payload_dict(db, "any-id", payload) is payload
        db.close()

    def test_returns_none_when_not_tiered(self, env):
        from app.services.artifact_coldstore_read import rehydrate_payload_dict

        db = env.Session()
        assert rehydrate_payload_dict(db, "unknown-id", None) is None
        db.close()
