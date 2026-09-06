# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Regenerate a BRD through the docgen pipeline (no tier override → the classifier picks the tier)
so we can grade whether the bloat fix actually produced a leaner doc. Usage:
  python scripts/regen_brd.py <change_id> <out_path>
"""
import asyncio
import sys

from app.core.database import SessionLocal
from app.services.docgen_runner import (
    build_initial_state, run_pipeline_in_thread, sections_to_markdown)
from app.api.agents import _decisions_block, _flow_spec
from app.models.change_request import ChangeRequest
from app.models.canvas import ProductCanvas

CID = sys.argv[1] if len(sys.argv) > 1 else "be258e9e-fc02-49d0-b9fd-211d58e62aea"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/regen_brd.md"


async def main():
    db = SessionLocal()
    cr = db.get(ChangeRequest, CID)
    canvas = (db.query(ProductCanvas).filter(ProductCanvas.change_request_id == CID)
              .order_by(ProductCanvas.version.desc()).first())
    canvas_content = (canvas.content if canvas else "") or ""

    state = build_initial_state(
        doc_type="BRD", change_id=CID, prompt=(cr.initial_prompt if cr else ""),
        document_title=f"BRD: {cr.title if cr else CID}",
        audience="Product Managers, Tech Leads, InfoSec, Risk",
        desired_outcome="Approved BRD",
        research_report="", canvas_content=canvas_content,
        additional_context="", include_diagrams=True, use_rag=True,
        brd_tier_override=None,                      # let the classifier pick — the thing we fixed
        proposals={}, decisions_block=_decisions_block(CID, db),
        source_flow_spec=_flow_spec(CID, db))

    final = await run_pipeline_in_thread(state)
    plan = final.get("document_plan") or {}
    secs = final.get("generated_sections") or []
    md = sections_to_markdown(plan, secs)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(md)
    print("STATUS:", final.get("status"))
    print("TIER:", state.get("brd_tier"))
    print("LEN:", len(md))
    print("SECTION_COUNT:", len(plan.get("sections") or []))
    db.close()


asyncio.run(main())
