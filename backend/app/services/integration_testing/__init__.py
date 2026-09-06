# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The integration-testing tunnel — impure halves (ITA I-1+).

The PURE halves live in `app.a2a_common.integration_contract` and
`integration_allowlist`: they are vendored byte-for-byte into both platforms
because a tunnel whose two ends disagree about header rules or alias resolution
corrupts silently. What lives HERE is per-service and legitimately differs:

  ingress.py   a local HTTP request → an A2A send to the far platform
  egress.py    an inbound A2A exchange → a local HTTP call (I-4 on this side)

Off by default (`integration_testing_enabled`), dev-only, and the ingress is
classified H3 — externally reachable and hostile.
"""
