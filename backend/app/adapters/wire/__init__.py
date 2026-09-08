# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Wire-format codecs — implementations of `app.core.wire.codec.WireCodec`.

One module per format. XML ships today; a JSON codec joins when a domain that
speaks JSON exists to prove it.
A codec is technology, not domain: nothing in here may know about the payment
network, its authority, or any pack's vocabulary.
"""
