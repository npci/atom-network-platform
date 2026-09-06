# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The ratified solution-design contract — the BINDING technical surface that BRD/TSD generation
must honour, so the document track stops inventing APIs/wire-messages/schemas the implementation
track never builds.

By design (`ChangeAnalysis` is "produced before the BRD"; `flow_spec` "owns step IDs that BRD/TSD
render from"), the ratified Change-Analysis plan is authoritative for the technical solution. This
renders that plan as a compact, loud contract block + the hard rule "describe ONLY this; never
invent a new API/message/schema." Empty string when no ratified plan exists yet (then the doc is
the first technical artifact and the consistency gate reconciles later).
"""
from __future__ import annotations
from app.core.prompts import render_prompt

import logging

from app.core.domain.registry import prompt_block

logger = logging.getLogger(__name__)

# The rule text is domain-neutral; the domain name and the illustrative
# message/schema examples come from the active pack (UPI bytes unchanged —
# prompt-snapshot-verified).
_RULE = render_prompt(
    "agents/plan_contract/rule.md",
    DOMAIN_NAME=prompt_block("domain_name", "platform"),
    MESSAGE_TYPE_EXAMPLE=prompt_block(
        "message_type_example", "e.g. new request/response message pairs"),
    SCHEMA_FIELD_EXAMPLE=prompt_block(
        "schema_field_example", "e.g. a new field on an existing message"),
)


def canonical_values(db, change_request_id: str) -> list[str]:
    """The ratified canonical values (fixed enums / caps / codes) for this change — the exact
    forms every downstream doc must reproduce verbatim. Used to guard auto-repairs: a repair may
    fix its flagged issue but must never drop or re-case one of these. Best-effort → [] on any error."""
    if db is None or not change_request_id:
        return []
    try:
        from app.models.change_analysis import ChangeAnalysis
        ca = (db.query(ChangeAnalysis)
              .filter(ChangeAnalysis.change_request_id == change_request_id)
              .order_by(ChangeAnalysis.version.desc()).first())
        ct = (ca.technical_analysis or {}).get("canonical_terms") if ca else None
        if not isinstance(ct, list):
            return []
        return [str(t["value"]).strip() for t in ct
                if isinstance(t, dict) and str(t.get("value") or "").strip()]
    except Exception:  # noqa: BLE001 — the guard must never break generation
        return []


def build_plan_contract(db, change_request_id: str) -> str:
    """Render the ratified plan as a binding contract block for BRD/TSD prompts. Empty when there
    is no plan yet, or on any error (best-effort — must never break document generation)."""
    if db is None or not change_request_id:
        return ""
    try:
        from app.models.change_analysis import ChangeAnalysis
        ca = (db.query(ChangeAnalysis)
              .filter(ChangeAnalysis.change_request_id == change_request_id)
              .order_by(ChangeAnalysis.version.desc()).first())
        if ca is None:
            return ""
        ta = ca.technical_analysis or {}
        fp = ca.functional_plan or {}
        flow = ca.flow_spec or {}
        parts: list[str] = [_RULE]

        ad = ta.get("approach_decision")
        if isinstance(ad, dict) and ad.get("approach"):
            line = (f"CHOSEN APPROACH: {ad.get('chosen_title') or ad.get('chosen_option_id') or '?'} "
                    f"[{ad.get('approach')}]")
            if ad.get("target_api"):
                line += f" → {ad['target_api']}"
            if ad.get("why"):
                line += f"\n  Rationale: {str(ad['why'])[:400]}"
            parts.append(line)

        # Uploaded-BRD reconciliation overrides — where a human accepted the
        # uploaded BRD's position over the original plan. AUTHORITATIVE; overrides
        # any conflicting statement below (added by record_reconciliation_version).
        addenda = ta.get("upload_reconciliation_addenda")
        if isinstance(addenda, list) and addenda:
            parts.append(
                "RECONCILED OVERRIDES (a human resolved uploaded-BRD-vs-plan conflicts — "
                "AUTHORITATIVE, overrides any conflicting statement below): "
                + "; ".join(str(a)[:300] for a in addenda[:20]))

        if fp.get("overview"):
            parts.append("SOLUTION OVERVIEW: " + str(fp["overview"])[:700])

        # CANONICAL TERMS — the fixed enums/values every downstream doc must reproduce verbatim.
        # Rendered prominently so the BRD/TSD writer copies them EXACTLY (and the value-fidelity
        # gate has an authoritative source) instead of re-casing / dropping them (BioAuth vs the
        # ratified BIOAUTH, or a lost ₹5000 cap).
        ct = ta.get("canonical_terms")
        if isinstance(ct, list) and ct:
            terms = []
            for t in ct:
                if isinstance(t, dict) and str(t.get("value") or "").strip():
                    lbl = str(t.get("term") or "").strip()
                    terms.append(f"{lbl} = {t['value']}" if lbl else str(t["value"]))
            if terms:
                parts.append("CANONICAL TERMS — reproduce these EXACT forms VERBATIM everywhere "
                             "(never re-case, re-spell, abbreviate, or omit a fixed value): "
                             + "; ".join(terms[:40]))

        # The data-model / wire decision is the field BRD/TSD most often violate.
        if ta.get("data_model_changes"):
            parts.append("DATA MODEL & WIRE/SCHEMA CHANGES (the COMPLETE set — nothing beyond this): "
                         + str(ta["data_model_changes"])[:1400])
        else:
            parts.append("DATA MODEL & WIRE/SCHEMA CHANGES: none specified by the plan — do NOT "
                         "introduce wire/schema changes.")

        # FROZEN WIRE CONTRACT — what Phase A (kind='xsd') ACTUALLY froze + a human approved. This is
        # the ground truth for the wire surface and OVERRIDES the plan narrative above, which can be
        # internally contradictory (cbabbf9c's plan said BOTH "extend, no new messages" AND "4 new
        # XSDs"). The TSD is generated after the XSD freeze so this populates for it; the BRD runs
        # earlier and gets an empty (harmless) section. Best-effort — the contract stands without it.
        try:
            from app.models.agentic import AgenticRun, ChangeManifest
            xrun = (db.query(AgenticRun)
                    .filter(AgenticRun.change_request_id == change_request_id, AgenticRun.kind == "xsd")
                    .order_by(AgenticRun.created_at.desc()).first())
            frozen: list[str] = []
            if xrun is not None:
                man = (db.query(ChangeManifest).filter(ChangeManifest.run_id == xrun.id)
                       .order_by(ChangeManifest.created_at.desc()).first())
                for op in ((man.operations if man else None) or []):
                    p = (op.get("path") or "") if isinstance(op, dict) else ""
                    if p.lower().endswith((".xsd", ".xjb")):
                        frozen.append(f"{(op.get('op') or 'modify')} {p.rsplit('/', 1)[-1]}")
            if frozen:
                parts.append(
                    "FROZEN WIRE CONTRACT (Phase A human-approved — AUTHORITATIVE; OVERRIDES the "
                    "wire/schema narrative above): the schema actually frozen for this change is EXACTLY: "
                    + "; ".join(sorted(set(frozen))[:30]) + ". Describe the wire/API surface against THIS "
                    "set only — a schema file listed here IS changed (never call it 'unchanged' / 'not "
                    "modified'); do NOT describe a new top-level message or schema that is not frozen here.")
        except Exception:  # noqa: BLE001 — best-effort; the plan contract still stands without it
            pass

        # API surface / messages the plan defines.
        msgs = flow.get("messages")
        if msgs:
            parts.append("MESSAGES / API SURFACE (the ONLY ones this change adds or touches): "
                         + str(msgs)[:900])
        steps = flow.get("steps") or flow.get("flow")
        if steps:
            parts.append("FLOW STEPS: " + str(steps)[:900])
        inv = [f"{i.get('repo', '')}:{i.get('path', '')}" for i in (ta.get("schema_inventory") or [])
               if isinstance(i, dict) and i.get("path")]
        if inv:
            parts.append("SCHEMA FILES IN SCOPE: " + ", ".join(inv[:30]))
        for key, label in (("constraints", "CONSTRAINTS"), ("risks", "RISKS")):
            if ta.get(key):
                parts.append(f"{label}: " + str(ta[key])[:500])
        if fp.get("assumptions"):
            parts.append("RATIFIED ASSUMPTIONS (honour; do not re-decide): " + str(fp["assumptions"])[:600])

        return "\n\n".join(parts)[:7000]   # headroom for the FROZEN WIRE CONTRACT section (TSD only)
    except Exception as e:  # noqa: BLE001 — best-effort; never break doc generation
        logger.warning("build_plan_contract failed for %s: %s", change_request_id, e)
        return ""
