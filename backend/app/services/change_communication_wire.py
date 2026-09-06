# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""v1.1 wire adapter for `change_communication`.

Maps the *internal* kit envelope produced by
`change_dispatch.build_kit_envelope()` (the pre-existing `documents[]` shape,
after per-partner size-gating) to the **v1.1 canonical wire payload** defined in
`docs/A2A_v1_1_change_communication.md`.

Why an adapter (instead of rewriting `build_kit_envelope`): the internal
`documents[]` shape is also consumed by `snapshot_publication()`, the size-gating
in `_gate_envelope_for_partner()`, and the authority-side diff/revision agents.
Transforming only at the last step before send keeps all of that untouched and
makes this transform a pure, unit-testable dict→dict function.

Single-emit: this emits ONLY the v1.1 shape — no `documents[]` / `docx_b64`
aliases. See the contract doc §1 for the cutover assumption.

Pure module: stdlib only. No app models, no DB, no SDK — so it imports cleanly in
any harness and is trivially testable.
"""
from __future__ import annotations

# Attachment kinds the internal envelope may carry as flat `<kind>_b64` keys.
_ATTACHMENT_KINDS = ("docx", "pptx", "xlsx", "video", "xsd_zip")

# Business/regulatory fields defined by the contract but not yet backed by any
# The Authority data source. Emitted as null until the capture workstream lands
# (contract §7). `prerequisite_change_ids` defaults to [] and is set separately.
_NULL_BUSINESS_FIELDS = (
    "effective_date",
    "backward_compatibility_until",
    "mandatory",                  # NO default — null or a captured value, never guessed
    "npci_contact",
)

# Internal doc_type → canonical wire doc_type (contract §6).
_DOC_TYPE_ALIASES = {"tsd": "tech_spec"}


def _doc_to_v1_1(doc: dict) -> dict:
    """Map one internal `documents[]` entry to a v1.1 `product_kit[]` entry.

    Collapses the flat `<kind>_b64` / `<kind>_filename` / `<kind>_omitted` keys
    into the uniform `attachments[]` model.
    """
    doc_type = doc.get("doc_type")
    doc_type = _DOC_TYPE_ALIASES.get(doc_type, doc_type)

    attachments: list[dict] = []
    for kind in _ATTACHMENT_KINDS:
        b64 = doc.get(f"{kind}_b64")
        omitted = bool(doc.get(f"{kind}_omitted"))
        filename = doc.get(f"{kind}_filename")
        # Emit an attachment entry when there is a blob, an omission marker, or
        # at least a filename — otherwise this kind isn't present on the doc.
        if b64 is None and not omitted and filename is None:
            continue
        attachments.append(
            {
                "kind":       kind,
                "bytes":      b64,          # None when size-gated out for this partner
                "filename":   filename,
                "sha256":     doc.get(f"{kind}_sha256"),
                "size_bytes": doc.get(f"{kind}_size_bytes"),
                "mime_type":  doc.get(f"{kind}_mime_type"),
            }
        )

    return {
        "doc_type":       doc_type,
        "version":        doc.get("version", 1),
        "content":        doc.get("content", ""),
        "content_sha256": doc.get("content_sha256"),
        "attachments":    attachments,
    }


def to_wire_v1_1(envelope: dict) -> dict:
    """Transform the internal kit envelope into the v1.1 `change_communication`
    payload. Idempotent: an envelope already in v1.1 shape is returned unchanged.
    """
    # Idempotency guard — already v1.1 (has product_kit, no legacy documents).
    if "product_kit" in envelope and "documents" not in envelope:
        return envelope

    kit_version = envelope.get("negotiation_version", 1) or 1
    change_summary = envelope.get("change_summary") or None

    payload: dict = {
        "change_id":              envelope.get("change_id"),
        "kit_version":            kit_version,
        "supersedes_kit_version": (kit_version - 1) if kit_version > 1 else None,
        # revision_reason / changed_documents / unchanged_documents are derived
        # from a KitPublication diff — left empty here pending that wiring.
        "revision_reason":        None,
        "revision_summary":       change_summary if kit_version > 1 else None,
        "changed_documents":      [],
        "unchanged_documents":    [],
        "title":                  envelope.get("title"),
        # TODO(capture §7): no real one-paragraph summary field exists yet;
        # fall back to the enhanced prompt so `summary` is non-empty.
        "summary":                envelope.get("enhanced_prompt") or change_summary or "",
    }

    # Business/regulatory metadata — optional, null until captured (contract §7).
    for field in _NULL_BUSINESS_FIELDS:
        payload[field] = None
    payload["prerequisite_change_ids"] = []

    # Optional the Authority operational fields (documented, not required).
    payload["kit_id"] = envelope.get("kit_id")
    payload["rollout_type"] = envelope.get("rollout_type")
    payload["valid_until"] = envelope.get("valid_until")

    payload["product_kit"] = [_doc_to_v1_1(d) for d in envelope.get("documents", [])]
    return payload
