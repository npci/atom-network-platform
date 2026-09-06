# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Knowledge-graph package (Slice 19+).

Hosts the Cypher client (`client.py`) and graph schema (`schema.py`) for the
Apache AGE extension running alongside pgvector in the same Postgres cluster.
Populated by sub-slice 19a (`ingest_from_rag.py`, not in this slice).
Queried by Slice 20 (graph retriever) and Slice 21 (impact analyzer).
"""
