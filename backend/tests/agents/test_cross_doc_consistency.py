# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Cross-document BRD↔TSD FR consistency (pending #3)."""
from app.agents.cross_doc_consistency import check_cross_doc, cross_doc_items


def test_consistent_when_frs_match():
    brd = "The BRD defines FR-1 and FR-2 and FR-3."
    tsd = "TSD implements FR-1, FR-2, and FR-3 at the wire level."
    r = check_cross_doc(brd, tsd)
    assert r["consistent"] and r["findings"] == []


def test_flags_fr_the_tsd_invented():
    brd = "Requirements: FR-1, FR-2."
    tsd = "Design covers FR-1, FR-2 and also FR-9."
    r = check_cross_doc(brd, tsd)
    items = {f["item"]: f["kind"] for f in r["findings"]}
    assert items.get("FR-9") == "fr_undefined"
    assert "FR-9" in cross_doc_items(r)


def test_flags_fr_the_tsd_dropped():
    brd = "Requirements: FR-1, FR-2, FR-3."
    tsd = "Design covers FR-1 and FR-2."
    r = check_cross_doc(brd, tsd)
    items = {f["item"]: f["kind"] for f in r["findings"]}
    assert items.get("FR-3") == "fr_uncovered"


def test_fail_open_on_empty():
    assert check_cross_doc("", "anything")["consistent"] is True
    assert check_cross_doc("FR-1", "")["consistent"] is True


def test_zero_padding_mismatch_is_not_a_finding():
    # M4 regression: BRD emits zero-padded FR ids, TSD emits unpadded — same
    # requirements must read as consistent, not 2N spurious findings.
    brd = "The BRD defines FR-01, FR-02, and FR-03."
    tsd = "TSD implements FR-1, FR-2, and FR-3."
    r = check_cross_doc(brd, tsd)
    assert r["consistent"] and r["findings"] == [], r


def test_padding_normalized_but_real_gap_still_flagged():
    brd = "Requirements: FR-01, FR-02, FR-03."
    tsd = "Design covers FR-1 and FR-2 only."
    r = check_cross_doc(brd, tsd)
    items = {f["item"]: f["kind"] for f in r["findings"]}
    assert items.get("FR-3") == "fr_uncovered"   # normalized to FR-3, genuinely missing
    assert "FR-1" not in items and "FR-2" not in items
