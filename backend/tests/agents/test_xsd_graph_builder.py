# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""XSD schema graph + deterministic diff (§7.1/§7.4). Pure lxml."""
from app.agents.xsd_graph_builder import (
    parse_schema, element_index, diff_schema, build_graph, dependents_of,
)

COMMON = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:common">
  <xs:complexType name="Money">
    <xs:sequence><xs:element name="amount" type="xs:decimal"/></xs:sequence>
  </xs:complexType>
</xs:schema>"""

REFUND = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:refund">
  <xs:import namespace="urn:common" schemaLocation="common.xsd"/>
  <xs:element name="Refund">
    <xs:complexType><xs:sequence>
      <xs:element name="amount" type="xs:decimal"/>
    </xs:sequence></xs:complexType>
  </xs:element>
</xs:schema>"""


def test_parse_schema_extracts_namespace_and_imports():
    ps = parse_schema(REFUND)
    assert ps.target_namespace == "urn:refund"
    assert ps.imports == [("urn:common", "common.xsd")]
    assert parse_schema(COMMON).imports == []


def test_element_index_and_diff_new_modified_deprecated():
    base = element_index(REFUND)
    assert "element:Refund" in base
    # MODIFIED — add a child element to Refund
    modified = REFUND.replace(
        '<xs:element name="amount" type="xs:decimal"/>',
        '<xs:element name="amount" type="xs:decimal"/><xs:element name="status" type="xs:string"/>')
    # NEW + DEPRECATED — add a top-level element, drop the import-only file has none
    added = modified.replace("</xs:schema>", '<xs:element name="Receipt"/></xs:schema>')
    d = diff_schema(REFUND, added)
    assert d.modified == ["element:Refund"]
    assert d.new == ["element:Receipt"]
    # dropping Refund in a 3rd version → DEPRECATED
    dropped = REFUND.replace('<xs:element name="Refund">', '<xs:element name="Other">')
    d2 = diff_schema(REFUND, dropped)
    assert "element:Refund" in d2.deprecated and "element:Other" in d2.new


def test_diff_against_empty_base_reports_all_new_without_crashing():
    # A newly-CREATED schema diffs against an empty base ("") — lxml can't parse an
    # empty document, so this must be handled (every element NEW), not raise.
    d = diff_schema("", REFUND)
    assert "element:Refund" in d.new
    assert d.modified == [] and d.deprecated == []
    assert element_index("") == {}          # blank/whitespace parses to no elements
    assert element_index("   \n  ") == {}


def test_build_graph_nodes_edges_and_resolution():
    nodes, edges = build_graph([
        {"path": "xsd/common.xsd", "content": COMMON},
        {"path": "xsd/refund.xsd", "content": REFUND},
    ])
    assert {n.path for n in nodes} == {"xsd/common.xsd", "xsd/refund.xsd"}
    assert len(edges) == 1
    e = edges[0]
    assert e.edge_type == "import" and e.from_path == "xsd/refund.xsd"
    assert e.to_path == "xsd/common.xsd"          # resolved relative to refund's dir
    assert e.namespace == "urn:common"


def test_unresolved_edge_kept_with_none_target():
    nodes, edges = build_graph([
        {"path": "xsd/refund.xsd", "content": REFUND},  # common.xsd absent from the set
    ])
    assert len(edges) == 1 and edges[0].to_path is None and edges[0].schema_location == "common.xsd"


def test_dependents_of_is_transitive():
    a = '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"><xs:include schemaLocation="b.xsd"/></xs:schema>'
    b = '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"><xs:include schemaLocation="c.xsd"/></xs:schema>'
    c = '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"/>'
    _, edges = build_graph([{"path": "a.xsd", "content": a},
                            {"path": "b.xsd", "content": b},
                            {"path": "c.xsd", "content": c}])
    # a→b→c ; a change to c impacts both b and a
    assert dependents_of("c.xsd", edges) == {"a.xsd", "b.xsd"}
