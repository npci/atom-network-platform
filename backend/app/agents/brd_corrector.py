# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Correct an uploaded BRD to match the plan for 'plan-wins' resolutions (Path B).

When the user decides the plan is right over their uploaded BRD, the BRD must be
corrected to match. We ask the LLM for the exact ``{find, replace}`` edits (find
MUST be a verbatim BRD substring so the deterministic editor can locate it), then
apply them in place via ``docx_surgical`` — formatting and images preserved —
producing a NEW BRD version. .md/.txt are edited as text; .pdf can't be edited in
place (Path A fallback — skipped here).

Both functions are best-effort: the resolution itself already stands, so a failure
here just means the BRD isn't auto-corrected (the user can revise/re-upload).
"""
from __future__ import annotations

import logging

from app.core.domain.registry import prompt_block
from app.core.llm import call_llm_structured
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE

logger = logging.getLogger(__name__)

_SYSTEM = (
    f"You correct a {prompt_block('document_register', 'change-management document')} so it matches an "
    "authoritative TARGET. You are given the document "
    "text and a list of CONFLICTS — each with the TARGET wording it must be made to match (the ratified "
    "plan, or a reviewer's explicit ruling). For each conflict, find the EXACT phrase in the document to "
    "change — a verbatim substring, copied character-for-character — and give the corrected wording that "
    "matches its TARGET. Keep each edit as small as possible. If you cannot find a verbatim phrase for a "
    "conflict, omit that conflict.\n"
    'Respond with ONLY JSON: {"corrections":[{"conflict_id":"..","find":"<verbatim substring>",'
    '"replace":"<corrected text>"}]}\n' + ANTI_INJECTION_CLAUSE
)


async def propose_brd_corrections(doc_content: str, conflicts: list[dict]) -> list[dict]:
    """For the given conflicts, return ``[{conflict_id, find, replace}]`` where ``find``
    is a verbatim doc substring. Each conflict may carry ``_target`` — the wording to
    correct TO (a reviewer's custom ruling); the default target is the ratified plan.
    Fail-open: ``[]``. Only corrections whose ``find`` actually occurs in the doc are kept."""
    if not conflicts or not (doc_content or "").strip():
        return []
    lines = []
    for c in conflicts:
        target = str(c.get("_target") or "the ratified plan (make the document consistent with it)").strip()
        detail = str((c.get("evidence") or {}).get("detail") or "").strip()
        row = f"- {c.get('id')}: {c.get('text')}"
        if detail:
            row += f"\n  divergence detail: {detail}"      # usually states BOTH sides → the correct value
        row += f"\n  TARGET: {target}"
        lines.append(row)
    user = ("CONFLICTS (correct the document to match each TARGET):\n" + "\n".join(lines) + "\n\n"
            f"DOCUMENT TEXT:\n{wrap_untrusted(doc_content[:24000], 'DOCUMENT')}\n\n"
            "Return the corrections JSON now.")
    try:
        # Forced tool use, not prose JSON: `find` carries verbatim doc text (quotes, newlines)
        # — exactly what breaks prose JSON. Worse, the json_recovery _sanitize repair could
        # escape a newline INSIDE a `find` string, so the substring no longer matched the doc
        # and the correction silently no-op'd. Tool-validated arguments end both failure modes.
        data = await call_llm_structured(
            _SYSTEM, user,
            schema={"type": "object",
                    "properties": {"corrections": {
                        "type": "array",
                        "items": {"type": "object",
                                  "properties": {"conflict_id": {"type": "string"},
                                                 "find": {"type": "string"},
                                                 "replace": {"type": "string"}},
                                  "required": ["conflict_id", "find", "replace"]}}},
                    "required": ["corrections"]},
            tool_name="record_corrections", agent_name="brd_corrector", max_tokens=2000,
        )
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.warning("propose_brd_corrections failed: %s", e)
        return []
    items = data.get("corrections") if isinstance(data, dict) else None
    out: list[dict] = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        find = str(it.get("find") or "")
        replace = str(it.get("replace") or "")
        if find and find in doc_content:   # only verbatim-locatable edits survive
            out.append({"conflict_id": str(it.get("conflict_id") or ""), "find": find, "replace": replace})
    return out


def uncorrected_followups(to_edit: list[dict], corrections: list[dict]) -> list[str]:
    """Manual-review breadcrumbs for the plan-wins / custom conflicts the corrector
    could NOT ground into a verbatim edit. A 'review'-shaped conflict often has no
    locatable phrase, so ``propose`` returns nothing for it — without this the user's
    resolution would silently no-op (no new version, no note). Each returned string is
    a follow-up line so the user knows that conflict still needs a hand-edit."""
    corrected = {c.get("conflict_id") for c in corrections}
    return [f"couldn't auto-correct — revise manually to match {c.get('_target') or 'the ratified plan'}: "
            f"{(c.get('text') or '').strip()}"
            for c in to_edit if c.get("id") not in corrected]


def _doc_model(doc_kind: str):
    """Map a doc_kind to (SQLAlchemy model, DRAFT status) for BRD or Tech Spec."""
    if doc_kind == "brd":
        from app.models.brd import BRD, BRDStatus
        return BRD, BRDStatus.DRAFT
    if doc_kind == "tech_spec":
        from app.models.tech_spec import TechSpec
        from app.models.research import ArtifactStatus
        return TechSpec, ArtifactStatus.DRAFT
    return None, None


def apply_doc_corrections(db, change_id: str, doc_kind: str, corrections: list[dict],
                          corrected_by: str | None = None,
                          additions: list[str] | None = None) -> int | None:
    """Apply ``{find, replace}`` corrections + append ``additions`` (dropped plan
    requirements to add back) to the latest uploaded doc (BRD or Tech Spec),
    persisting a NEW version. Corrections that can't be located are surfaced as a
    manual-review note in the doc (never silently dropped — G7). .docx → in-place
    surgical edit (formatting/images preserved, image byte-identity gated); .md/.txt
    → text; .pdf → skipped (Path A). Returns the new version, or None when nothing
    applied."""
    from pathlib import Path
    from datetime import datetime, timezone
    from app.models.document_source import DocumentSource
    Model, draft_status = _doc_model(doc_kind)
    if Model is None:
        return None
    adds = [a for a in (additions or []) if str(a or "").strip()]
    if not corrections and not adds:
        return None
    try:
        doc = (db.query(Model).filter(Model.change_request_id == change_id)
               .order_by(Model.version.desc()).first())
        if doc is None or not doc.file_path:
            return None
        src = Path(doc.file_path)
        if not src.exists():
            return None
        ext = src.suffix.lower()
        new_version = (doc.version or 1) + 1
        out = src.with_name(f"{src.stem}_reconciled_v{new_version}{ext}")

        # Downstream text = prior content + the edits applied (robust vs re-extraction).
        # Collect corrections we couldn't locate so they're surfaced, not dropped.
        new_content = doc.content or ""
        unmatched: list[str] = []
        for c in corrections:
            f = c.get("find") or ""
            if not f:
                continue
            if f in new_content:
                new_content = new_content.replace(f, c.get("replace") or "", 1)
            else:
                unmatched.append(f)

        # G7: a single follow-ups note — requirements added back + corrections that
        # need a manual edit — appended to both the content and the file.
        note_items = list(adds) + [f"needs manual edit (couldn't auto-locate): “{u}”" for u in unmatched]
        if note_items:
            new_content += "\n\nReconciliation follow-ups:\n" + "\n".join(f"• {n}" for n in note_items)

        if ext == ".docx":
            from app.services.docx_surgical import correct_docx
            res = correct_docx(str(src), corrections, str(out), additions=note_items)
            if res["applied"] == 0 and res.get("added", 0) == 0:
                out.unlink(missing_ok=True)
                return None
            if not res["media_preserved"]:
                logger.warning("apply_doc_corrections: media changed — discarding for %s", change_id)
                out.unlink(missing_ok=True)
                return None
            # correct_docx runs its OWN find/replace matching against the real paragraph
            # runs, independent of the naive whole-string replace on `doc.content` above —
            # its own unmatched edits are discarded internally, so the two can diverge
            # (an edit applied to the file but not reflected in `new_content`, or vice
            # versa). Re-extract from the actual output file so the stored `content`
            # (what every downstream consumer reads) matches the bytes, not a guess.
            stored_content = new_content
            try:
                from app.services.text_extraction import extract_full_text
                extracted = extract_full_text(out)
                if extracted.strip():
                    stored_content = extracted
            except Exception as e:  # noqa: BLE001 — fall back to the computed text
                logger.warning("apply_doc_corrections: re-extraction failed for %s: %s", change_id, e)
        elif ext in (".md", ".txt"):
            if (len(corrections) - len(unmatched)) == 0 and not adds:
                return None
            out.write_text(new_content, encoding="utf-8")
            stored_content = new_content
        else:
            logger.info("apply_doc_corrections: %s not editable in place (Path A) for %s", ext, change_id)
            return None

        db.add(Model(
            change_request_id=change_id, content=stored_content, version=new_version,
            status=draft_status, source=DocumentSource.UPLOADED,
            original_filename=(doc.original_filename or src.name),
            file_path=str(out), uploaded_by=corrected_by,
            uploaded_at=datetime.now(timezone.utc),
        ))
        db.commit()
        logger.info("apply_doc_corrections: change=%s kind=%s v%d (%d applied, %d added, %d unmatched)",
                    change_id, doc_kind, new_version, len(corrections) - len(unmatched), len(adds), len(unmatched))
        return new_version
    except Exception as e:  # noqa: BLE001 — best-effort; the resolution already stands
        logger.warning("apply_doc_corrections failed for %s/%s: %s", change_id, doc_kind, e)
        db.rollback()
        return None
