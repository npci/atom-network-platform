# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Domain-neutral contracts shared by the platform core.

Nothing in this package may import from `app.agents`, `app.docgen` or any
other pipeline: the dependency arrow points inward. Domain-specific CONTENT
(section wording, participant names, error-code vocabularies) belongs in a
domain pack.
"""
