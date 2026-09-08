# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Knowledge-graph package.

Hosts the Cypher client (`client.py`) and graph schema (`schema.py`) for the
Apache AGE extension running alongside pgvector in the same Postgres cluster.
Populated by `ingest_from_rag.py`.
Queried by the graph retriever and the impact analyzer.
"""
