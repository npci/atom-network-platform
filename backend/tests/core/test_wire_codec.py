# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The wire-format codec seam: XML codec semantics + registry resolution.

The codec is what lets `services/cert_assertions.py` stay format-agnostic:
occurrence/mandatory read `count`, datatype/length/enum/pattern read `values`,
and the format key travels as DATA on stored assertion rows. These tests pin
the read semantics the assertion kinds depend on — presence counting for
valueless elements, verbatim values, namespace-stripped matching — and the
loud-on-unknown registry behaviour.
"""
import pytest

from app.adapters.wire.xml_codec import XmlCodec
from app.core.wire import CodecError, UnknownWireFormatError, WireCodec, codec_for

# Namespaced root, unqualified children, attributes, repeats, an empty element,
# a container element — the shapes a captured payload actually has.
SAMPLE = (
    '<network:ReqTransfer xmlns:network="http://example.org/schema/">'
    '  <Head ver="2.0" msgId="M1"/>'
    '  <Txn id="T1" note="">'
    '    <Amt value="100.00" curr="INR"/>'
    "  </Txn>"
    '  <Payer addr="a@bank"><Info/></Payer>'
    "  <Ref>first</Ref>"
    "  <Ref>second</Ref>"
    "  <Empty></Empty>"
    "</network:ReqTransfer>"
)


@pytest.fixture()
def codec():
    return XmlCodec()


@pytest.fixture()
def doc(codec):
    return codec.parse(SAMPLE)


# ── parse ────────────────────────────────────────────────────────────────────

def test_garbage_raises_codec_error(codec):
    with pytest.raises(CodecError):
        codec.parse("this is not xml <at all")


def test_empty_body_raises_codec_error(codec):
    with pytest.raises(CodecError):
        codec.parse("")
    with pytest.raises(CodecError):
        codec.parse(b"   ")


def test_str_body_with_encoding_declaration_parses(codec):
    """ElementTree rejects str+encoding-declaration; the codec re-parses as
    bytes rather than surfacing that quirk to the engine."""
    body = '<?xml version="1.0" encoding="UTF-8"?><ReqTransfer><Ref>x</Ref></ReqTransfer>'
    doc = codec.parse(body)
    assert codec.values(doc, "ReqTransfer/Ref") == ["x"]


def test_bytes_body_parses(codec):
    doc = codec.parse(SAMPLE.encode("utf-8"))
    assert codec.count(doc, "ReqTransfer") == 1


# ── count / values: elements ─────────────────────────────────────────────────

def test_namespaced_root_matches_local_name(codec, doc):
    """Registry paths are namespace-free; `{ns}ReqTransfer` matches `ReqTransfer`."""
    assert codec.count(doc, "ReqTransfer") == 1


def test_repeated_elements_count_and_order(codec, doc):
    assert codec.count(doc, "ReqTransfer/Ref") == 2
    assert codec.values(doc, "ReqTransfer/Ref") == ["first", "second"]


def test_empty_element_is_present_with_empty_value(codec, doc):
    """`<Empty></Empty>` occurs (mandatory sees it) and carries "" (length
    sees an empty string) — presence and value are different questions."""
    assert codec.count(doc, "ReqTransfer/Empty") == 1
    assert codec.values(doc, "ReqTransfer/Empty") == [""]


def test_container_element_counts_without_a_text_value(codec, doc):
    """An element holding only children still occurs — occurrence assertions
    on structural nodes must not read them as absent."""
    assert codec.count(doc, "ReqTransfer/Payer") == 1
    assert codec.values(doc, "ReqTransfer/Payer") == [""]


def test_nested_path(codec, doc):
    assert codec.count(doc, "ReqTransfer/Txn/Amt") == 1


def test_wrong_root_matches_nothing(codec, doc):
    assert codec.count(doc, "RespTransfer/Ref") == 0
    assert codec.values(doc, "RespTransfer/Ref") == []


def test_absent_path_matches_nothing(codec, doc):
    assert codec.count(doc, "ReqTransfer/NoSuchChild") == 0
    assert codec.values(doc, "ReqTransfer/NoSuchChild") == []


def test_empty_path_matches_nothing(codec, doc):
    assert codec.count(doc, "") == 0
    assert codec.values(doc, "") == []


# ── count / values: attributes ───────────────────────────────────────────────

def test_attribute_value_verbatim(codec, doc):
    assert codec.count(doc, "ReqTransfer/Head/@ver") == 1
    assert codec.values(doc, "ReqTransfer/Head/@ver") == ["2.0"]


def test_absent_attribute_is_absent(codec, doc):
    """An element that exists but lacks the attribute: the ATTRIBUTE does not
    occur — mandatory on `.../@x` must not be satisfied by the bare element."""
    assert codec.count(doc, "ReqTransfer/Head/@missing") == 0
    assert codec.values(doc, "ReqTransfer/Head/@missing") == []


def test_empty_attribute_is_present_with_empty_value(codec, doc):
    assert codec.count(doc, "ReqTransfer/Txn/@note") == 1
    assert codec.values(doc, "ReqTransfer/Txn/@note") == [""]


def test_attribute_on_nested_element(codec, doc):
    assert codec.values(doc, "ReqTransfer/Txn/Amt/@curr") == ["INR"]


# ── registry ─────────────────────────────────────────────────────────────────

def test_codec_for_xml_resolves_and_satisfies_the_protocol():
    resolved = codec_for("xml")
    assert isinstance(resolved, WireCodec)
    assert resolved.key == "xml"


def test_codec_for_is_case_insensitive():
    assert codec_for("XML").key == "xml"


def test_unknown_format_is_loud_and_names_the_known_set():
    """Never a silent fallback: the wrong parser would grade every field as a
    confident FAIL against an innocent partner."""
    with pytest.raises(UnknownWireFormatError) as exc:
        codec_for("protobuf")
    assert "xml" in str(exc.value)


def test_core_registry_does_not_import_the_adapter_at_module_import():
    """Same discipline as core/domain/registry: core carries import-path
    STRINGS and resolves lazily, keeping core→adapters a call-time edge."""
    import inspect

    from app.core.wire import registry

    src = inspect.getsource(registry)
    top_level_imports = [
        line for line in src.splitlines()
        if line.startswith(("import ", "from ")) and "adapters" in line
    ]
    assert top_level_imports == []


# ── the write half (§3.1 variant materialisation) ────────────────────────────

class TestSetValue:
    """Set an EXISTING node; never invent structure — a path absent from the
    template is a mismatch, and creating nodes would let a variant certify a
    document the registry never described."""

    def _codec(self):
        from app.core.wire.registry import codec_for
        return codec_for("xml")

    def test_sets_an_attribute_and_round_trips(self):
        c = self._codec()
        doc = c.parse('<ReqTransfer><Txn type="PAY"/></ReqTransfer>')
        assert c.set_value(doc, "ReqTransfer/Txn/@type", "COLLECT") == 1
        assert list(c.values(doc, "ReqTransfer/Txn/@type")) == ["COLLECT"]
        assert "COLLECT" in c.serialize(doc)

    def test_sets_element_text(self):
        c = self._codec()
        doc = c.parse("<ReqTransfer><Note>old</Note></ReqTransfer>")
        assert c.set_value(doc, "ReqTransfer/Note", "new") == 1
        assert list(c.values(doc, "ReqTransfer/Note")) == ["new"]

    def test_an_absent_path_sets_nothing_and_says_so(self):
        c = self._codec()
        doc = c.parse("<ReqTransfer/>")
        assert c.set_value(doc, "ReqTransfer/Nope/@x", "v") == 0
        assert "Nope" not in c.serialize(doc), "no structure invented"

    def test_sets_every_matching_node(self):
        c = self._codec()
        doc = c.parse('<R><I n="1"/><I n="2"/></R>')
        assert c.set_value(doc, "R/I/@n", "9") == 2
        assert list(c.values(doc, "R/I/@n")) == ["9", "9"]

    def test_namespaced_attributes_match_by_local_name(self):
        c = self._codec()
        doc = c.parse('<R xmlns:a="urn:x"><I a:n="1"/></R>')
        assert c.set_value(doc, "R/I/@n", "9") == 1
        assert list(c.values(doc, "R/I/@n")) == ["9"]

    def test_xmlns_is_refused_rather_than_written_back_invisibly(self):
        """The read side recovers xmlns from the tag because ElementTree
        consumes declarations at parse time — writing one would set a value
        that reads back absent."""
        c = self._codec()
        doc = c.parse('<R xmlns="urn:x"/>')
        assert c.set_value(doc, "R/@xmlns", "urn:y") == 0
