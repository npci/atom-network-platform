# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Resolve an eval gold set for the ACTIVE domain pack.

A gold set is domain data, not platform data. "How does a transaction flow from
the payer's PSP to the beneficiary bank?" is a question about one industry; so
is every keyword it is scored on. Shipping those at the platform level made the
evaluation harness assert that this is a payments product, and made the numbers
meaningless for any other domain — a library-lending deployment was graded on
whether its retrieval could find UPI limits.

So: the platform ships a small DOMAIN-NEUTRAL gold set, and a pack may override
it with cases written against its own corpus and vocabulary.

    <pack dir>/eval/<name>.jsonl     the active pack's cases, if present
    tests/eval/<name>.jsonl          the neutral default

Resolution is by convention rather than a declared pack key: a gold set is test
data, and requiring every pack author to declare one in order to run an eval
they may not use is friction for no gain. A pack that ships no `eval/` directory
simply gets the neutral cases.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent


def _active_pack_dir() -> Path | None:
    """Directory of the active domain pack, or None if it cannot be resolved.

    Deliberately best-effort: an eval must still run when the pack registry is
    unavailable (no DOMAIN_PACK set, a syntactically broken pack, an import
    error under a bare `python tests/eval/run_*.py`). Falling back to the
    neutral gold set is always safe — it is the same set the platform shipped
    before packs could override it.
    """
    try:
        from app.core.domain.registry import get_active_pack
        pack = get_active_pack()
    except Exception as e:                      # pragma: no cover - env-dependent
        logger.debug("gold-set pack resolution unavailable (%s)", type(e).__name__)
        return None

    # Packs expose their location differently depending on how they were
    # loaded, so try the shapes rather than assuming one. `_source_path` is
    # what ConfigPack (a YAML pack) actually carries; the public names are
    # tried first in case that ever becomes one.
    for attr in ("pack_dir", "source_path", "path", "_source_path"):
        raw = getattr(pack, attr, None)
        if not raw:
            continue
        p = Path(str(raw))
        return p if p.is_dir() else p.parent

    # Fall back to the pack's declared key. Packs live at app/packs/<key>/ by
    # convention, and `key` is public API where the path attribute is not.
    key = (getattr(pack, "key", "") or "").strip()
    if key:
        candidate = Path(__file__).resolve().parents[1] / "app" / "packs" / key
        if candidate.is_dir():
            return candidate

    module = getattr(type(pack), "__module__", "")
    if module:
        try:
            import importlib
            mod_file = getattr(importlib.import_module(module), "__file__", None)
            if mod_file:
                return Path(mod_file).parent
        except Exception:                       # pragma: no cover - defensive
            pass
    return None


def resolve_gold(name: str) -> Path:
    """Path to gold set `name` (e.g. "retrieval_gold.jsonl") for the active pack.

    Returns the pack's own copy when it ships one, else the neutral default.
    Logs which was chosen — a run scored against a different gold set than the
    reader assumes is the kind of thing that should never be silent.
    """
    default = HERE / name
    pack_dir = _active_pack_dir()
    if pack_dir is not None:
        candidate = pack_dir / "eval" / name
        if candidate.is_file():
            logger.info("gold set %s: using the active pack's cases (%s)", name, candidate)
            return candidate
    logger.info("gold set %s: using the platform's domain-neutral cases", name)
    return default
