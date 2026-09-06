# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Deterministic XSD → API Registry ingest.

Parses the platform's real UPI schemas (workspace repo clones or the KB
``existing_xsds`` folder) into ``ApiMessage``/``ApiField`` rows — NO LLM
anywhere in this path. Two-pass: collect named simple/complex types across
every ``.xsd`` in the folder (UPI messages include ``network-common.xsd``), then
expand each top-level ``xs:element`` into a field tree with occurrence,
datatype, length facets, enumerations and mandatoriness.

Re-runs are idempotent. A human-edited row (``updated_by`` set) keeps its
canonical values — the fresh parse only refreshes its ``constraint_sources``
evidence, so the UI can surface drift instead of silently clobbering edits.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from xml.etree import ElementTree as ET  # kept for Element type hints + ParseError

# Ingest dirs are repo clones — a malicious .xsd could carry an entity-expansion
# ("billion laughs") / external-entity payload that stdlib ET does not defend
# against. defusedxml.parse forbids entities + external refs; the tree it returns
# is still standard ElementTree Elements, so the rest of the parser is unchanged.
from defusedxml.ElementTree import parse as _safe_xml_parse
from defusedxml.common import DefusedXmlException

logger = logging.getLogger(__name__)

XS = "{http://www.w3.org/2001/XMLSchema}"
_MAX_DEPTH = 14


# ---------------------------------------------------------------------------
# Parse model
# ---------------------------------------------------------------------------

@dataclass
class FieldSpec:
    xml_tag: str
    is_attribute: bool = False
    occurrence: str = "1..1"
    mandatory: str = "Y"
    datatype: str | None = None
    length_rule: str | None = None
    enum_values: list[str] | None = None
    facts: dict = dc_field(default_factory=dict)   # raw XSD facts → constraint_sources
    children: list["FieldSpec"] = dc_field(default_factory=list)
    tag_num: str = ""


@dataclass
class MessageSpec:
    api_name: str
    namespace: str | None
    schema_file: str
    root: FieldSpec
    # Prefix the SOURCE schema binds to its own targetNamespace, "" when it binds
    # the target as the DEFAULT namespace (`xmlns="…"`). Read off the real file —
    # see `_ns_prefix_for`. Never a literal: the sample XML must echo the schema
    # it was built from, whatever ecosystem that schema belongs to.
    ns_prefix: str = ""


def _local(name: str | None) -> str | None:
    """Strip any namespace prefix from a QName ('ns:headType' → 'headType')."""
    if not name:
        return name
    return name.split(":")[-1]


def _ns_prefix_for(xsd_text: str, tns: str | None) -> str:
    """The prefix the schema binds to ``tns``; "" when bound as the default namespace.

    ElementTree consumes `xmlns:*` declarations — they never reach `root.attrib` —
    so the binding is recovered from the raw source instead of re-parsing.

    WHY THIS EXISTS. `build_sample_xml` used to emit a hardcoded `upi:` prefix
    while taking the URI from the schema, so every generated sample read
    `<upi:ReqMemberVerify xmlns:upi="http://nlln.in/schema/v1">` — a prefix from
    one ecosystem stapled to another's namespace. The prefix is a property of the
    schema, so it is read from the schema.
    """
    if not tns:
        return ""
    quoted = re.escape(tns)
    m = re.search(rf'xmlns:([A-Za-z_][\w.-]*)\s*=\s*["\']{quoted}["\']', xsd_text)
    if m:
        return m.group(1)
    return ""    # bound as the default namespace (or not bound) → unprefixed sample


# ---------------------------------------------------------------------------
# Pass 1 — folder-wide type registry
# ---------------------------------------------------------------------------

class TypeRegistry:
    def __init__(self):
        self.simple: dict[str, dict] = {}      # name → facts
        self.complex: dict[str, ET.Element] = {}
        # (name, node, file, tns, ns_prefix)
        self.top_elements: list[tuple[str, ET.Element, str, str | None, str]] = []

    @staticmethod
    def build(xsd_paths: list[Path]) -> "TypeRegistry":
        reg = TypeRegistry()
        for p in sorted(xsd_paths):
            try:
                tree = _safe_xml_parse(p)
            except (ET.ParseError, DefusedXmlException) as e:
                logger.warning("api-registry ingest: skipping unparseable/unsafe %s (%s)", p, e)
                continue
            root = tree.getroot()
            tns = root.get("targetNamespace")
            try:
                ns_prefix = _ns_prefix_for(p.read_text(encoding="utf-8", errors="replace"), tns)
            except OSError:  # unreadable after a successful parse — fall back to unprefixed
                logger.warning("api-registry ingest: could not re-read %s for its namespace prefix", p)
                ns_prefix = ""
            for child in root:
                name = child.get("name")
                if child.tag == f"{XS}simpleType" and name:
                    reg.simple[name] = _restriction_facts(child)
                elif child.tag == f"{XS}complexType" and name:
                    reg.complex[name] = child
                elif child.tag == f"{XS}element" and name:
                    reg.top_elements.append((name, child, str(p), tns, ns_prefix))
        return reg


def _restriction_facts(simple_type_node: ET.Element) -> dict:
    facts: dict = {}
    restriction = simple_type_node.find(f"{XS}restriction")
    if restriction is None:
        return facts
    facts["base"] = _local(restriction.get("base"))
    enums = [e.get("value") for e in restriction.findall(f"{XS}enumeration") if e.get("value") is not None]
    if enums:
        facts["enums"] = enums
    for facet, key in (("minLength", "min_length"), ("maxLength", "max_length"),
                       ("length", "length"), ("pattern", "pattern"),
                       ("minInclusive", "min_inclusive"), ("maxInclusive", "max_inclusive"),
                       ("totalDigits", "total_digits"), ("fractionDigits", "fraction_digits")):
        node = restriction.find(f"{XS}{facet}")
        if node is not None and node.get("value") is not None:
            facts[key] = node.get("value")
    return facts


# ---------------------------------------------------------------------------
# Facts → documented constraint cells
# ---------------------------------------------------------------------------

def _datatype_label(facts: dict) -> str:
    base = (facts.get("base") or facts.get("xsd_type") or "").lower()
    if facts.get("enums"):
        return "Code"
    if "datetime" in base or "date" in base:
        return "ISODateTime"
    if base in ("decimal", "integer", "int", "long", "short", "positiveinteger", "nonnegativeinteger"):
        return "Numeric"
    if base == "boolean":
        return "Boolean"
    if base:
        return "Alphanumeric"
    return "Alphanumeric"


def _length_label(facts: dict) -> str | None:
    if "length" in facts:
        return f"Length : {facts['length']}"
    lo, hi = facts.get("min_length"), facts.get("max_length")
    if lo is not None or hi is not None:
        parts = []
        if lo is not None:
            parts.append(f"Min Length : {lo}")
        if hi is not None:
            parts.append(f"Max Length : {hi}")
        return " ".join(parts)
    if "min_inclusive" in facts or "max_inclusive" in facts:
        parts = []
        if "min_inclusive" in facts:
            parts.append(f"minInclusive : {facts['min_inclusive']}")
        if "max_inclusive" in facts:
            parts.append(f"maxInclusive : {facts['max_inclusive']}")
        return " ".join(parts)
    if facts.get("enums"):
        return "Fixed value"
    return None


def _occurrence(node: ET.Element) -> tuple[str, str]:
    lo = node.get("minOccurs", "1")
    hi = node.get("maxOccurs", "1")
    hi_disp = "n" if hi == "unbounded" else hi
    return f"{lo}..{hi_disp}", ("Y" if lo not in ("0",) else "N")


# ---------------------------------------------------------------------------
# Pass 2 — element expansion
# ---------------------------------------------------------------------------

def _leaf_from_facts(spec: FieldSpec, facts: dict) -> None:
    spec.facts.update(facts)
    spec.datatype = _datatype_label(facts)
    spec.length_rule = _length_label(facts)
    if facts.get("enums"):
        spec.enum_values = facts["enums"]


def _expand_attribute(node: ET.Element, reg: TypeRegistry) -> FieldSpec:
    # An XSD attribute's cardinality follows its `use`: required → exactly one
    # (1..1), optional/absent → at most one (0..1), prohibited → none (0..0).
    # Occurrence was previously hardcoded 1..1 for EVERY attribute, so the
    # certification occurrence-assertion (cert_assertions.assert_occurrence,
    # which reads `occurrence`, not `mandatory`) demanded that an OPTIONAL
    # attribute be present — failing any conformant message that legitimately
    # omitted it. That is the exact failure mode an additive optional
    # attribute (e.g. NLLN CR-1's Book/@language) must not trigger. `mandatory`
    # already tracked `use` correctly; only `occurrence` was wrong.
    use = node.get("use") or "optional"
    occurrence = {"required": "1..1", "prohibited": "0..0"}.get(use, "0..1")
    spec = FieldSpec(
        xml_tag=node.get("name") or _local(node.get("ref")) or "attr",
        is_attribute=True,
        occurrence=occurrence,
        mandatory="Y" if use == "required" else "N",
    )
    type_ref = _local(node.get("type"))
    inline = node.find(f"{XS}simpleType")
    if inline is not None:
        _leaf_from_facts(spec, _restriction_facts(inline))
    elif type_ref in reg.simple:
        _leaf_from_facts(spec, dict(reg.simple[type_ref]))
    elif type_ref:
        _leaf_from_facts(spec, {"xsd_type": type_ref})
    spec.facts.setdefault("xsd_type", type_ref or "string")
    spec.facts["use"] = node.get("use") or "optional"
    return spec


def _expand_complex(ct: ET.Element, reg: TypeRegistry, depth: int, seen: frozenset[str]) -> tuple[list[FieldSpec], list[FieldSpec]]:
    """Return (child_elements, attributes) of a complexType node."""
    elements: list[FieldSpec] = []
    attributes: list[FieldSpec] = []

    def walk(container: ET.Element):
        for child in container:
            if child.tag in (f"{XS}sequence", f"{XS}choice", f"{XS}all"):
                walk(child)
            elif child.tag == f"{XS}element":
                elements.append(_expand_element(child, reg, depth + 1, seen))
            elif child.tag == f"{XS}attribute":
                attributes.append(_expand_attribute(child, reg))
            elif child.tag in (f"{XS}simpleContent", f"{XS}complexContent"):
                ext = child.find(f"{XS}extension") or child.find(f"{XS}restriction")
                if ext is not None:
                    base = _local(ext.get("base"))
                    if base in reg.complex and base not in seen:
                        b_els, b_attrs = _expand_complex(reg.complex[base], reg, depth, seen | {base})
                        elements.extend(b_els)
                        attributes.extend(b_attrs)
                    walk(ext)

    walk(ct)
    return elements, attributes


def _expand_element(node: ET.Element, reg: TypeRegistry, depth: int = 0,
                    seen: frozenset[str] = frozenset()) -> FieldSpec:
    name = node.get("name") or _local(node.get("ref")) or "Element"
    occ, mand = _occurrence(node)
    spec = FieldSpec(xml_tag=name, occurrence=occ, mandatory=mand)
    if depth > _MAX_DEPTH:
        return spec

    type_ref = _local(node.get("type"))
    inline_ct = node.find(f"{XS}complexType")
    inline_st = node.find(f"{XS}simpleType")

    ct = None
    if inline_ct is not None:
        ct = inline_ct
    elif type_ref in reg.complex and type_ref not in seen:
        ct = reg.complex[type_ref]
        seen = seen | {type_ref}

    if ct is not None:
        elements, attributes = _expand_complex(ct, reg, depth, seen)
        # NPCI convention: container tag rows read Datatype=Alphabetic, Length=Fixed value.
        spec.datatype = "Alphabetic"
        spec.length_rule = "Fixed value"
        spec.children = attributes + elements
        spec.facts["xsd_type"] = type_ref or "(inline)"
    elif inline_st is not None:
        _leaf_from_facts(spec, _restriction_facts(inline_st))
    elif type_ref in reg.simple:
        _leaf_from_facts(spec, dict(reg.simple[type_ref]))
    else:
        _leaf_from_facts(spec, {"xsd_type": type_ref or "string"})
    return spec


def parse_xsd_dir(xsd_dir: Path) -> list[MessageSpec]:
    paths = sorted(p for p in Path(xsd_dir).glob("*.xsd") if p.is_file())
    reg = TypeRegistry.build(paths)
    messages: list[MessageSpec] = []
    for name, node, file, tns, ns_prefix in reg.top_elements:
        root = _expand_element(node, reg)
        _assign_tag_nums(root)
        messages.append(MessageSpec(api_name=name, namespace=tns, schema_file=file, root=root,
                                    ns_prefix=ns_prefix))
    return messages


# ---------------------------------------------------------------------------
# NPCI-style hierarchical tag numbering (deterministic)
# ---------------------------------------------------------------------------

def _assign_tag_nums(root: FieldSpec) -> None:
    root.tag_num = "1.1"
    # Synthetic xmlns row mirrors the real TSD tables ("1.1.1 API Schema namespace").
    xmlns = FieldSpec(xml_tag="xmlns", is_attribute=True, occurrence="1..1", mandatory="Y",
                      datatype="Alphanumeric",
                      length_rule="Min Length : 1 Max Length : 255",
                      facts={"synthetic": "namespace declaration"})
    xmlns.tag_num = "1.1.1"
    root.children = [xmlns] + [c for c in root.children if not (c.is_attribute and c.xml_tag == "xmlns")]

    block = 1
    for child in root.children:
        if child.is_attribute:
            if not child.tag_num:
                block_attrs = [c for c in root.children if c.is_attribute and c.tag_num]
                child.tag_num = f"1.1.{len(block_attrs) + 1}"
            continue
        block += 1
        counter = {"n": 0}

        def number_element(el: FieldSpec):
            counter["n"] += 1
            el.tag_num = f"{block}.{counter['n']}"
            attr_i = 0
            for c in el.children:
                if c.is_attribute:
                    attr_i += 1
                    c.tag_num = f"{el.tag_num}.{attr_i}"
            for c in el.children:
                if not c.is_attribute:
                    number_element(c)

        number_element(child)


# ---------------------------------------------------------------------------
# Deterministic sample XML (enums inline, NPCI style)
# ---------------------------------------------------------------------------

def _attr_sample(spec: FieldSpec) -> str:
    if spec.enum_values:
        joined = "|".join(spec.enum_values)
        if len(joined) > 80:
            joined = "|".join(spec.enum_values[:5]) + "|.."
        return f'{spec.xml_tag}="{joined}"'
    return f'{spec.xml_tag}=""'


def _element_sample(spec: FieldSpec, indent: int, ns_prefix: str = "") -> list[str]:
    pad = "    " * indent
    attrs = [c for c in spec.children if c.is_attribute and c.xml_tag != "xmlns"]
    els = [c for c in spec.children if not c.is_attribute]
    tag = f"{ns_prefix}{spec.xml_tag}"
    attr_str = (" " + " ".join(_attr_sample(a) for a in attrs)) if attrs else ""
    if not els:
        return [f"{pad}<{tag}{attr_str}/>"]
    lines = [f"{pad}<{tag}{attr_str}>"]
    for el in els:
        lines.extend(_element_sample(el, indent + 1))
    lines.append(f"{pad}</{tag}>")
    return lines


def build_sample_xml(msg: MessageSpec) -> str:
    """Sample wire XML for a message, echoing the SOURCE schema's own namespace binding.

    Prefix and URI both come from the ingested `.xsd` (see `_ns_prefix_for`). A schema
    that binds its target as the default namespace — as NLLN's does with
    `xmlns="http://nlln.in/schema/v1"` — yields an unprefixed sample; one that binds a
    prefix yields that prefix. Neither is written down here.

    This is rendered VERBATIM into the TSD by `render_registry_sections`, so a wrong
    prefix ships to the reader as fact. It previously hardcoded `upi:` alongside a
    dynamic URI, which produced `<upi:ReqMemberVerify xmlns:upi="http://nlln.in/schema/v1">`
    in a library-domain document. There is likewise no default URI: a schema with no
    targetNamespace gets no `xmlns` rather than another ecosystem's.
    """
    root = msg.root
    attrs = [c for c in root.children if c.is_attribute and c.xml_tag != "xmlns"]
    els = [c for c in root.children if not c.is_attribute]
    p = f"{msg.ns_prefix}:" if msg.ns_prefix else ""
    if msg.namespace:
        xmlns = f' xmlns:{msg.ns_prefix}="{msg.namespace}"' if msg.ns_prefix else f' xmlns="{msg.namespace}"'
    else:
        xmlns = ""
    open_tag = f"<{p}{root.xml_tag}{xmlns}"
    if attrs:
        open_tag += " " + " ".join(_attr_sample(a) for a in attrs)
    lines = [open_tag + ">"]
    for el in els:
        lines.extend(_element_sample(el, 1))
    lines.append(f"</{p}{root.xml_tag}>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Source-dir discovery
# ---------------------------------------------------------------------------

def discover_default_xsd_dirs(baselines: list[dict] | None = None) -> list[Path]:
    """Best-effort default: KB existing_xsds, else schema dirs from workspace clones.

    KB keeps priority (it's the curated platform schema set). The clone fallback
    resolves via ``select_source_clones`` — best clone per production-baseline repo,
    rooted at ``agentic_workspace_root`` — and returns every ``src/main/resources``
    dir beneath the chosen clones that directly holds ``*.xsd`` files
    (``parse_xsd_dir`` reads a dir non-recursively).
    """
    from app.core.config import settings
    from app.services.api_registry_code_harvest import select_source_clones

    kb = Path(settings.knowledge_base_dir) / "existing_xsds"
    if kb.is_dir() and any(kb.glob("*.xsd")):
        return [kb]

    def xsd_count(clone: Path) -> int:
        return sum(len(list(d.glob("*.xsd"))) for d in clone.rglob("src/main/resources"))

    dirs: list[Path] = []
    for clone in select_source_clones(baselines, xsd_count):
        dirs.extend(d for d in sorted(clone.rglob("src/main/resources"))
                    if any(d.glob("*.xsd")))
    return dirs


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def _flatten(root: FieldSpec) -> list[tuple[FieldSpec, str, int, str | None]]:
    """Depth-first (spec, xpath, depth, parent_xpath) — attributes before child elements."""
    out: list[tuple[FieldSpec, str, int, str | None]] = []

    def walk(spec: FieldSpec, path: str, depth: int, parent: str | None):
        out.append((spec, path, depth, parent))
        for c in spec.children:
            seg = f"@{c.xml_tag}" if c.is_attribute else c.xml_tag
            walk(c, f"{path}/{seg}", depth + 1, path)

    walk(root, root.xml_tag, 0, None)
    return out


def ingest_from_dir(db, xsd_dir: Path, source: str = "xsd_parse",
                    introduced_by_change_id: str | None = None) -> dict:
    from app.models.api_registry import ApiMessage, ApiField

    messages = parse_xsd_dir(xsd_dir)
    counts = {"messages_created": 0, "messages_updated": 0,
              "fields_created": 0, "fields_updated": 0, "fields_removed": 0,
              "human_locked_skipped": 0}

    # Two schema files can declare the same root element (e.g. ReqChkTxn.xsd and
    # UdirReqChkTxn.xsd) — first wins within a run (sorted file order, so the
    # canonically-named file beats the variant), rather than last-writer-wins.
    seen_names: set[str] = set()
    for msg in messages:
        if msg.api_name in seen_names:
            logger.info("api-registry ingest: duplicate root element %s in %s — skipped",
                        msg.api_name, Path(msg.schema_file).name)
            continue
        seen_names.add(msg.api_name)
        row = db.query(ApiMessage).filter(ApiMessage.api_name == msg.api_name).first()
        direction = ("request" if msg.api_name.lower().startswith("req")
                     else "response" if msg.api_name.lower().startswith("resp") else "other")
        sample = build_sample_xml(msg)
        rel_path = str(msg.schema_file)
        if row is None:
            row = ApiMessage(api_name=msg.api_name, direction=direction, namespace=msg.namespace,
                             sample_xml=sample, source=source, source_schema_path=rel_path,
                             introduced_by_change_id=introduced_by_change_id)
            db.add(row)
            db.flush()
            counts["messages_created"] += 1
        else:
            row.namespace = msg.namespace
            row.sample_xml = sample
            row.source_schema_path = rel_path
            counts["messages_updated"] += 1

        existing = {f.xpath: f for f in db.query(ApiField).filter(ApiField.message_id == row.id).all()}
        seen_xpaths: set[str] = set()
        id_by_xpath: dict[str, str] = {}

        for pos, (spec, xpath, depth, parent_xpath) in enumerate(_flatten(msg.root)):
            seen_xpaths.add(xpath)
            xsd_facts = dict(spec.facts)
            xsd_facts["schema_file"] = Path(msg.schema_file).name
            xsd_facts["occurrence"] = spec.occurrence
            xsd_facts["mandatory"] = spec.mandatory
            if spec.length_rule:
                xsd_facts["length_rule"] = spec.length_rule
            f = existing.get(xpath)
            if f is None:
                f = ApiField(
                    message_id=row.id,
                    parent_field_id=id_by_xpath.get(parent_xpath),
                    position=pos, depth=depth, tag_num=spec.tag_num,
                    xml_tag=spec.xml_tag, is_attribute=spec.is_attribute, xpath=xpath,
                    occurrence=spec.occurrence, datatype=spec.datatype,
                    length_rule=spec.length_rule, mandatory=spec.mandatory,
                    enum_values=spec.enum_values,
                    constraint_sources={"xsd": xsd_facts},
                    source=source, introduced_by_change_id=introduced_by_change_id,
                )
                db.add(f)
                db.flush()
                counts["fields_created"] += 1
            else:
                cs = dict(f.constraint_sources or {})
                cs["xsd"] = xsd_facts
                f.constraint_sources = cs
                f.position, f.depth = pos, depth
                f.parent_field_id = id_by_xpath.get(parent_xpath)
                f.status = "active"
                if f.updated_by is None:
                    f.tag_num = spec.tag_num
                    f.occurrence = spec.occurrence
                    f.datatype = spec.datatype
                    f.length_rule = spec.length_rule
                    f.mandatory = spec.mandatory if f.mandatory != "C" else f.mandatory
                    f.enum_values = spec.enum_values
                    counts["fields_updated"] += 1
                else:
                    counts["human_locked_skipped"] += 1
            id_by_xpath[xpath] = f.id

        for xpath, f in existing.items():
            if xpath in seen_xpaths:
                continue
            if f.source == "xsd_parse" and f.updated_by is None:
                db.delete(f)
                counts["fields_removed"] += 1
            else:
                f.status = "stale"

    db.commit()
    counts["messages_total"] = len(messages)
    return counts


# ---------------------------------------------------------------------------
# TSD rendering support
# ---------------------------------------------------------------------------

TSD_TABLE_HEADERS = ["Tag Num", "Message Item", "<XMLTag>", "Occurrence",
                     "Datatype", "Length", "Mandatory", "Rules"]


def derive_involved_api_names(*texts: str) -> list[str]:
    """Wire-message names found in ``texts``, in first-seen order.

    The name shape comes from the active pack's ``message_name_pattern``
    (UPI: Req*/Resp*/Ack). A pack that declares none yields [] — "no messages
    found" — rather than borrowing another domain's naming convention.
    """
    from app.core.domain.contract import message_name_pattern_of
    from app.core.domain.registry import get_active_pack

    pattern = message_name_pattern_of(get_active_pack())
    if pattern is None:
        return []
    names: list[str] = []
    for t in texts:
        if not t:
            continue
        for m in pattern.finditer(t):
            if m.group(0) not in names:
                names.append(m.group(0))
    return names


def _rules_cell(f) -> str:
    parts = []
    if f.rules_ref:
        parts.append(f.rules_ref)
    if f.condition_text:
        parts.append(f.condition_text)
    if f.enum_values and not f.rules_ref:
        joined = "|".join(str(v) for v in f.enum_values)
        if len(joined) > 90:
            joined = "|".join(str(v) for v in f.enum_values[:6]) + "|.."
        parts.append(joined)
    return " — ".join(parts)


def registry_specs_all(db) -> dict[str, dict]:
    """Spec blocks for every active registry message, keyed by api_name.

    Loaded once per TSD run so the docgen pipeline can append a section for ANY
    registry API the document turns out to mention (post-write sweep) without
    needing DB access mid-pipeline.
    """
    from app.models.api_registry import ApiMessage

    names = sorted(n for (n,) in
                   db.query(ApiMessage.api_name).filter(ApiMessage.status == "active").all())
    return {s["api_name"]: s for s in registry_specs_for_context(db, names)}


def sections_scan_text(sections: list[dict]) -> str:
    """Flatten generated sections (prose, headings, tables, code blocks) into one
    scannable text — the same surface the deterministic eval check reads, so the
    sweep and the check agree on which APIs the document mentions."""
    parts: list[str] = []
    for s in sections or []:
        parts.append(str(s.get("section_heading") or ""))
        for key in ("paragraphs", "bullet_points", "numbered_items", "code_blocks"):
            parts.extend(str(x) for x in (s.get(key) or []))
        td = s.get("table_data")
        if isinstance(td, dict):
            parts.extend(str(h) for h in (td.get("headers") or []))
            for row in (td.get("rows") or []):
                cells = row.values() if isinstance(row, dict) else (row if isinstance(row, list) else [])
                parts.extend(str(c) for c in cells)
    return "\n".join(parts)


def registry_specs_for_context(db, api_names: list[str]) -> list[dict]:
    """Registry-backed spec blocks for the given APIs (order preserved, misses skipped)."""
    from app.models.api_registry import ApiMessage, ApiField

    specs: list[dict] = []
    for name in api_names:
        msg = (db.query(ApiMessage)
               .filter(ApiMessage.api_name == name, ApiMessage.status == "active").first())
        if msg is None:
            continue
        fields = (db.query(ApiField)
                  .filter(ApiField.message_id == msg.id, ApiField.status == "active")
                  .order_by(ApiField.position).all())
        rows = []
        for f in fields:
            display_tag = f.xml_tag if f.is_attribute else f"<{f.xml_tag}>"
            rows.append([
                f.tag_num or "", f.message_item or "", display_tag,
                f.occurrence or "", f.datatype or "", f.length_rule or "",
                f.mandatory or "", _rules_cell(f),
            ])
        specs.append({
            "api_name": msg.api_name,
            "description": msg.description or "",
            "sample_xml": msg.sample_xml or "",
            "headers": list(TSD_TABLE_HEADERS),
            "rows": rows,
        })
    return specs


def render_registry_sections(specs: list[dict]) -> list[dict]:
    """GeneratedContent-shaped sections rendered purely from registry rows (no LLM)."""
    sections = []
    for spec in specs:
        paragraphs = []
        if spec.get("description"):
            paragraphs.append(spec["description"])
        paragraphs.append(
            "The message structure and field dictionary below are rendered verbatim "
            "from the platform API Registry (deterministic — not model-generated)."
        )
        sections.append({
            "section_key": f"api_registry_{spec['api_name']}",
            "section_heading": f"API Specification — {spec['api_name']}",
            "render_style": "body",
            "level": 2,
            "paragraphs": paragraphs,
            "bullet_points": [],
            "numbered_items": [],
            "code_blocks": [spec["sample_xml"]] if spec.get("sample_xml") else [],
            "table_data": {"headers": spec["headers"], "rows": spec["rows"]},
        })
    return sections


def content_hash(specs: list[dict]) -> str:
    import json
    return hashlib.sha256(json.dumps(specs, sort_keys=True).encode()).hexdigest()[:16]
