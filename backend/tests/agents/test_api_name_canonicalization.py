# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Deterministic canonicalization of SCREAMING_SNAKE network API placeholders (Fix 1).

The writer occasionally emits REQ_FOO_BAR / RESP_FOO_BAR despite the prompt rule,
which the doc validator flags as `invented_api_name`. canonicalize_api_names fixes
the FORMAT deterministically, leaving non-API SCREAMING_SNAKE (error codes / enums)
alone.
"""
from app.agents.document_validator import canonicalize_api_names as C


def test_resp_screaming_snake_to_canonical():
    assert C("send RESP_MERCHANT_REVIEW now") == "send RespMerchantReview now"


def test_req_and_resp_in_one_line():
    assert C("REQ_SET_LIMIT and RESP_SET_LIMIT") == "ReqSetLimit and RespSetLimit"


def test_error_codes_and_enums_untouched():
    assert C("decline code LIMIT_BREACH stays") == "decline code LIMIT_BREACH stays"


def test_already_canonical_untouched():
    assert C("ReqTransfer / RespTransfer unchanged") == "ReqTransfer / RespTransfer unchanged"


# ── H1 / M3: table-row + code-block handling in the pipeline canon pass ───────
# These replicate the exact per-field logic in pipeline.write_content so the
# dict-row data-loss fix (H1) and code-block exclusion (M3) are pinned.

def _canon_cell(c):
    return C(c) if isinstance(c, str) else c


def _canon_row(row):
    if isinstance(row, dict):
        return {k: _canon_cell(v) for k, v in row.items()}
    if isinstance(row, list):
        return [_canon_cell(c) for c in row]
    return row


def test_dict_row_preserves_keys_and_values():
    # H1 regression: iterating a dict row with `for c in row` would drop all
    # cell values, leaving column names. The fix preserves keys + canon values.
    row = {"ID": "FR-01", "API": "PSP sends REQ_CORP_ENROLL", "Priority": "High"}
    out = _canon_row(row)
    assert out == {"ID": "FR-01", "API": "PSP sends ReqCorpEnroll", "Priority": "High"}


def test_list_row_still_canonicalized():
    assert _canon_row(["REQ_CORP_ENROLL", "note"]) == ["ReqCorpEnroll", "note"]
