# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Regenerate a TSD for a change through the docgen pipeline (no prior-TSD anchor) so we can grade
whether the new TSD prompts + technical-design threading actually improved it. Usage:
  python scripts/regen_tsd.py <change_id> <out_path>
"""
import asyncio
import sys

from app.core.database import SessionLocal
from app.services.docgen_runner import (
    build_initial_state, run_pipeline_in_thread, sections_to_markdown)
from app.services.doc_skeleton import brd_flow_skeleton
from app.api.agents import _tech_design_block, _decisions_block, _flow_spec, _latest_xsd_content
from app.models.change_request import ChangeRequest
from app.models.brd import BRD
from app.models.canvas import ProductCanvas

CID = sys.argv[1] if len(sys.argv) > 1 else "be258e9e-fc02-49d0-b9fd-211d58e62aea"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/regen_tsd.md"


async def main():
    db = SessionLocal()
    cr = db.get(ChangeRequest, CID)
    brd = (db.query(BRD).filter(BRD.change_request_id == CID)
           .order_by(BRD.version.desc()).first())
    brd_content = (brd.content if brd else "") or ""
    canvas = (db.query(ProductCanvas).filter(ProductCanvas.change_request_id == CID)
              .order_by(ProductCanvas.version.desc()).first())
    canvas_content = (canvas.content if canvas else "") or ""

    parts = []
    if brd_content:
        parts.append("--- Approved BRD ---\n" + brd_content)
    additional_context = "\n\n".join(parts)

    state = build_initial_state(
        doc_type="TSD", change_id=CID, prompt=(cr.initial_prompt if cr else ""),
        document_title=f"Technical Specification: {cr.title if cr else CID}",
        audience="Tech Leads, Architects, InfoSec, Risk",
        desired_outcome="Approved Technical Specification",
        research_report="", canvas_content=canvas_content,
        additional_context=additional_context, include_diagrams=True, use_rag=True,
        proposals={}, source_skeleton=(brd_flow_skeleton(brd_content) if brd_content else ""),
        decisions_block=_decisions_block(CID, db), source_flow_spec=_flow_spec(CID, db),
        source_xsd=_latest_xsd_content(CID, db),
        # The key lever — the ratified technical design as a DEDICATED untruncated field.
        tech_design=_tech_design_block(CID, db))

    final = await run_pipeline_in_thread(state)
    plan = final.get("document_plan") or {}
    secs = final.get("generated_sections") or []
    md = sections_to_markdown(plan, secs)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(md)
    print("STATUS:", final.get("status"))
    print("LEN:", len(md))
    print("HEADINGS:", [s.get("heading") for s in (plan.get("sections") or [])])
    db.close()


asyncio.run(main())
