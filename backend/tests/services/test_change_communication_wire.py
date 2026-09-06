# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Contract tests for the v1.1 `change_communication` wire adapter.

Asserts `app.services.change_communication_wire.to_wire_v1_1` produces the shape
defined in `docs/A2A_v1_1_change_communication.md` — canonical names, the
`attachments[]` model, single-emit (no legacy aliases), nullable business fields,
and idempotency.

Pure test: the adapter is stdlib-only, so this imports without the app graph.
"""
from __future__ import annotations

import json

from app.services.change_communication_wire import to_wire_v1_1


def _internal(nv: int = 1) -> dict:
    """A representative internal envelope (post build_kit_envelope + gating)."""
    return {
        "schema_version": "1.0",
        "message_kind": "CHANGE_COMMUNICATION",
        "kit_id": "CHG_abc",
        "rollout_type": "STANDARD",
        "valid_until": "2026-07-31T10:15:00Z",
        "change_id": "CHG-0142",
        "negotiation_version": nv,
        "title": "Lite Auto Top-up",
        "enhanced_prompt": "Enhanced description of the change.",
        "change_summary": "TSD 4.2.3 relaxed to 6s." if nv > 1 else "",
        "documents": [
            {
                "doc_type": "brd", "content": "BRD md", "version": 1, "content_sha256": "aa",
                "docx_b64": "B64", "docx_filename": "BRD.docx", "docx_sha256": "bb",
                "docx_size_bytes": 48213, "docx_mime_type": "application/…docx",
            },
            {"doc_type": "tsd", "content": "TSD md", "version": 1, "content_sha256": "cc"},
            {
                "doc_type": "cert_test_cases", "content": "md", "version": 1, "content_sha256": "gg",
                "xlsx_b64": "X64", "xlsx_filename": "cases.xlsx", "xlsx_sha256": "hh",
                "xlsx_size_bytes": 91002, "xlsx_mime_type": "…sheet",
            },
            {
                "doc_type": "promo_video", "content": "", "version": 1, "content_sha256": "dd",
                "video_filename": "promo.mp4", "video_sha256": "ee", "video_size_bytes": 33554432,
                "video_mime_type": "video/mp4", "video_omitted": True,
                "video_omitted_reason": "attachment exceeds partner inline limit",
            },
            {"doc_type": "faq", "content": "faq md", "version": 1, "content_sha256": "ff"},
        ],
    }


def test_single_emit_no_legacy_aliases():
    out = to_wire_v1_1(_internal())
    assert "documents" not in out
    assert "negotiation_version" not in out
    assert "schema_version" not in out
    assert "message_kind" not in out
    assert "docx_b64" not in json.dumps(out)


def test_kit_version_and_supersedes():
    v1 = to_wire_v1_1(_internal(nv=1))
    assert v1["kit_version"] == 1
    assert v1["supersedes_kit_version"] is None
    v2 = to_wire_v1_1(_internal(nv=2))
    assert v2["kit_version"] == 2
    assert v2["supersedes_kit_version"] == 1
    assert v2["revision_summary"] == "TSD 4.2.3 relaxed to 6s."


def test_business_fields_nullable():
    out = to_wire_v1_1(_internal())
    for field in ("effective_date", "backward_compatibility_until", "mandatory", "npci_contact"):
        assert out[field] is None, field
    assert out["prerequisite_change_ids"] == []
    # mandatory in particular must never be a guessed default
    assert out["mandatory"] is None
    # dropped from the wire entirely — not emitted as null
    for field in (
        "scope", "affected_apis", "affected_roles", "required_cert_subset",
        "npci_circular_ref", "rbi_reference", "simulator_available_from",
    ):
        assert field not in out, field


def test_doc_type_tsd_normalised():
    out = to_wire_v1_1(_internal())
    types = {d["doc_type"] for d in out["product_kit"]}
    assert "tech_spec" in types and "tsd" not in types


def test_attachments_model():
    out = to_wire_v1_1(_internal())
    pk = {d["doc_type"]: d for d in out["product_kit"]}
    brd_att = pk["brd"]["attachments"][0]
    assert brd_att == {
        "kind": "docx", "bytes": "B64", "filename": "BRD.docx", "sha256": "bb",
        "size_bytes": 48213, "mime_type": "application/…docx",
    }
    assert pk["faq"]["attachments"] == []
    assert pk["cert_test_cases"]["attachments"][0]["kind"] == "xlsx"


def test_size_gated_attachment_has_no_bytes():
    """A size-gated attachment still carries its metadata but no bytes, and the
    wire no longer advertises omitted/omitted_reason flags."""
    out = to_wire_v1_1(_internal())
    vid = {d["doc_type"]: d for d in out["product_kit"]}["promo_video"]["attachments"][0]
    assert vid["kind"] == "video"
    assert vid["bytes"] is None
    assert vid["filename"] == "promo.mp4"
    assert "omitted" not in vid
    assert "omitted_reason" not in vid


def test_idempotent():
    once = to_wire_v1_1(_internal())
    assert to_wire_v1_1(once) is once
