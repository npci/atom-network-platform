# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Wire-format seam — how the platform reads a captured payload.

The certification engine asserts registry constraints against request/response
bodies it did not produce. Everything it needs from a body is two operations at
a registry field path: how many nodes are there, and what values do they carry.
`WireCodec` names exactly that surface; `codec_for` resolves a format key to an
implementation.

This lives in `core/` because it is a Protocol the engine depends on, not a
technology. Implementations live in `app/adapters/wire/` (an adapter implements
a core Protocol against one concrete technology — see `app/adapters/__init__`),
and the DEFAULT format for a domain is declared by its pack via
`app.core.domain.contract.wire_format_of`. The binding recorded on stored
assertion rows is data (`wire_format` column), so re-evaluating an old round
never depends on what the pack says today.
"""
from app.core.wire.codec import CodecError, WireCodec
from app.core.wire.registry import UnknownWireFormatError, codec_for

__all__ = ["CodecError", "WireCodec", "UnknownWireFormatError", "codec_for"]
