# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""XML `WireCodec` — stdlib ElementTree, namespace-stripped local-name match.

Pure stdlib on purpose: `services/cert_assertions.py` imports nothing but the
codec Protocol and `re`, and its tests instantiate `XmlCodec()` directly — no
app graph, no DB, no fixtures beyond strings.

NAMESPACES. Captured payloads carry `xmlns` declarations; registry paths are
namespace-free (`ReqTransfer/Head/@ver`). So matching is by LOCAL NAME — `{ns}Head`
matches segment `Head` — for elements and attributes both. Two same-named
elements in different namespaces would collapse into one match, which is
acceptable for this grammar: the registry's own paths cannot tell them apart
either.

VALUES ARE VERBATIM. Element text is returned exactly as parsed (`""` for a
present-but-valueless element); no stripping, no type coercion. Whether
whitespace matters is the assertion's judgement.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Sequence

from app.core.wire.codec import CodecError

__all__ = ["XmlCodec"]

# Security note: bodies reaching this codec are certification captures already
# accepted by the platform's own transport, not arbitrary user uploads. stdlib
# ElementTree does not resolve external entities (no XXE) and rejects entity
# expansion bombs since CPython 3.7's default limits; the XSD ingest path
# (`services/api_registry_ingest.py`) keeps its own hardened parser.


def _local(tag: str) -> str:
    """`{urn:ns}Head` -> `Head`; unqualified tags pass through."""
    return tag.rsplit("}", 1)[-1]


class XmlCodec:
    key = "xml"

    def parse(self, body: str | bytes) -> ET.Element:
        if body is None or (isinstance(body, (str, bytes)) and not body.strip()):
            raise CodecError("empty body is not an XML document")
        try:
            return ET.fromstring(body)
        except ValueError:
            # str body with an encoding declaration ("Unicode strings with
            # encoding declaration are not supported") — re-parse as bytes.
            if isinstance(body, str):
                try:
                    return ET.fromstring(body.encode("utf-8"))
                except ET.ParseError as exc:
                    raise CodecError(f"body is not well-formed XML: {exc}") from exc
            raise
        except ET.ParseError as exc:
            raise CodecError(f"body is not well-formed XML: {exc}") from exc

    def count(self, doc: ET.Element, path: str) -> int:
        elements, attr = self._resolve(doc, path)
        if attr is None:
            return len(elements)
        return sum(1 for el in elements if self._attr_get(el, attr) is not None)

    def values(self, doc: ET.Element, path: str) -> Sequence[str]:
        elements, attr = self._resolve(doc, path)
        if attr is None:
            return [el.text if el.text is not None else "" for el in elements]
        found = (self._attr_get(el, attr) for el in elements)
        return [v for v in found if v is not None]

    def set_value(self, doc: ET.Element, path: str, value: str) -> int:
        """Set an EXISTING node's value; never create one (see the Protocol).

        `xmlns` is refused rather than written: the read side recovers it from
        the element's Clark-notation tag because ElementTree consumes
        namespace declarations at parse time, so writing an `xmlns` attribute
        would produce a document whose namespace the parser then ignores —
        a value that appears set and reads back absent.
        """
        elements, attr = self._resolve(doc, path)
        if attr == "xmlns":
            return 0
        set_count = 0
        for el in elements:
            if attr is None:
                el.text = value
            else:
                key = next((k for k in el.attrib if _local(k) == attr), attr)
                el.set(key, value)
            set_count += 1
        return set_count

    def serialize(self, doc: ET.Element) -> str:
        return ET.tostring(doc, encoding="unicode")

    # ── internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _attr_get(el: ET.Element, name: str) -> str | None:
        """Attribute by local name — exact key first, then namespaced keys.

        `xmlns` is special: ElementTree consumes namespace DECLARATIONS during
        parsing (they never appear in `attrib`), but the registry models them
        as `.../@xmlns` field rows — and without this recovery the engine
        would report a bound namespace as ABSENT and fail the partner for
        data that is right there in the payload. The element's Clark-notation
        tag (`{uri}Name`) carries the truth: a qualified element yields its
        namespace URI, an unqualified one yields no match.
        """
        if name == "xmlns":
            tag = el.tag
            if isinstance(tag, str) and tag.startswith("{"):
                return tag[1:].split("}", 1)[0]
            return None
        if name in el.attrib:
            return el.attrib[name]
        for key, value in el.attrib.items():
            if _local(key) == name:
                return value
        return None

    @staticmethod
    def _resolve(doc: ET.Element, path: str) -> tuple[list[ET.Element], str | None]:
        """Walk `path` from the document root; return (matched elements, attr).

        `attr` is the trailing `@name` when present, else None. An empty or
        root-mismatched path matches nothing — the occurrence assertion then
        reads "0 found", which is the honest answer for a path this document
        does not contain.
        """
        segments = [s for s in (path or "").split("/") if s]
        attr: str | None = None
        if segments and segments[-1].startswith("@"):
            attr = segments.pop()[1:]
        if not segments:
            return [], attr
        if _local(doc.tag) != segments[0]:
            return [], attr
        nodes: list[ET.Element] = [doc]
        for seg in segments[1:]:
            nodes = [
                child
                for node in nodes
                for child in node
                if isinstance(child.tag, str) and _local(child.tag) == seg
            ]
            if not nodes:
                break
        return nodes, attr
