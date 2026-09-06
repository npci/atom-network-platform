# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Shared pytest configuration for the backend test suite.

Slice 1 scope: no fixtures needed — the characterization tests in
`tests/rag/test_hybrid_characterization.py` are pure/data-only and exercise
constants + pure functions without touching the database or LLM.

Future slices will add fixtures here (e.g. `db_session`, `mock_llm`,
`sample_chunks`). Kept deliberately minimal for now.
"""
