# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Shared pytest configuration for the backend test suite.

Holds one thing today: a way for a test to say "I am asserting the NETWORK
pack's vocabulary" out loud.

WHY THAT NEEDS SAYING. The platform default used to be the network pack, so a
test that wanted network vocabulary got it by doing nothing at all — or, worse,
by `monkeypatch.delenv("DOMAIN_PACK")`, which read as "isolate from the
environment" and happened to mean "select the network pack". Those two
intentions were indistinguishable while there was only one plausible default.

The default is now the domain-neutral `generic` pack, which is the correct
default for a platform that is not a payments product. That single change
turned every silent dependency into a failure: taxonomy lookups returning only
`general`, `channel_of()` returning None, message-name matching finding no
`ReqTransfer`/`RespBalEnq`, the party-flow gate declining to reject because it
has no declared parties to reject against. None of those are bugs — they are
the neutral pack behaving correctly and the tests never having said which pack
they meant.

So: if a test asserts payments vocabulary, it must ASK for the payments pack.
"""
from __future__ import annotations

import pathlib

import pytest

_PACKS_DIR = pathlib.Path(__file__).resolve().parents[1] / "app" / "packs"

NETWORK_PACK = str(_PACKS_DIR / "network" / "network.yaml")
NLLN_PACK = str(_PACKS_DIR / "nlln" / "nlln.yaml")
GENERIC_PACK = str(_PACKS_DIR / "generic" / "generic.yaml")


def use_pack(monkeypatch, pack_path: str) -> None:
    """Point DOMAIN_PACK at `pack_path` and drop the registry's cache.

    The cache clear is not optional: `registry._load` is `lru_cache`d on the
    path, and the module-level pack objects several agents build at import time
    are resolved through it, so a test that sets the env var without clearing
    gets whichever pack happened to load first in the session.
    """
    from app.core.domain import registry

    monkeypatch.setenv("DOMAIN_PACK", pack_path)
    registry._load.cache_clear()


@pytest.fixture
def network_pack(monkeypatch):
    """Pin the network (payments) pack for one test.

    Request this in any test whose assertions name payments vocabulary —
    `payment_initiation`, `ReqTransfer`, the `a2a` channel, U-codes. The
    alternative is a test that passes only on a deployment configured for
    payments, which is the exact coupling this pack seam exists to remove.
    """
    use_pack(monkeypatch, NETWORK_PACK)
    yield NETWORK_PACK
    from app.core.domain import registry
    registry._load.cache_clear()
