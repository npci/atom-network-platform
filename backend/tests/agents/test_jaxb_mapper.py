# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""JAXB element→Java link extraction + confidence tiers (§7.2/§7.3). Pure."""
from app.agents.jaxb_mapper import (
    extract_java_links, extract_xjb_links, build_links, split_by_confidence,
    parse_pom_jaxb, JaxbLink,
)


def _by_xpath(links):
    return {l.xpath: l for l in links}


def test_xml_root_element_named():
    links = extract_java_links(
        '@XmlRootElement(name = "Refund")\npublic class RefundType {}', "x/RefundType.java")
    m = _by_xpath(links)
    assert m["Refund"].source == "jaxb_root" and m["Refund"].confidence == 0.95
    assert "RefundType" in m["Refund"].symbol


def test_bare_xml_root_element_derives_lowercased_class():
    links = extract_java_links("@XmlRootElement\npublic class Payment {}", "x/Payment.java")
    m = _by_xpath(links)
    assert "payment" in m and m["payment"].confidence == 0.85


def test_xml_type_and_xml_element_field():
    links = extract_java_links(
        '@XmlType(name = "MoneyType")\npublic class Money {\n'
        '  @XmlElement(name="amount") java.math.BigDecimal amount;\n}', "x/Money.java")
    m = _by_xpath(links)
    assert m["MoneyType"].confidence == 0.90
    assert m["amount"].confidence == 0.85


def test_object_factory_element_decl_not_treated_as_field():
    java = ('@XmlRegistry\npublic class ObjectFactory {\n'
            '  @XmlElementDecl(namespace="urn:r", name="Refund")\n'
            '  public JAXBElement<RefundType> createRefund() { return null; }\n}')
    links = extract_java_links(java, "x/ObjectFactory.java")
    m = _by_xpath(links)
    assert m["Refund"].source == "object_factory" and m["Refund"].confidence == 0.92


def test_namespace_only_root_element_derives_from_class():
    # @XmlRootElement(namespace=...) with no name= must still derive a link.
    links = extract_java_links(
        '@XmlRootElement(namespace = "urn:pay")\npublic class Payment {}', "x/Payment.java")
    m = _by_xpath(links)
    assert "payment" in m and m["payment"].confidence == 0.85


def test_pom_output_dir_scoped_to_jaxb_plugin_not_compiler():
    pom = ("<project><build><plugins>"
           "<plugin><artifactId>maven-compiler-plugin</artifactId>"
           "<configuration><outputDirectory>target/classes</outputDirectory></configuration></plugin>"
           "<plugin><artifactId>jaxb2-maven-plugin</artifactId>"
           "<configuration><outputDirectory>target/generated-sources/jaxb</outputDirectory></configuration>"
           "</plugin></plugins></build></project>")
    cfg = parse_pom_jaxb(pom)
    assert cfg["output_dir"] == "target/generated-sources/jaxb"   # NOT target/classes


def test_nested_xjb_bindings_no_double_count():
    xjb = ('<jaxb:bindings xmlns:jaxb="j" schemaLocation="refund.xsd">'
           '<jaxb:bindings node="//xs:element[@name=\'Refund\']">'
           '<jaxb:class name="RefundDto"/></jaxb:bindings></jaxb:bindings>')
    links = extract_xjb_links(xjb, "x/binding.xjb")
    assert len(links) == 1                                   # only the inner node-binding
    assert links[0].xpath == "//xs:element[@name='Refund']" and "RefundDto" in links[0].symbol


def test_xjb_binding_link():
    xjb = ('<jaxb:bindings xmlns:jaxb="https://jakarta.ee/xml/ns/jaxb" version="3.0" '
           'node="//xs:element[@name=\'Refund\']">'
           '<jaxb:class name="RefundDto"/></jaxb:bindings>')
    links = extract_xjb_links(xjb, "x/binding.xjb")
    assert any(l.source == "xjb" and "RefundDto" in l.symbol for l in links)


def test_split_by_confidence_floor():
    links = [JaxbLink("A", "p", "jaxb_root", 0.95, {}),
             JaxbLink("B", "p", "rag", 0.40, {})]
    definite, needs = split_by_confidence(links, floor=0.55)
    assert [l.xpath for l in definite] == ["A"] and [l.xpath for l in needs] == ["B"]


def test_parse_pom_jaxb():
    pom = ("<project><build><plugins><plugin>"
           "<artifactId>jaxb2-maven-plugin</artifactId>"
           "<configuration><outputDirectory>target/generated-sources/jaxb</outputDirectory>"
           "</configuration></plugin></plugins></build></project>")
    cfg = parse_pom_jaxb(pom)
    assert cfg["plugin"] == "jaxb2-maven-plugin"
    assert cfg["output_dir"] == "target/generated-sources/jaxb"
    assert parse_pom_jaxb("<project/>") is None


def test_build_links_combines_java_and_xjb():
    links = build_links(
        java_files=[{"path": "R.java", "content": '@XmlRootElement(name="Refund")\nclass R {}'}],
        xjb_files=[{"path": "b.xjb", "content":
                    '<jaxb:bindings xmlns:jaxb="j" node="x"><jaxb:class name="C"/></jaxb:bindings>'}],
    )
    sources = {l.source for l in links}
    assert "jaxb_root" in sources and "xjb" in sources
