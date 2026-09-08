# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""NLLN domain pack — the library-loan change-management reference domain.

Exists to prove `DomainPack` genuinely decouples the platform from the payment
network: it plugs in an unrelated ecosystem (a library-consortium loan protocol,
not a payment system) through the same seam `app.packs.network` uses, with no
core code change. See `app.core.domain.registry` for how `DOMAIN_PACK` selects it.
"""
