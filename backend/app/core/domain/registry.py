# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Resolve the active domain pack.

Core never imports a pack directly — it asks here. That indirection is the
seam: `DOMAIN_PACK` names a YAML file (see `app.core.domain.config_pack` for
the schema) and the code around this module never knows which domain it got.

WHY THIS RESOLVES AT IMPORT TIME, with no DB and no request context: several
agents build their system prompts as module-level f-strings evaluated when the
module is first imported (`agents/canvas.py:31`, `agents/xsd.py:15,55`). A
registry that needed a database session or a request would be unusable from
those call sites, so selection is by environment variable only.

Selection: `DOMAIN_PACK`, a file path, defaulting to `packs/network/network.yaml`
(`DEFAULT_PACK` below) when unset.

DORMANT, ON PURPOSE: this module previously also supported a registered-KEY
form (`DOMAIN_PACK=network` / `DOMAIN_PACK=nlln`) resolving through a `_PACKS`
dict to a real Python class (`app.packs.network.pack.NetworkPack`,
`app.packs.nlln.pack.NllnPack`). Those classes still exist — `NetworkPack` still
implements real `certification()`/`channel()` (an actual cert harness, the
A2A partner channel) — but the registry no longer resolves DOMAIN_PACK to
them. A YAML file cannot express behaviour, only data, so this is a real,
deliberate trade: the ONLY active resolution path now returns vocabulary
(prompt blocks, participants, cert vocabulary, operation/risk/compliance
choice lists) and never certification/channel/validators, for ANY domain,
including the default network domain. `certification_of()`/`channel_of()` on whatever pack is
currently active will always return `None` as a result. To get the network pack's real
certification/channel capability back, a caller would need to import
`app.packs.network.pack.NetworkPack` directly rather than going through this
registry — this module intentionally no longer offers that path.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid importing the contract (and pydantic) at runtime here
    from app.core.domain.contract import DomainPack

# Fallback when DOMAIN_PACK is unset — still just a YAML path, resolved
# relative to this file so it works regardless of the process's cwd. NOT a
# `_PACKS`-style registry lookup: this is the same generic config_pack.load()
# path every other value goes through.
DEFAULT_PACK = str(Path(__file__).resolve().parents[2] / "packs" / "network" / "network.yaml")


class UnknownPackError(RuntimeError):
    """Raised when DOMAIN_PACK names a file that can't be loaded as a domain
    pack (see `config_pack.ConfigPackError`, re-raised through here so
    callers only need to catch one exception type).

    Deliberately loud. Silently falling back to a default would mean a
    deployment intending one domain's vocabulary quietly generating documents
    in another's — a failure that shows up as subtly wrong prose, not as an
    error, which is the worst possible way to find it.
    """


def active_pack_key() -> str:
    # No lowercasing: this is always a filesystem path now, and paths (and
    # this repo's own checkout) can be case-sensitive.
    return (os.environ.get("DOMAIN_PACK") or DEFAULT_PACK).strip()


@lru_cache(maxsize=None)
def _load(key: str) -> "DomainPack":
    from app.core.domain.config_pack import ConfigPackError, load as load_config_pack

    try:
        return load_config_pack(key)
    except ConfigPackError as exc:
        raise UnknownPackError(f"DOMAIN_PACK={key!r}: {exc}") from exc


def get_active_pack() -> "DomainPack":
    """The pack selected by `DOMAIN_PACK`. Cached — packs are stateless."""
    return _load(active_pack_key())


def prompt_block(name: str, default: str = "") -> str:
    """One named prompt block from the active pack.

    Returns `default` when the pack does not supply that block. This is not
    defensive padding: a domain with no publishing authority supplies no
    "authority" block, and that is a valid domain rather than a broken pack.
    The contract requires core to treat every block name as optional.
    """
    try:
        blocks = get_active_pack().prompt_blocks()
    except UnknownPackError:
        raise
    except Exception:  # noqa: BLE001 — a malformed pack must not break imports
        return default
    return blocks.get(name, default) or default
