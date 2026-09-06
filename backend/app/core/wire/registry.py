# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Resolve a wire-format key to its codec.

Same shape as `core/domain/registry` and for the same reasons: import paths
rather than imported modules (resolving a codec must not drag every codec's
dependencies into every process), `lru_cache` because codecs are stateless, and
LOUD on an unknown key — grading one format's payload with another format's
parser would surface as a wall of confident FAILs against an innocent partner,
which is the worst possible way to discover a typo.

Where the key comes from: the active pack declares its domain's default
(`wire_format_of(pack)`), the case builder SNAPSHOTS that key onto every stored
assertion/variant row, and evaluation resolves `codec_for(row.wire_format)`.
The pack is consulted at generation time only — stored rounds stay
reproducible no matter what the pack says later.
"""
from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.wire.codec import WireCodec

__all__ = ["UnknownWireFormatError", "codec_for", "known_formats"]

# Import paths, not imports — see module docstring.
_CODECS: dict[str, str] = {
    "xml": "app.adapters.wire.xml_codec:XmlCodec",
}


class UnknownWireFormatError(RuntimeError):
    """Raised when a format key names no registered codec.

    Deliberately loud, never a fallback: a silent default would evaluate a
    payload with the wrong parser and report the mismatches as partner
    failures.
    """


def known_formats() -> tuple[str, ...]:
    return tuple(sorted(_CODECS))


@lru_cache(maxsize=None)
def codec_for(format_key: str) -> "WireCodec":
    """The codec registered for `format_key` (case-insensitive)."""
    key = (format_key or "").strip().lower()
    try:
        target = _CODECS[key]
    except KeyError:
        known = ", ".join(sorted(_CODECS)) or "(none registered)"
        raise UnknownWireFormatError(
            f"wire format {format_key!r} has no registered codec. Known: {known}."
        ) from None

    module_path, _, class_name = target.partition(":")
    from importlib import import_module

    cls = getattr(import_module(module_path), class_name)
    return cls()
