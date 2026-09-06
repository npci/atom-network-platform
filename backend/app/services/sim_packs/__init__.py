# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Capability packs (SIM thread) — immutable, content-addressed simulator
contracts generated from the API Registry.

`contract.py` is the S-0 pack contract (pure: model, validation, canonical
bytes, content hash). `builder.py` (S-1) projects the same registry delta that
builds `cert_case_specs`/`cert_request_variants` into a pack, so the
simulator's behaviour and the grader's expectations cannot drift — neither is
authored, both are projections of the same rows.
"""
from app.services.sim_packs.contract import (  # noqa: F401
    PackValidationError,
    SimPack,
    canonical_json,
    content_hash,
    stamp,
    validate_pack,
)
