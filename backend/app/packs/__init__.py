# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Domain packs.

A pack supplies the CONTENT for one ecosystem — change ontology, artifact
blueprints, prompt vocabulary, validation rules — against the contract in
`app.core.domain.contract`. Core supplies the machinery and must never import a
pack directly; it resolves one through `app.core.domain.registry`.
"""
