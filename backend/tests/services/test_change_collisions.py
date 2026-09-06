# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Cross-change collision detection (accuracy S8) — the SQL-side intersect: only the
overlapping (repo_id, path) rows on NON-completed other changes come back."""
import pytest


@pytest.fixture
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import app.models  # noqa: F401 — register models
    from app.models.change_analysis import ChangeImpactedPath
    from app.models.change_request import ChangeRequest
    from app.core.database import Base
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[ChangeImpactedPath.__table__, ChangeRequest.__table__])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _change(db, cid, status):
    from app.models.change_request import ChangeRequest
    db.add(ChangeRequest(id=cid, initial_prompt="x", created_by="u1", status=status))


def _path(db, cid, repo, path):
    from app.models.change_analysis import ChangeImpactedPath
    db.add(ChangeImpactedPath(change_request_id=cid, repo_id=repo, path=path))


def test_no_impacted_paths_returns_empty(db_session):
    from app.services.change_collisions import cross_change_collisions
    assert cross_change_collisions(db_session, "mine") == []


def test_overlapping_noncompleted_change_is_reported(db_session):
    from app.models.change_request import ChangeStatus
    from app.services.change_collisions import cross_change_collisions
    _change(db_session, "mine", ChangeStatus.XSD)
    _change(db_session, "other", ChangeStatus.BRD)
    _path(db_session, "mine", "r1", "NET-Common.xsd")
    _path(db_session, "other", "r1", "NET-Common.xsd")
    db_session.commit()
    out = cross_change_collisions(db_session, "mine")
    assert len(out) == 1
    assert out[0]["change_request_id"] == "other" and out[0]["path"] == "NET-Common.xsd"


def test_completed_change_is_excluded(db_session):
    from app.models.change_request import ChangeStatus
    from app.services.change_collisions import cross_change_collisions
    _change(db_session, "mine", ChangeStatus.XSD)
    _change(db_session, "done", ChangeStatus.COMPLETED)
    _path(db_session, "mine", "r1", "p.xsd")
    _path(db_session, "done", "r1", "p.xsd")
    db_session.commit()
    assert cross_change_collisions(db_session, "mine") == []


def test_composite_match_requires_same_repo_and_path(db_session):
    from app.models.change_request import ChangeStatus
    from app.services.change_collisions import cross_change_collisions
    _change(db_session, "mine", ChangeStatus.XSD)
    _change(db_session, "other", ChangeStatus.BRD)
    _path(db_session, "mine", "r1", "a.xsd")
    _path(db_session, "other", "r1", "b.xsd")          # same repo, different file → no hit
    _path(db_session, "other", "r2", "a.xsd")          # same file, different repo → no hit
    db_session.commit()
    assert cross_change_collisions(db_session, "mine") == []
