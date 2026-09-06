# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""XSD schema graph + deterministic element-index diff (THE BOOK §7.1/§7.4).

Replaces the weak ``parent_xsd_id`` lineage with a real **schema graph**: each
XSD file is a node, each ``<xs:include>``/``<xs:import>`` an edge. A change to one
schema is traced through the (reverse) edges to find dependent schemas — that is
what makes "which XSDs are in scope" correct.

The XSD-Discovery subagent (S9) *decides* reuse/extend/new agentically; this
module produces the **deterministic record**: the element-index diff that yields
the stable ``[NEW]/[MODIFIED]/[DEPRECATED]`` set (§7.4 — judgement is agentic,
fact is deterministic). Parsing is lxml-only (no new dependency).
"""
from __future__ import annotations

import hashlib
import logging
import posixpath
from dataclasses import dataclass, field

from lxml import etree

logger = logging.getLogger("app.agentic")

_XS = "http://www.w3.org/2001/XMLSchema"
_NS = {"xs": _XS}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _root(content: str, path: str = "<xsd>"):
    # Empty/blank content (e.g. the "before" version of a newly-CREATED file, which
    # diffs against base="") is "no document" — return None so callers treat it as
    # zero elements. lxml's fromstring(b"") raises "Document is empty" even with
    # recover=True, which would otherwise crash the whole run.
    if not content or not content.strip():
        return None
    # recover=True tolerates the odd malformed schema rather than aborting a whole
    # index run; resolve_entities off — we never fetch external entities. Recovery
    # is logged (not silent) so a spurious NEW/DEPRECATED from a partial parse is
    # traceable.
    parser = etree.XMLParser(resolve_entities=False, recover=True)
    root = etree.fromstring(content.encode("utf-8"), parser=parser)
    if parser.error_log:
        logger.warning("XSD parse recovered from %d error(s) in %s: %s",
                       len(parser.error_log), path, str(parser.error_log[0]))
    return root


# ── Parsed schema ─────────────────────────────────────────────────────────────

@dataclass
class ParsedSchema:
    target_namespace: str | None
    includes: list[str] = field(default_factory=list)            # schemaLocation
    imports: list[tuple[str | None, str | None]] = field(default_factory=list)  # (namespace, schemaLocation)


def parse_schema(content: str) -> ParsedSchema:
    root = _root(content)
    if root is None:
        return ParsedSchema(None)
    tns = root.get("targetNamespace")
    includes = [e.get("schemaLocation") for e in root.findall("xs:include", _NS) if e.get("schemaLocation")]
    imports = [(e.get("namespace"), e.get("schemaLocation")) for e in root.findall("xs:import", _NS)]
    return ParsedSchema(target_namespace=tns, includes=includes, imports=imports)


# ── Element index + deterministic diff (§7.4) ─────────────────────────────────────

# Restriction facets that form part of a type's WIRE CONTRACT. `enumeration` is the
# load-bearing one (a purpose/status code list), but a widened pattern or length bound
# is just as much a contract change, so they all belong in the signature.
_FACET_KINDS = ("enumeration", "pattern", "minLength", "maxLength",
                "minInclusive", "maxInclusive", "minExclusive", "maxExclusive",
                "totalDigits", "fractionDigits", "length", "whiteSpace")


def _facet_signature(node) -> list[str]:
    """Sorted ``facet=value`` descriptors for every restriction facet under ``node``
    (including nested/inline simpleTypes). Sorted, so formatting and declaration order
    never affect the signature — only the SET of constraints does."""
    out: list[str] = []
    for kind in _FACET_KINDS:
        for f in node.findall(f".//xs:{kind}", _NS):
            v = f.get("value")
            if v is not None:
                out.append(f"{kind}={v}")
    return sorted(out)


def element_index(content: str) -> dict[str, str]:
    """Map every top-level ``xs:element``/``xs:complexType``/``xs:simpleType`` (by name)
    to a stable signature of its descendant element (name,type) pairs AND its restriction
    facets. A signature change => MODIFIED; appearance/disappearance => NEW/DEPRECATED.
    Deterministic: both parts are sorted, so attribute order / formatting never affects it.

    ``simpleType`` and the facet set are indexed because they carry the WIRE CONTRACT that
    the element/complexType structure alone does not. Adding ``<xs:enumeration value="BG"/>``
    to a purpose-code simpleType changes no element (name,type) pair, so a structure-only
    index reported it as NO CHANGE AT ALL — the schema diff shown to the reviewer and to the
    human at the approval gate was empty for exactly the class of edit most likely to collide
    with an existing wire value. Facets close that blind spot.
    """
    root = _root(content)
    index: dict[str, str] = {}
    if root is None:
        return index
    for kind in ("element", "complexType", "simpleType"):
        for node in root.findall(f"xs:{kind}", _NS):
            name = node.get("name")
            if not name:
                continue
            children = sorted(
                f"{c.get('name')}:{c.get('type') or ''}"
                for c in node.findall(".//xs:element", _NS)
                if c.get("name")
            )
            facets = _facet_signature(node)
            index[f"{kind}:{name}"] = _sha256(
                node.get("type", "") + "|" + ";".join(children) + "|" + ";".join(facets))
    return index


def enumeration_index(content: str) -> dict[str, list[str]]:
    """Map every NAMED top-level type to the sorted set of ``xs:enumeration`` values
    declared anywhere inside it. Used to extract the literals a schema edit ADDED, which
    are then occupancy-checked against the real code before the schema is frozen.

    Keyed the same way as :func:`element_index` (``simpleType:txnPurpose``) so the two
    can be read side by side. Values are de-duplicated and sorted for determinism.
    """
    root = _root(content)
    out: dict[str, list[str]] = {}
    if root is None:
        return out
    for kind in ("element", "complexType", "simpleType"):
        for node in root.findall(f"xs:{kind}", _NS):
            name = node.get("name")
            if not name:
                continue
            vals = {f.get("value") for f in node.findall(".//xs:enumeration", _NS)
                    if f.get("value") is not None}
            if vals:
                out[f"{kind}:{name}"] = sorted(vals)
    return out


def added_enum_values(old_content: str, new_content: str) -> dict[str, list[str]]:
    """``{type_key: [values present in new but not in old]}`` — the literals this edit
    INTRODUCES to the wire contract. A type that gained no value is omitted entirely, so
    an empty result means "this edit added no new enum literal" and the caller can skip
    the (repo-wide, git-grep-backed) occupancy sweep."""
    old, new = enumeration_index(old_content), enumeration_index(new_content)
    out: dict[str, list[str]] = {}
    for key, vals in new.items():
        added = sorted(set(vals) - set(old.get(key, [])))
        if added:
            out[key] = added
    return out


@dataclass
class XsdDiff:
    new: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deprecated: list[str] = field(default_factory=list)

    def as_records(self) -> list[dict]:
        return (
            [{"key": k, "status": "NEW"} for k in self.new]
            + [{"key": k, "status": "MODIFIED"} for k in self.modified]
            + [{"key": k, "status": "DEPRECATED"} for k in self.deprecated]
        )


def diff_schema(old_content: str, new_content: str) -> XsdDiff:
    """Deterministic ``[NEW]/[MODIFIED]/[DEPRECATED]`` between two schema versions.

    Scope: the per-schema signature reflects each element/type's INLINE
    structure. A change to a *referenced* type (``type="Money"``) surfaces as a
    MODIFIED on ``Money`` itself; consumers that reference it are found via the
    schema graph's reverse edges (:func:`dependents_of`), not this single-file
    diff. Use the diff and the graph together for full impact.
    """
    old, new = element_index(old_content), element_index(new_content)
    diff = XsdDiff()
    for k, sig in new.items():
        if k not in old:
            diff.new.append(k)
        elif old[k] != sig:
            diff.modified.append(k)
    for k in old:
        if k not in new:
            diff.deprecated.append(k)
    for lst in (diff.new, diff.modified, diff.deprecated):
        lst.sort()
    return diff


# ── Schema graph (nodes + edges) ──────────────────────────────────────────────

@dataclass
class GraphNode:
    path: str
    target_namespace: str | None
    content_hash: str


@dataclass
class GraphEdge:
    from_path: str
    to_path: str | None          # resolved sibling path, or None when external/unresolved
    edge_type: str               # "include" | "import"
    schema_location: str | None
    namespace: str | None


def build_graph(files: list[dict]) -> tuple[list[GraphNode], list[GraphEdge]]:
    """Build nodes + include/import edges from ``[{path, content}, ...]``.

    Edge targets resolve by normalising the include/import ``schemaLocation``
    relative to the including file's directory; an unresolved location (external
    catalog, missing sibling) keeps the edge with ``to_path=None`` so the link is
    still recorded, never silently dropped.
    """
    by_path = {f["path"]: f["content"] for f in files}
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    for path, content in by_path.items():
        ps = parse_schema(content)
        nodes.append(GraphNode(path, ps.target_namespace, _sha256(content)))
        base = posixpath.dirname(path)
        for loc in ps.includes:
            edges.append(GraphEdge(path, _resolve(base, loc, by_path), "include", loc, None))
        for ns, loc in ps.imports:
            edges.append(GraphEdge(path, _resolve(base, loc, by_path), "import", loc, ns))
    return nodes, edges


def _resolve(base_dir: str, location: str | None, by_path: dict) -> str | None:
    if not location:
        return None
    cand = posixpath.normpath(posixpath.join(base_dir, location)) if base_dir else posixpath.normpath(location)
    if cand in by_path:
        return cand
    # also try the bare location (some schemas reference by path-from-root)
    bare = posixpath.normpath(location)
    return bare if bare in by_path else None


def dependents_of(path: str, edges: list[GraphEdge]) -> set[str]:
    """Schemas that include/import ``path`` (reverse edges) — the impact set of a
    change to ``path`` (§7.1). Transitive: a dependent's dependents are included."""
    out: set[str] = set()
    frontier = [path]
    while frontier:
        cur = frontier.pop()
        for e in edges:
            if e.to_path == cur and e.from_path not in out:
                out.add(e.from_path)
                frontier.append(e.from_path)
    return out


# ── Persist (idempotent rebuild per repo) ─────────────────────────────────────

def persist_graph(db, repo_id: str, files: list[dict], base_commit_sha: str | None = None) -> tuple[int, int]:
    """Replace the repo's XSD graph with one rebuilt from ``files``. Returns
    ``(node_count, edge_count)``. Idempotent: re-running with the same files
    yields the same graph."""
    from app.models.xsd_graph import XsdSchemaNode, XsdSchemaEdge

    # Drop the old graph for this repo (edges first — FK to nodes).
    old_node_ids = [n.id for n in db.query(XsdSchemaNode).filter(XsdSchemaNode.repo_id == repo_id).all()]
    if old_node_ids:
        db.query(XsdSchemaEdge).filter(XsdSchemaEdge.from_node_id.in_(old_node_ids)).delete(synchronize_session=False)
        db.query(XsdSchemaNode).filter(XsdSchemaNode.repo_id == repo_id).delete(synchronize_session=False)

    nodes, edges = build_graph(files)
    id_by_path: dict[str, str] = {}
    for n in nodes:
        row = XsdSchemaNode(repo_id=repo_id, path=n.path, target_namespace=n.target_namespace,
                            content_hash=n.content_hash, base_commit_sha=base_commit_sha)
        db.add(row)
        db.flush()
        id_by_path[n.path] = row.id
    for e in edges:
        db.add(XsdSchemaEdge(
            from_node_id=id_by_path[e.from_path],
            to_node_id=id_by_path.get(e.to_path) if e.to_path else None,
            edge_type=e.edge_type, schema_location=e.schema_location, namespace=e.namespace,
        ))
    db.flush()
    return len(nodes), len(edges)
