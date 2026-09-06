# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""JAXB binding model — the element→Java link (THE BOOK §7.2/§7.3).

Models the JAXB toolchain so XSD→Java links are *correct, not guessed*. Each
link carries **evidence + confidence + provenance**, ordered by tier:

* ``@XmlRootElement(name=)`` exact ............ 0.95  (jaxb_root)
* ``ObjectFactory`` ``@XmlElementDecl(name=)`` . 0.92  (object_factory)
* ``@XmlType(name=)`` ......................... 0.90  (jaxb_root, type)
* external ``.xjb`` binding ................... 0.90  (xjb)
* ``@XmlElement(name=)`` field ................ 0.85  (jaxb_root, field)

A hard floor (``xsd_link_min_confidence`` = 0.55) routes weaker links to a
*verification list* — never presented as definite (§7.3). The high-confidence
tiers here all sit above it; lower-confidence sources (catalogue/doc/impact/RAG)
are added by other slices and pass through the same :func:`split_by_confidence`.

Generated JAXB sources are DO-NOT-EDIT (§7.2/§7.5); :func:`parse_pom_jaxb` reports
the plugin's generated-source output dir so verification (S10) can regenerate
before compile.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.core.config import settings

logger = logging.getLogger("app.agentic")

# Confidence tiers (§7.3) — named so a tier change is a one-line edit.
_C_ROOT = 0.95
_C_ROOT_DERIVED = 0.85
_C_OBJECT_FACTORY = 0.92
_C_XML_TYPE = 0.90
_C_XJB = 0.90
_C_XML_ELEMENT = 0.85

# JAXB annotation patterns (regex is pragmatic for Java; the structural extractor
# in §7.2 sharpens this later). DOTALL where the annotation may wrap lines.
_RE_ROOT = re.compile(r"@XmlRootElement\s*\(\s*[^)]*?name\s*=\s*\"([^\"]+)\"", re.S)
_RE_TYPE = re.compile(r"@XmlType\s*\(\s*[^)]*?name\s*=\s*\"([^\"]+)\"", re.S)
_RE_ELEMENT = re.compile(r"@XmlElement\s*\(\s*[^)]*?name\s*=\s*\"([^\"]+)\"", re.S)
_RE_ELEMENT_DECL = re.compile(r"@XmlElementDecl\s*\(\s*[^)]*?name\s*=\s*\"([^\"]+)\"", re.S)
_RE_CLASS = re.compile(r"\b(?:public\s+|final\s+|abstract\s+)*class\s+(\w+)")


@dataclass
class JaxbLink:
    xpath: str          # the XSD-side key (element or type name)
    symbol: str         # clone-relative Java/.xjb path (+class where known)
    source: str         # XsdLinkSource value
    confidence: float
    evidence: dict = field(default_factory=dict)


def _class_name(java: str) -> str | None:
    m = _RE_CLASS.search(java)
    return m.group(1) if m else None


def extract_java_links(java_content: str, java_path: str) -> list[JaxbLink]:
    """Element/type→Java links from one Java source file."""
    links: list[JaxbLink] = []
    cls = _class_name(java_content)
    is_object_factory = "@XmlRegistry" in java_content or (cls == "ObjectFactory")
    symbol = f"{java_path}::{cls}" if cls else java_path

    named_roots = _RE_ROOT.findall(java_content)
    for name in named_roots:
        links.append(JaxbLink(name, symbol, "jaxb_root", _C_ROOT, {"anno": "XmlRootElement", "class": cls}))
    if not named_roots and "@XmlRootElement" in java_content and cls:
        # @XmlRootElement with NO name attribute (bare OR namespace-only) → JAXB
        # derives the element name from the class (first letter lowercased).
        derived = cls[0].lower() + cls[1:]
        links.append(JaxbLink(derived, symbol, "jaxb_root", _C_ROOT_DERIVED,
                              {"anno": "XmlRootElement", "derived_from_class": cls}))
    for name in _RE_TYPE.findall(java_content):
        links.append(JaxbLink(name, symbol, "jaxb_root", _C_XML_TYPE, {"anno": "XmlType", "class": cls}))
    if is_object_factory:
        for name in _RE_ELEMENT_DECL.findall(java_content):
            links.append(JaxbLink(name, symbol, "object_factory", _C_OBJECT_FACTORY,
                                  {"anno": "XmlElementDecl", "class": cls}))
    else:
        for name in _RE_ELEMENT.findall(java_content):
            links.append(JaxbLink(name, symbol, "jaxb_root", _C_XML_ELEMENT, {"anno": "XmlElement", "class": cls}))
    return links


def extract_xjb_links(xjb_content: str, xjb_path: str) -> list[JaxbLink]:
    """External .xjb binding customizations → links (schemaLocation + node + class)."""
    from lxml import etree
    links: list[JaxbLink] = []
    try:
        root = etree.fromstring(xjb_content.encode("utf-8"),
                                etree.XMLParser(resolve_entities=False, recover=True))
    except Exception:
        return links
    if root is None:
        return links
    # A meaningful element link is a bindings element with a `node` XPath into the
    # schema. Read the class from DIRECT children only — iterating descendants
    # would pull a nested binding's class up and double-count it. (The outer
    # schemaLocation-only binding has no `node` and is correctly skipped.)
    for b in root.iter():
        if etree.QName(b).localname != "bindings":
            continue
        node_xpath = b.get("node")
        if not node_xpath:
            continue
        cls = next((c.get("name") for c in b
                    if etree.QName(c).localname == "class" and c.get("name")), None)
        links.append(JaxbLink(node_xpath, f"{xjb_path}{('::' + cls) if cls else ''}",
                              "xjb", _C_XJB, {"binding": "xjb", "class": cls}))
    return links


def build_links(java_files: list[dict] | None = None,
                xjb_files: list[dict] | None = None) -> list[JaxbLink]:
    """All links from a repo's ``[{path, content}]`` Java + .xjb files."""
    out: list[JaxbLink] = []
    for f in java_files or []:
        out.extend(extract_java_links(f["content"], f["path"]))
    for f in xjb_files or []:
        out.extend(extract_xjb_links(f["content"], f["path"]))
    return out


_C_IMPACT = 0.45      # KG impact tier (§7.3, ×0.7-ish) — BELOW the 0.55 floor
_C_RAG = 0.40         # semantic retrieval tier — BELOW the floor


def advisory_links(db, *, change_description: str | None = None,
                   element_names: list[str] | None = None) -> list[JaxbLink]:
    """Lower-confidence element→Java link CANDIDATES (§7.3 impact/rag tiers).

    These are deliberately BELOW the 0.55 floor, so :func:`split_by_confidence`
    routes them to the "needs confirmation" list — they are NEVER presented as
    definite (the accuracy safeguard). Fail-open: if the KG / retrieval
    subsystems are absent or error, returns whatever it could (often [])."""
    out: list[JaxbLink] = []
    if change_description:
        try:
            from app.kg.impact_analyzer import analyze_impact
            rep = analyze_impact(db=db, change_description=change_description[:3000])
            for f in (getattr(rep, "files_affected", None) or [])[:15]:
                out.append(JaxbLink("(change)", str(f), "impact", _C_IMPACT, {"tier": "impact"}))
        except Exception as e:  # noqa: BLE001 — KG/AGE may be unavailable
            logger.debug("advisory impact tier skipped: %s", e)
    for name in (element_names or [])[:5]:
        try:
            from app.rag.retrieval import retrieve
            for h in (retrieve(name, db, top_k=3) or []):
                sf = getattr(h, "source_file", None) or (h.get("source_file") if isinstance(h, dict) else None)
                if sf:
                    out.append(JaxbLink(name, str(sf), "rag", _C_RAG, {"tier": "rag"}))
        except Exception as e:  # noqa: BLE001 — retrieval may be unavailable
            logger.debug("advisory rag tier skipped: %s", e)
    return out


def split_by_confidence(links: list[JaxbLink], floor: float | None = None
                        ) -> tuple[list[JaxbLink], list[JaxbLink]]:
    """``(definite, needs_confirmation)`` split at the confidence floor (§7.3).
    Below the floor is surfaced as "needs confirmation", never as definite."""
    f = settings.xsd_link_min_confidence if floor is None else floor
    definite = [l for l in links if l.confidence >= f]
    needs = [l for l in links if l.confidence < f]
    return definite, needs


# ── pom.xml JAXB plugin config (§7.2/§7.5) ────────────────────────────────────────

_RE_JAXB_PLUGIN = re.compile(r"<artifactId>\s*(jaxb2-maven-plugin|cxf-xjc-plugin|maven-jaxb2-plugin)\s*</artifactId>")
_RE_OUTPUT_DIR = re.compile(r"<(?:outputDirectory|generateDirectory|sourceRoot)>\s*([^<]+?)\s*</", re.S)


def parse_pom_jaxb(pom_content: str) -> dict | None:
    """Detect a JAXB codegen plugin + its generated-source output dir, so S10 can
    regenerate sources before compile. None when no JAXB plugin is configured."""
    m = _RE_JAXB_PLUGIN.search(pom_content or "")
    if not m:
        return None
    # Scope the output-dir search to THIS plugin's <plugin>…</plugin> block so a
    # different plugin's <outputDirectory> (e.g. maven-compiler → target/classes)
    # isn't mistaken for the JAXB generated-source dir.
    block = pom_content[m.start():]
    end = block.find("</plugin>")
    if end != -1:
        block = block[:end]
    out = _RE_OUTPUT_DIR.search(block)
    return {
        "plugin": m.group(1),
        "output_dir": (out.group(1).strip() if out else "target/generated-sources/jaxb"),
    }


# ── Persist (idempotent rebuild per repo) ─────────────────────────────────────

def persist_links(db, repo_id: str, links: list[JaxbLink], base_commit_sha: str | None = None) -> int:
    """Replace the repo's xsd_java_links with ``links``. Returns the count."""
    from app.models.xsd_graph import XsdJavaLink

    db.query(XsdJavaLink).filter(XsdJavaLink.repo_id == repo_id).delete(synchronize_session=False)
    for l in links:
        db.add(XsdJavaLink(
            repo_id=repo_id, xpath=l.xpath, symbol_chunk_id_or_path=l.symbol,
            source=l.source, confidence=l.confidence, evidence_json=l.evidence,
            base_commit_sha=base_commit_sha,
        ))
    db.flush()
    return len(links)
