# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Deterministic XML-block validation for generated docs (accuracy S6).

Covers the two-way split the module exists to get right: SCHEMA blocks must REPRODUCE
an approved schema (whitespace-normalised substring), INSTANCE blocks must VALIDATE
against the approved set. Both paths fail-open.
"""
import pytest
from app.services import xsd_validation as V


def test_extract_xml_blocks_case_insensitive_and_drops_empty():
    md = "intro\n```xml\n<a/>\n```\nmid\n```XML\n<b/>\n```\n```xml\n\n```\n"
    assert V.extract_xml_blocks(md) == ["<a/>", "<b/>"]
    assert V.extract_xml_blocks("") == []
    assert V.extract_xml_blocks("no fenced blocks here") == []


def test_is_schema_block():
    assert V._is_schema_block('<xs:schema xmlns:xs="...">')
    assert V._is_schema_block('<xsd:schema>')
    assert V._is_schema_block('<schema xmlns="http://www.w3.org/2001/XMLSchema">')
    assert not V._is_schema_block('<ReqTransfer><Txn/></ReqTransfer>')


def test_no_approved_schemas_yields_no_findings():
    md = "```xml\n<xs:schema><xs:element name='X'/></xs:schema>\n```"
    assert V.validate_xml_blocks(md, []) == []                # empty approved set → early return
    assert V.validate_xml_blocks("", ["<xs:schema/>"]) == []  # no blocks → nothing to check


def test_schema_block_matching_approved_is_clean():
    # collapsed-whitespace match: the approved schema has double spaces, the block single —
    # both normalise to the same string, so the block is recognised as a reproduction.
    approved = '<xs:schema  xmlns:xs="x"><xs:element  name="Refund"/></xs:schema>'
    md = '```xml\n<xs:schema xmlns:xs="x"><xs:element name="Refund"/></xs:schema>\n```'
    assert V.validate_xml_blocks(md, [approved]) == []


def test_schema_block_not_in_approved_is_flagged():
    approved = '<xs:schema xmlns:xs="x"><xs:element name="Refund"/></xs:schema>'
    md = '```xml\n<xs:schema xmlns:xs="x"><xs:element name="Invented"/></xs:schema>\n```'
    findings = V.validate_xml_blocks(md, [approved])
    assert len(findings) == 1 and "SCHEMA that does not match" in findings[0]


def test_instance_block_validates_against_schema():
    pytest.importorskip("xmlschema")
    schema = ('<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
              '<xs:element name="Root"><xs:complexType><xs:sequence>'
              '<xs:element name="Child" type="xs:string"/>'
              '</xs:sequence></xs:complexType></xs:element></xs:schema>')
    assert V.validate_xml_blocks("```xml\n<Root><Child>ok</Child></Root>\n```", [schema]) == []
    findings = V.validate_xml_blocks("```xml\n<Root><Nope/></Root>\n```", [schema])
    assert len(findings) == 1 and "does not validate" in findings[0]


def test_malformed_instance_is_not_double_reported():
    pytest.importorskip("xmlschema")
    schema = ('<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
              '<xs:element name="Root"/></xs:schema>')
    # not well-formed → fail-open, no finding from the instance path
    assert V.validate_xml_blocks("```xml\n<Root><unclosed>\n```", [schema]) == []
