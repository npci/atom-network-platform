# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Where the authority-policy seed document comes from.

One resolver, shared by the first-boot seeder (`app.main`) and the
`reset-to-seed` endpoint (`app.api.authority_policy`), because the two silently
disagreeing about the source file is exactly the bug that makes a reset restore
something other than what was seeded.

Order, most specific first:

1. **`settings.authority_policy_path`**, when the file exists. This is the
   operator's document — `docker-compose.yml` binds it into the container, so
   editing it on the host and re-seeding is the supported way to supply real
   policy without rebuilding an image.
2. **The active pack's `data/authority_policy.md`**, when it ships one. A
   domain's own policy corpus is pack content, and this is the seam that lets a
   pack carry it.
3. **Nothing** — the row seeds empty and the resolver says it has no policy
   rather than inventing one.

The platform's own copy at `backend/data/authority_policy.md` is an empty
TEMPLATE, deliberately. It used to be an 818-line brief on one payments
network's operating circulars, listing real banks and their market shares,
which every deployment of this platform installed into its database on first
boot and fed to an LLM as authoritative policy. That corpus now lives in the
network pack, where a deployment of that pack picks it up through rule 2 and no
other deployment ever sees it.
"""
from __future__ import annotations

from pathlib import Path

from app.core.config import settings

POLICY_SEED_FILENAME = "authority_policy.md"


def authority_policy_seed_path() -> Path | None:
    """The file to seed / reset the policy singleton from, or None."""
    configured = Path(settings.authority_policy_path)
    if configured.is_file():
        return configured

    # Imported here, not at module scope: registry resolves DOMAIN_PACK from the
    # environment at call time and several tests swap packs mid-process.
    from app.core.domain.registry import pack_data_file

    return pack_data_file(POLICY_SEED_FILENAME)
