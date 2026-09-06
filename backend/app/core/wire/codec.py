# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The `WireCodec` Protocol — read a captured payload at registry field paths.

Deliberately tiny. The assertion engine (`services/cert_assertions.py`) needs
node counts (occurrence / mandatory) and node values (datatype / length / enum
/ pattern); `response_code` assertions never touch the payload at all. There is
NO write/render path here: request variants are materialized as *data* merged
into the harness call, and the executing side renders its own bodies. Adding a
`render()` before a second harness needs one would be the speculative-codec
trap in another shape.

PATH GRAMMAR — `ApiField.xpath` strings, NOT full XPath. The registry stores
paths like ``Message/Header/@version``: slash-separated element segments
starting at the document root, with an ``@name`` leaf for an attribute. No
predicates, no axes, no wildcards. That grammar is the registry's stable join
key, and a codec owns interpreting it for its format — a JSON codec would read
the same string as key traversal.

(The example is deliberately generic: `core/` must not name any one domain's
message types, even illustratively — that is what the domain-term ratchet in
`scripts/ci/hygiene-check.sh` measures.)

`parse` is separate from `count`/`values` because one captured body is
evaluated against MANY assertion rows: parse once per body, then read per row.
"""
from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable

__all__ = ["CodecError", "WireCodec"]


class CodecError(ValueError):
    """The body could not be parsed as this wire format.

    Policy lives ABOVE the codec: the evaluation layer decides what an
    unparseable capture means (our capture defect → SKIP, never a partner
    FAIL). The codec only reports the fact.
    """


@runtime_checkable
class WireCodec(Protocol):
    """Read one wire format. Stateless; implementations are cached by the
    registry and shared freely."""

    key: str  # format key as stored on assertion rows, e.g. "xml"

    def parse(self, body: str | bytes) -> Any:
        """Parse a captured body into an opaque document handle.

        Raises `CodecError` when the body is not this format. The handle is
        meaningful only to this codec's other methods.
        """
        ...

    def count(self, doc: Any, path: str) -> int:
        """Number of nodes at `path` — the occurrence/mandatory read.

        A node counts by PRESENCE: an element with children and no text, or an
        empty element, still occurs.
        """
        ...

    def values(self, doc: Any, path: str) -> Sequence[str]:
        """The value carried by each node at `path`, in document order.

        One entry per matched node: attribute values verbatim; element text
        verbatim, `""` for a present-but-valueless element. No normalisation —
        whether whitespace matters is the assertion's judgement, not the
        codec's.
        """
        ...

    # ── the write half (§3.1 variant materialisation) ────────────────────────
    #
    # Added when the harness began EXECUTING variants rather than merely
    # recording them: without it every variant of a case sends byte-identical
    # bytes and the variant axis is decorative on the wire.
    #
    # Deliberately primitive — set an EXISTING node, report how many were
    # set. It does NOT create missing structure: a path absent from the
    # template is a mismatch between the variant and the message shape, and
    # inventing nodes to hold the value would let a variant certify a
    # document the registry never described. What an unmatched path means is
    # policy, and policy lives above the codec.

    def set_value(self, doc: Any, path: str, value: str) -> int:
        """Set `value` at every existing node at `path`. Returns how many
        nodes were set; 0 means the path is not in this document."""
        ...

    def serialize(self, doc: Any) -> str:
        """The document back to wire text, for sending."""
        ...
