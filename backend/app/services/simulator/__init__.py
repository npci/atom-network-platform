# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The pack-driven simulator (SIM-2…SIM-5) — greenfield Python.

Decision 2026-08-31: the old Java cert-simulator is forgotten, not extended.
This package IS the simulator: a pack store over `sim_packs` (migration
0134), a chain-merge resolver cached by content address, the engine's
advertised capability set, and — with SIM-3/SIM-4 — validation and scenario
engines that grade and answer requests from pack data alone.

The rules that must never soften (SIMULATOR_MIGRATION_PLAN §3.1):

* `?pack=` absent → the active published baseline. No baseline published →
  no pack constraint at all (the pre-pack world), stated, not guessed.
* `?pack=` present and unknown OR withdrawn → HTTP 400 `unknown_pack`,
  NEVER a silent fallback — a silent fallback certifies a bank against the
  old contract while the report says the new one.
* Packs are immutable and content-addressed; resolution is cached by
  `pack_id` chain, safe forever.
"""
from app.services.simulator.engine import CAPABILITIES, ENGINE_VERSION  # noqa: F401
from app.services.simulator.resolver import (  # noqa: F401
    ResolvedPack,
    UnknownPackError,
    resolve,
)
