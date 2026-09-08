# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""`ingest_all`'s orphan sweep must not delete rows for a folder it cannot see.

The sweep deletes any chunk whose `source_file` is absent from disk. That is
correct for a file the operator removed, and catastrophic for a whole category
whose DIRECTORY is missing — an unmounted bind mount and a renamed folder look
identical to a deletion, and the sweep commits.

This is not hypothetical. Renaming the `upi_product_docs` folder key while
migration 0139 rewrote those rows to the NEW category value made the corpus
known-to-the-sweep and absent-from-disk in the same step, and one `ingest_all`
run would have dropped every product-doc chunk.

There were no `ingest_all` tests at all before this file, which is also how a
plain `AttributeError` in the sweep's own logging reached a running server:
`DocCategory` is a class of string CONSTANTS, not an Enum, so a `.value` on one
raises — and the only signal was a startup line reading "Startup KB ingest /
BM25 build failed ... 'str' object has no attribute 'value'".
"""
from __future__ import annotations

import pytest

from app.models.document_chunk import DocCategory, DocumentChunk
from app.rag.ingestion import FOLDER_TO_CATEGORY, LEGACY_FOLDER_ALIASES


@pytest.fixture
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401 — register models so metadata is complete
    from app.core.database import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[DocumentChunk.__table__])
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


def _chunk(**kw):
    base = dict(
        source_file="network_product_docs/spec.pdf",
        doc_category=DocCategory.NETWORK_PRODUCT_DOC,
        content="body",
        chunk_index=0,
    )
    base.update(kw)
    return DocumentChunk(**base)


def test_doc_category_members_are_plain_strings():
    """Pins the trap directly: `DocCategory` is NOT an Enum.

    Anything that treats a category as one — `.value`, `.name`, iterating the
    class — raises at runtime and, on the ingest path, only as a swallowed
    startup warning.
    """
    assert isinstance(DocCategory.NETWORK_PRODUCT_DOC, str)
    assert not hasattr(DocCategory.NETWORK_PRODUCT_DOC, "value")
    assert all(isinstance(c, str) for c in FOLDER_TO_CATEGORY.values())


def test_sweep_keeps_rows_whose_folder_is_absent(db_session, tmp_path, monkeypatch):
    """An empty knowledge_base must not empty the database.

    Every category's folder is missing here, which is precisely the state an
    unmounted volume produces. Nothing may be deleted, and the run must not
    raise — the logging on this path used to.
    """
    from app.core.config import settings
    from app.rag import ingestion

    monkeypatch.setattr(settings, "knowledge_base_dir", str(tmp_path), raising=False)
    db_session.add(_chunk())
    db_session.commit()

    summary = ingestion.ingest_all(db_session, force=False)

    assert db_session.query(DocumentChunk).count() == 1, (
        "the sweep deleted a category whose folder is merely absent — an "
        "unmounted bind mount is now indistinguishable from a purge")
    assert not summary.get("orphans_removed")


def test_legacy_folder_alias_is_scanned(db_session, tmp_path, monkeypatch, caplog):
    """A file under the SUPERSEDED folder name is still seen, so its row lives.

    A folder name keys a datastore the operator owns — it is in a bind mount,
    not the repo — so it is pinned rather than renamed. Without the alias the
    directory is invisible to the scan while 0139 has already rewritten its rows
    to the new category, and the sweep then treats every one of them as an
    orphan.

    The file has to actually exist: a row whose file is genuinely gone IS an
    orphan, and deleting it is the sweep working correctly.
    """
    import logging

    from app.core.config import settings
    from app.rag import ingestion

    legacy = next(iter(LEGACY_FOLDER_ALIASES))
    (tmp_path / legacy).mkdir()
    (tmp_path / legacy / "old.md").write_text("# spec\n\nbody text\n")
    monkeypatch.setattr(settings, "knowledge_base_dir", str(tmp_path), raising=False)
    # Keep the test off the embedding backend; this is about which folders the
    # scan reaches, not about vectors.
    monkeypatch.setattr(ingestion, "embed_texts",
                        lambda texts: [[0.0] * 8 for _ in texts])
    db_session.add(_chunk(source_file=f"{legacy}/old.md"))
    db_session.commit()

    # force=False deliberately: `force` re-ingests, which deletes and rebuilds
    # the row and would mask whether the SWEEP spared it — the thing under test.
    with caplog.at_level(logging.WARNING, logger="app.rag.ingestion"):
        summary = ingestion.ingest_all(db_session, force=False)

    assert db_session.query(DocumentChunk).count() >= 1, (
        "a file under the legacy folder name was not seen by the scan, so its "
        "row was swept as an orphan")
    assert not summary.get("orphans_removed")
    assert any("superseded folder name" in r.message for r in caplog.records), (
        "the operator was never told to rename the directory")
