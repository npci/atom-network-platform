# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Evaluation harness — runtime quality layer for stage-to-stage handoffs.

Phase 0: contracts, schemas, hard-fail catalog, policy definitions.
Phase 1: advisory runtime runner, verdict persistence, read APIs.
Phase 2: gate activation, retry, override, audit trail.

Importing this package has no side effects.
No DB, no LLM, no API imports at package level.
"""
