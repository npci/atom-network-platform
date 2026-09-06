# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""XSD context for TSD generation — the input-side fix for XSD↔TSD drift.

Three jobs, one module:

1. ``xsd_stage_complete`` — the compulsory XSD-before-TSD ordering gate. The v2
   stage flow already puts XSD ahead of TECH_SPEC, but nothing stopped the TSD
   WebSocket from generating while the XSD stage was untouched. This is the
   enforcement point.

2. ``build_involved_xsd_bundle`` — resolves the schemas *involved in the flow*
   (not just the changed one) and packages them as per-schema blocks for the
   TSD writer. ``source_xsd`` (api.agents._latest_xsd_content) already carries
   the CHANGED schema; the historical gap is that the TSD never saw the
   unchanged siblings the flow rides on (the RespTransfer to a changed ReqTransfer, the
   imported common-types schema), so it re-invented their structure. Candidates
   come from the approved ChangeAnalysis (flow_spec message tokens + the
   technical_analysis schema_inventory) and the XSD schema graph
   (include/import neighbours of the changed files); content comes from the
   agentic run's workspace clone when it is still on disk, else from ingested
   KB chunks (existing_xsds / npci_xml_spec categories).

3. ``xml_tag_tripwire`` — an ADVISORY post-check (deliberately not a gate):
   XML element tags appearing in the generated TSD must exist somewhere in the
   schemas it was grounded on. When the input-side grounding works this never
   fires; when the plumbing regresses it names the invented tags immediately.
"""
from __future__ import annotations

import logging
import posixpath
import re

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

def _msg_tokens(text: str) -> list[str]:
    """Wire-message tokens (UPI: ReqTransfer, RespAuthDetail, …) as they appear in
    flow_spec routes, plan text, and schema file names. The token shape is the
    active pack's ``message_name_pattern``; a pack that declares none finds no
    tokens, so the bundle simply carries no message-matched schemas."""
    from app.core.domain.contract import message_name_pattern_of
    from app.core.domain.registry import get_active_pack

    pattern = message_name_pattern_of(get_active_pack())
    if pattern is None or not text:
        return []
    return [m.group(0) for m in pattern.finditer(text)]
_XSD_NAME_RE = re.compile(r"\b([A-Za-z0-9._\-]+\.xsd)\b")
_ANY_FENCE_RE = re.compile(r"```[a-zA-Z]*\s*\n(.*?)```", re.DOTALL)
# Captures (prefix, localname) so namespaced sample tags (<network:ReqTransfer>) are visible
# to the tripwire; xs/xsd/xsi-prefixed tags are schema scaffolding, not payload.
_XML_TAG_RE = re.compile(r"<\s*(?:([A-Za-z][A-Za-z0-9_]*):)?([A-Za-z][A-Za-z0-9_]*)[\s/>]")
_XS_NAME_RE = re.compile(
    r"<\s*(?:\w+:)?(?:element|complexType|attribute|simpleType|group)\b[^>]*?\bname\s*=\s*\"([^\"]+)\"")

# Per-schema and whole-bundle size budgets. A schema over the per-file cap is
# summarised to its element dictionary instead of being clipped mid-element.
_PER_FILE_CAP = 15_000
_BUNDLE_CAP = 60_000


# ── Stage gate ────────────────────────────────────────────────────────────────

def xsd_stage_complete(change_id: str, db: Session) -> tuple[bool, str]:
    """Has the XSD stage produced its verdict/artifact for this change?

    Complete when ANY of:
      * an agentic ``kind="xsd"`` run froze its Phase-A scope (handoff_json has
        ``xsd_scope`` — covers both "schemas realized" and "no schema change
        needed", which is a valid stage outcome);
      * the doc-table ``xsds`` row says NOT required;
      * the doc-table row says required AND actual schema blocks were generated
        (not just the assessment text).

    Returns (ok, reason) — reason is user-facing when not ok.
    """
    try:
        from app.models.agentic import AgenticRun
        run = (db.query(AgenticRun)
               .filter(AgenticRun.change_request_id == change_id, AgenticRun.kind == "xsd")
               .order_by(AgenticRun.created_at.desc()).first())
        if run and (getattr(run, "handoff_json", None) or {}).get("xsd_scope") is not None:
            return True, ""
    except Exception as e:  # noqa: BLE001
        logger.warning("xsd_stage_complete: agentic lookup failed change=%s (%s)", change_id, e)

    try:
        from app.models.xsd import XSD
        from app.services.xsd_bundle import extract_xsd_blocks
        row = (db.query(XSD).filter(XSD.change_request_id == change_id)
               .order_by(XSD.version.desc()).first())
        if row:
            if row.is_required is False:
                return True, ""
            if extract_xsd_blocks(row.content or ""):
                return True, ""
            return False, ("The XSD stage says schema changes are REQUIRED but no schema "
                           "has been generated yet. Complete the XSD stage first.")
    except Exception as e:  # noqa: BLE001
        logger.warning("xsd_stage_complete: doc-table lookup failed change=%s (%s)", change_id, e)

    return False, ("The XSD stage has not run for this change. Run the XSD assessment "
                   "(and generate the schema if required) before the Tech Spec — the TSD "
                   "is authored against the approved schemas.")


# ── Involved-schema bundle ────────────────────────────────────────────────────

def _candidate_names(change_id: str, db: Session) -> set[str]:
    """Message tokens + .xsd file names named by the approved analysis
    (flow_spec routes + technical_analysis schema_inventory / plan text)."""
    names: set[str] = set()
    try:
        from app.models.change_analysis import ChangeAnalysis
        ca = (db.query(ChangeAnalysis).filter(ChangeAnalysis.change_request_id == change_id)
              .order_by(ChangeAnalysis.version.desc()).first())
        if not ca:
            return names
        for blob in (ca.flow_spec or {}, (ca.technical_analysis or {}).get("schema_inventory")):
            text = str(blob or "")
            names.update(_msg_tokens(text))
            names.update(_XSD_NAME_RE.findall(text))
    except Exception as e:  # noqa: BLE001
        logger.warning("xsd bundle: candidate extraction failed change=%s (%s)", change_id, e)
    return names


def _changed_paths(handoff: dict) -> set[str]:
    """repo-relative paths Phase A edited/created ('repo_id:path' tokens)."""
    scope = (handoff or {}).get("xsd_scope") or {}
    out: set[str] = set()
    for token in list(scope.get("edits_applied") or []) + list(scope.get("created") or []):
        out.add(str(token).rsplit(":", 1)[-1])
    return out


def _read_workspace_file(run_id: str, repo_id: str, path: str) -> str:
    """Best-effort read from the agentic run's clone (may be GC'd)."""
    try:
        from app.agents import workspace_local
        base = workspace_local.repo_dir(run_id, repo_id).resolve()
        target = (base / path).resolve()
        if not target.is_relative_to(base):  # DB-sourced path — refuse traversal
            return ""
        return target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
    except Exception:  # noqa: BLE001
        return ""


def _kb_schema_content(name: str, db: Session) -> tuple[str, str]:
    """Reassemble an ingested schema (KB existing_xsds / npci_xml_spec chunks)
    whose source file matches ``name``. Returns (source_file, content).

    Exact-file match first: a bare ``%name%`` for token ReqTransfer resolves to whichever
    sibling the row ordering surfaces first (ReqTransferAdvice.xsd), silently displacing
    the schema actually asked for. Substring stays as the last-resort fallback for
    unconventionally named source files."""
    try:
        from app.models.document_chunk import DocCategory, DocumentChunk
        fname = name if name.lower().endswith(".xsd") else f"{name}.xsd"
        rows = []
        for pattern in (fname, f"%/{fname}", f"%{fname}", f"%{name}%"):
            rows = (db.query(DocumentChunk)
                    .filter(DocumentChunk.doc_category.in_([DocCategory.XSD, DocCategory.AUTHORITY_XML_SPEC]),
                            DocumentChunk.source_file.ilike(pattern))
                    .order_by(DocumentChunk.source_file, DocumentChunk.chunk_index)
                    .limit(50).all())
            if rows:
                break
        if not rows:
            return "", ""
        src = rows[0].source_file
        body = "\n".join(r.content for r in rows if r.source_file == src)
        return src, body
    except Exception as e:  # noqa: BLE001
        logger.warning("xsd bundle: KB lookup failed for %s (%s)", name, e)
        return "", ""


def _element_summary(content: str) -> str:
    """Readable element dictionary for a schema too large to inline: every named
    top-level element/complexType with its child (name:type) pairs."""
    try:
        from lxml import etree
        parser = etree.XMLParser(resolve_entities=False, recover=True)
        root = etree.fromstring(content.encode("utf-8"), parser=parser)
        if root is None:
            return ""
        ns = {"xs": "http://www.w3.org/2001/XMLSchema"}
        lines: list[str] = []
        for kind in ("element", "complexType"):
            for node in root.findall(f"xs:{kind}", ns):
                name = node.get("name")
                if not name:
                    continue
                children = [f"{c.get('name')}:{c.get('type') or '-'}"
                            for c in node.findall(".//xs:element", ns) if c.get("name")]
                attrs = [f"@{a.get('name')}" for a in node.findall(".//xs:attribute", ns) if a.get("name")]
                lines.append(f"  {kind} {name}: " + ", ".join(children + attrs))
        return "\n".join(lines[:200])
    except Exception:  # noqa: BLE001
        return ""


def build_involved_xsd_bundle(change_id: str, db: Session,
                              max_total_chars: int = _BUNDLE_CAP) -> str:
    """Per-schema context blocks for every UNCHANGED schema involved in the flow.

    The changed schema itself already reaches the TSD via ``source_xsd`` — this
    bundle deliberately excludes it and adds the surrounding flow: schemas whose
    file name matches a flow_spec/plan message token, plus include/import
    neighbours of the changed files from the XSD schema graph.
    """
    names = _candidate_names(change_id, db)

    run = None
    handoff: dict = {}
    try:
        from app.models.agentic import AgenticRun
        run = (db.query(AgenticRun)
               .filter(AgenticRun.change_request_id == change_id, AgenticRun.kind == "xsd")
               .order_by(AgenticRun.created_at.desc()).first())
        handoff = (getattr(run, "handoff_json", None) or {}) if run else {}
    except Exception as e:  # noqa: BLE001
        logger.warning("xsd bundle: agentic run lookup failed change=%s (%s)", change_id, e)

    changed = _changed_paths(handoff)
    changed_names = {posixpath.basename(p) for p in changed}

    # Resolve candidates against the schema graph: name matches + graph
    # neighbours of the changed files (both edge directions — what a changed
    # schema includes/imports, and what includes/imports it).
    involved: dict[str, tuple[str, str]] = {}   # path -> (repo_id, node_id)
    try:
        from app.models.xsd_graph import XsdSchemaEdge, XsdSchemaNode
        nodes = []
        for n in sorted(names):
            # Exact-basename first — token ReqTransfer must resolve to ReqTransfer.xsd, not the
            # ReqTransferAdvice.xsd sibling; substring only when no exact file exists.
            fname = n if n.lower().endswith(".xsd") else f"{n}.xsd"
            exact = (db.query(XsdSchemaNode)
                     .filter(XsdSchemaNode.path.ilike(f"%{fname}")).limit(5).all())
            nodes += exact or (db.query(XsdSchemaNode)
                               .filter(XsdSchemaNode.path.ilike(f"%{n}%")).limit(5).all())
        changed_nodes = []
        for p in sorted(changed):
            changed_nodes += (db.query(XsdSchemaNode)
                              .filter(XsdSchemaNode.path.ilike(f"%{posixpath.basename(p)}"))
                              .limit(3).all())
        neighbour_ids: set[str] = set()
        if changed_nodes:
            ids = [n.id for n in changed_nodes]
            for e in (db.query(XsdSchemaEdge)
                      .filter((XsdSchemaEdge.from_node_id.in_(ids))
                              | (XsdSchemaEdge.to_node_id.in_(ids))).limit(100).all()):
                neighbour_ids.update(x for x in (e.from_node_id, e.to_node_id) if x)
        if neighbour_ids:
            nodes += (db.query(XsdSchemaNode)
                      .filter(XsdSchemaNode.id.in_(list(neighbour_ids))).all())
        for node in nodes:
            if posixpath.basename(node.path) in changed_names or node.path in changed:
                continue  # the changed schema rides in source_xsd, not here
            involved.setdefault(node.path, (node.repo_id, node.id))
    except Exception as e:  # noqa: BLE001
        logger.warning("xsd bundle: schema-graph resolution failed change=%s (%s)", change_id, e)

    blocks: list[str] = []
    used = 0
    covered_names: set[str] = set()
    dropped: list[str] = []

    def _append(label: str, body: str) -> None:
        nonlocal used
        if not body.strip():
            return
        if len(body) > _PER_FILE_CAP:
            summary = _element_summary(body)
            body = (summary and
                    f"(schema too large to inline — element dictionary)\n{summary}") or body[:_PER_FILE_CAP]
        block = f"<!-- INVOLVED SCHEMA (unchanged — flow context): {label} -->\n{body}"
        if used + len(block) > max_total_chars:
            dropped.append(label)
            return
        blocks.append(block)
        used += len(block)

    for path, (repo_id, _nid) in sorted(involved.items()):
        content = _read_workspace_file(run.id, repo_id, path) if run else ""
        if content:
            _append(path, content)
            covered_names.add(posixpath.basename(path))
            covered_names.update(_msg_tokens(posixpath.basename(path)))
        else:
            src, body = _kb_schema_content(posixpath.basename(path), db)
            if body:
                _append(src or path, body)
                covered_names.add(posixpath.basename(path))

    # Message tokens with no graph hit (no repo indexed / pure doc flow) — try
    # the KB directly so e.g. RespTransfer still grounds the flow narrative. Coverage is
    # by exact stem, not substring: ReqTransferAdvice.xsd in the bundle must not mark
    # ReqTransfer as already covered (that silently skipped the real ReqTransfer.xsd).
    covered_stems = {(_c[:-4] if _c.lower().endswith(".xsd") else _c).lower() for _c in covered_names}
    for n in sorted(names):
        n_stem = (n[:-4] if n.lower().endswith(".xsd") else n).lower()
        if n in covered_names or n in changed_names or n_stem in covered_stems:
            continue
        src, body = _kb_schema_content(n, db)
        if body:
            _append(src or n, body)
            covered_names.add(n)
            covered_stems.add(n_stem)

    if dropped:
        blocks.append("<!-- NOTE: involved schemas omitted for size (element structure not shown): "
                      + ", ".join(dropped[:10]) + " -->")
        logger.info("xsd bundle: change=%s dropped %d schema(s) over budget: %s",
                    change_id, len(dropped), dropped[:10])
    if blocks:
        logger.info("xsd bundle: change=%s schemas=%d chars=%d", change_id, len(blocks), used)
    return "\n\n".join(blocks)


# ── Advisory tripwire ─────────────────────────────────────────────────────────

def _xmlish_fences(markdown: str):
    """Every fenced code block whose body looks like XML — regardless of the
    fence's language tag. TSD writers emit samples as ```xml, bare ``` and even
    ```java blocks; inspecting only ```xml made the tripwire structurally blind."""
    for body in _ANY_FENCE_RE.findall(markdown or ""):
        b = body.strip()
        if b.startswith("<") and ("</" in b or "/>" in b):
            yield b


def _fence_tags(text: str):
    """Local names of the element tags in an XML-looking fence, namespace prefixes
    stripped; XML-Schema scaffolding (xs:/xsd:/xsi:-prefixed) excluded."""
    for prefix, local in _XML_TAG_RE.findall(text):
        if prefix.lower() in ("xs", "xsd", "xsi"):
            continue
        yield local


def xml_tag_tripwire(tsd_markdown: str, *schema_sources: str) -> list[str]:
    """XML element tags used in the TSD's XML-looking samples that are not
    defined in any supplied schema text. Advisory only — callers log, never block."""
    known: set[str] = set()
    for src in schema_sources:
        known.update(_XS_NAME_RE.findall(src or ""))
        # Samples embedded in KB spec material count as ground truth too.
        for fence in _xmlish_fences(src):
            known.update(_fence_tags(fence))
    if not known:
        return []
    unknown: set[str] = set()
    for fence in _xmlish_fences(tsd_markdown):
        for tag in _fence_tags(fence):
            if tag not in known and not tag.startswith(("xs", "xsd")):
                unknown.add(tag)
    return sorted(unknown)
