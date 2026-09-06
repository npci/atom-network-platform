# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Wire-format codecs — implementations of `app.core.wire.codec.WireCodec`.

One module per format. XML ships today; a JSON codec joins when a domain that
speaks JSON exists to prove it (`docs/COMBINED_EXECUTION_PLAN.md` follow-ups).
A codec is technology, not domain: nothing in here may know about UPI, NPCI, or
any pack's vocabulary.
"""
