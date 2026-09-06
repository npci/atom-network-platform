# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Source-document seeding (uploaded BRD at change creation → Phase A prompt block)."""
from types import SimpleNamespace

from app.services.source_material import source_block, SOURCE_DOC_MAX_CHARS


def _cr(text=None, name=None):
    return SimpleNamespace(source_doc_text=text, source_doc_name=name)


def test_no_document_renders_nothing():
    assert source_block(_cr()) == ""
    assert source_block(_cr(text="   ")) == ""


def test_block_is_wrapped_untrusted_and_named():
    out = source_block(_cr(text="Limit is Rs. 5000 per txn.", name="mandate_brd.docx"))
    assert "SOURCE_DOCUMENT" in out and "untrusted" in out          # injection defense wrapper
    assert "mandate_brd.docx" in out
    assert "Limit is Rs. 5000 per txn." in out
    assert "does not\nreplace" in out or "does not" in out          # seed-not-substitute framing


def test_oversized_document_is_capped_with_visible_note():
    big = "x" * (SOURCE_DOC_MAX_CHARS + 5000)
    out = source_block(_cr(text=big, name="huge.pdf"))
    assert len(out) < SOURCE_DOC_MAX_CHARS + 2000                   # bounded, not the full text
    assert "truncated" in out                                        # the model KNOWS it's partial


def test_appends_cleanly_to_an_intent():
    intent = "add retryFlag to Pay"
    combined = intent + source_block(_cr(text="doc body", name="a.docx"))
    assert combined.startswith(intent)
    # and the no-document case leaves the intent byte-identical
    assert intent + source_block(_cr()) == intent
