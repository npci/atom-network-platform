# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""xsd_context pure helpers — the XSD→TSD grounding tripwire."""
from app.services import xsd_context as X

_SCHEMA = ('<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
           '<xs:element name="ReqTransfer"/><xs:complexType name="Amount"/></xs:schema>')


def test_tripwire_sees_namespaced_sample_tags():
    # <network:ReqTransfer> must resolve to the schema's ReqTransfer — a prefix-blind regex made
    # every namespaced sample invisible to the tripwire.
    tsd = "```xml\n<network:ReqTransfer><network:Amount/></network:ReqTransfer>\n```"
    assert X.xml_tag_tripwire(tsd, _SCHEMA) == []


def test_tripwire_flags_invented_tags_but_not_xs_scaffolding():
    tsd = ("```xml\n<network:ReqTransfer><network:Invented/></network:ReqTransfer>\n```\n"
           "```xml\n<xs:element name=\"whatever\"/>\n```")
    assert X.xml_tag_tripwire(tsd, _SCHEMA) == ["Invented"]


def test_tripwire_silent_without_schema_ground_truth():
    assert X.xml_tag_tripwire("```xml\n<Foo/>\n```") == []
