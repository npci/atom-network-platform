# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Pure unit tests for the KG client + schema constants (Slice 19).

These tests do NOT require a live Postgres/AGE — they verify the string
building and label constants only. Integration tests (live AGE) belong
in a separate module guarded by `@pytest.mark.age`.
"""
from __future__ import annotations

import pytest

from app.kg import schema as kg_schema
from app.kg.client import (
    build_cypher_sql,
    escape_cypher_literal,
    _decode_agtype,
)


# ──────────────────────────────────────────────────────────────────────────────
# build_cypher_sql
# ──────────────────────────────────────────────────────────────────────────────

class TestBuildCypherSql:

    def test_default_return_col_produces_result_agtype(self):
        sql = build_cypher_sql("MATCH (n) RETURN n", graph_name="npci_kg")
        assert "SELECT * FROM cypher('npci_kg'" in sql
        assert "AS (result agtype)" in sql
        assert "MATCH (n) RETURN n" in sql
        assert sql.strip().endswith(";")

    def test_tuple_return_cols_with_custom_types(self):
        sql = build_cypher_sql(
            "MATCH (n:Class) RETURN n.name, n.loc",
            graph_name="npci_kg",
            return_cols=[("name", "agtype"), ("loc", "agtype")],
        )
        assert "AS (name agtype, loc agtype)" in sql

    def test_string_return_cols_default_to_agtype(self):
        sql = build_cypher_sql(
            "RETURN 1", graph_name="npci_kg", return_cols=["v"],
        )
        assert "AS (v agtype)" in sql

    def test_empty_cypher_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            build_cypher_sql("", graph_name="npci_kg")
        with pytest.raises(ValueError, match="non-empty"):
            build_cypher_sql("   ", graph_name="npci_kg")

    def test_dollar_quote_in_cypher_rejected(self):
        # $$ would terminate the outer dollar-quoted SQL literal.
        with pytest.raises(ValueError, match="\\$\\$"):
            build_cypher_sql("MATCH (n) $$ bad", graph_name="npci_kg")

    def test_invalid_graph_name_rejected(self):
        # Graph names must be alphanumeric + underscore.
        with pytest.raises(ValueError, match="invalid graph_name"):
            build_cypher_sql("RETURN 1", graph_name="bad'; DROP TABLE x;--")
        with pytest.raises(ValueError, match="invalid graph_name"):
            build_cypher_sql("RETURN 1", graph_name="")

    def test_invalid_return_col_name_rejected(self):
        with pytest.raises(ValueError, match="invalid return_col name"):
            build_cypher_sql(
                "RETURN 1", graph_name="npci_kg",
                return_cols=[("1bad", "agtype")],
            )
        with pytest.raises(ValueError, match="invalid return_col name"):
            build_cypher_sql(
                "RETURN 1", graph_name="npci_kg",
                return_cols=[("drop; --", "agtype")],
            )

    def test_invalid_return_col_type_rejected(self):
        with pytest.raises(ValueError, match="invalid return_col type"):
            build_cypher_sql(
                "RETURN 1", graph_name="npci_kg",
                return_cols=[("v", "agtype; DROP")],
            )

    def test_underscore_graph_name_allowed(self):
        sql = build_cypher_sql("RETURN 1", graph_name="my_graph_2")
        assert "cypher('my_graph_2'" in sql

    def test_body_preserves_newlines_and_spacing(self):
        body = "MATCH (a:Function)-[:CALLS]->(b)\nRETURN a.name, b.name"
        sql = build_cypher_sql(body, graph_name="npci_kg")
        assert body in sql


# ──────────────────────────────────────────────────────────────────────────────
# escape_cypher_literal
# ──────────────────────────────────────────────────────────────────────────────

class TestEscapeCypherLiteral:

    def test_none(self):
        assert escape_cypher_literal(None) == "null"

    def test_bool(self):
        assert escape_cypher_literal(True) == "true"
        assert escape_cypher_literal(False) == "false"

    def test_int(self):
        assert escape_cypher_literal(42) == "42"
        assert escape_cypher_literal(-7) == "-7"

    def test_float(self):
        assert escape_cypher_literal(3.14) == "3.14"

    def test_plain_string(self):
        assert escape_cypher_literal("hello") == "'hello'"

    def test_string_with_single_quote_escaped(self):
        # Single quotes get backslash-escaped for Cypher.
        out = escape_cypher_literal("it's")
        assert out == "'it\\'s'"

    def test_string_with_backslash_escaped(self):
        out = escape_cypher_literal("path\\to\\file")
        assert out == "'path\\\\to\\\\file'"

    def test_list_json(self):
        out = escape_cypher_literal(["a", "b"])
        assert out == '["a", "b"]'

    def test_dict_json(self):
        out = escape_cypher_literal({"name": "x", "kind": "class"})
        # JSON dict → Cypher map literal (close enough syntactically in AGE).
        assert "name" in out and "class" in out

    def test_unsupported_type_raises(self):
        class Custom: ...
        with pytest.raises(TypeError):
            escape_cypher_literal(Custom())

    # ── SCR finding #1 (Second Order SQL Injection) ─────────────────────────
    # Symbol names / file paths ingested from source code (a classic
    # second-order vector: attacker-controlled text stored once, then reused
    # unescaped in a later query) flow into Cypher queries built by
    # app/kg/impact_analyzer.py and app/kg/graph_retriever.py via this
    # function. These tests pin down that a value designed to break out of
    # the surrounding quoted literal is neutralised rather than terminating
    # the string and injecting Cypher syntax.

    def test_injection_payload_breaking_out_of_quotes_is_neutralised(self):
        payload = "x' OR '1'='1"
        out = escape_cypher_literal(payload)
        # The single quotes inside the payload must be escaped, not left free
        # to close the literal early — the whole payload stays inside ONE
        # pair of unescaped quotes (the literal's own open/close).
        assert out == "'x\\' OR \\'1\\'=\\'1'"
        # Sanity: exactly two unescaped (literal-delimiting) single quotes.
        import re as _re
        unescaped_quotes = _re.findall(r"(?<!\\)'", out)
        assert len(unescaped_quotes) == 2

    def test_injection_payload_with_merge_clause_stays_a_single_string_literal(self):
        # A payload attempting to close the literal and append a second
        # Cypher clause (e.g. a MERGE creating an unauthorized node).
        payload = "a' MERGE (evil:Malicious) SET evil.pwned=true //"
        out = escape_cypher_literal(payload)
        assert out.startswith("'") and out.endswith("'")
        assert "MERGE" in out  # present, but as ESCAPED text, not live syntax
        import re as _re
        unescaped_quotes = _re.findall(r"(?<!\\)'", out)
        assert len(unescaped_quotes) == 2  # only the literal's own delimiters

    def test_injection_payload_with_backslash_quote_combo_is_neutralised(self):
        # Backslash-then-quote payloads try to exploit escaping order bugs
        # (e.g. an escaper that only handles one of the two characters).
        payload = "\\' OR 1=1 --"
        out = escape_cypher_literal(payload)
        import re as _re
        unescaped_quotes = _re.findall(r"(?<!\\)'", out)
        assert len(unescaped_quotes) == 2


# ──────────────────────────────────────────────────────────────────────────────
# _decode_agtype
# ──────────────────────────────────────────────────────────────────────────────

class TestDecodeAgtype:

    def test_none(self):
        assert _decode_agtype(None) is None

    def test_native_int_passthrough(self):
        assert _decode_agtype(42) == 42

    def test_native_dict_passthrough(self):
        assert _decode_agtype({"k": 1}) == {"k": 1}

    def test_json_string(self):
        assert _decode_agtype('"hello"') == "hello"
        assert _decode_agtype("42") == 42
        assert _decode_agtype("true") is True

    def test_agtype_suffix_stripped(self):
        assert _decode_agtype('"hello"::agtype') == "hello"
        assert _decode_agtype("42::agtype") == 42

    def test_non_json_string_falls_back_to_raw(self):
        # Vertex/edge agtype payloads have structural headers that json.loads
        # can't parse; we should return the raw string rather than crash.
        raw = "{id: 123, label: \"Class\"}"
        assert _decode_agtype(raw) == raw


# ──────────────────────────────────────────────────────────────────────────────
# Schema constants
# ──────────────────────────────────────────────────────────────────────────────

class TestSchemaConstants:

    def test_node_label_count_matches_plan(self):
        # Plan §5.1 calls out 17 vertex label types.
        assert len(kg_schema.NODE_LABELS) == 17

    def test_edge_label_count_matches_plan(self):
        # Plan §5.1 calls out 15 edge label types.
        assert len(kg_schema.EDGE_LABELS) == 15

    def test_node_labels_unique(self):
        assert len(set(kg_schema.NODE_LABELS)) == len(kg_schema.NODE_LABELS)

    def test_edge_labels_unique(self):
        assert len(set(kg_schema.EDGE_LABELS)) == len(kg_schema.EDGE_LABELS)

    def test_core_node_labels_present(self):
        expected = {
            "Document", "DocChunk", "Repo", "File", "Module", "Class",
            "Function", "Endpoint", "Schema", "Service", "Feature",
            "Capability", "Requirement", "ADR", "Ticket", "Person", "Team",
        }
        assert set(kg_schema.NODE_LABELS) == expected

    def test_core_edge_labels_present(self):
        expected = {
            "CALLS", "IMPLEMENTS", "INHERITS", "IMPORTS", "EXPOSES",
            "CONSUMES", "DESCRIBES", "EXAMPLE_OF", "PART_OF", "OWNED_BY",
            "DEPENDS_ON", "DEPRECATES", "CHANGED_IN", "INTRODUCED_IN",
            "BROKEN_BY",
        }
        assert set(kg_schema.EDGE_LABELS) == expected

    def test_labels_are_valid_cypher_identifiers(self):
        import re
        ident = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
        for label in kg_schema.NODE_LABELS:
            assert ident.fullmatch(label), f"invalid vertex label: {label}"
        for label in kg_schema.EDGE_LABELS:
            assert ident.fullmatch(label), f"invalid edge label: {label}"


# ──────────────────────────────────────────────────────────────────────────────
# Config integration
# ──────────────────────────────────────────────────────────────────────────────

def test_kg_graph_name_configured():
    """Slice 19 adds `settings.kg_graph_name` with the expected default."""
    from app.core.config import settings
    assert settings.kg_graph_name == "npci_kg"


# ──────────────────────────────────────────────────────────────────────────────
# Integration-tier placeholders (skipped without live AGE)
# ──────────────────────────────────────────────────────────────────────────────

class TestIsAgeAvailableRollsBack:
    """Regression: a failed probe (e.g. `LOAD 'age'` when AGE isn't installed)
    aborts the Postgres transaction. `is_age_available` MUST roll back before
    returning False, else the poisoned transaction surfaces later as
    InFailedSqlTransaction on the next unrelated statement (the drive_run crash
    that motivated this test)."""

    def test_probe_failure_rolls_back_and_returns_false(self, monkeypatch):
        from app.kg import client as kg_client

        calls = {"rollback": 0}

        class _FakeDb:
            def rollback(self):
                calls["rollback"] += 1

        def _boom(*a, **k):
            raise RuntimeError("could not access file \"$libdir/age\"")

        monkeypatch.setattr(kg_client, "run_cypher", _boom)
        assert kg_client.is_age_available(_FakeDb()) is False
        assert calls["rollback"] == 1

    def test_probe_success_does_not_roll_back(self, monkeypatch):
        from app.kg import client as kg_client

        calls = {"rollback": 0}

        class _FakeDb:
            def rollback(self):
                calls["rollback"] += 1

        monkeypatch.setattr(kg_client, "run_cypher", lambda *a, **k: [{"v": 1}])
        assert kg_client.is_age_available(_FakeDb()) is True
        assert calls["rollback"] == 0


@pytest.mark.age
def test_is_age_available_against_live_db():
    """Smoke test the probe against a live AGE-enabled Postgres.

    Skipped automatically when AGE isn't reachable — see pytest marker
    config in `pytest.ini`. To run: `pytest -m age`.
    """
    from app.core.database import SessionLocal
    from app.kg.client import is_age_available

    db = SessionLocal()
    try:
        if not is_age_available(db):
            pytest.skip("Apache AGE not available in this environment")
        assert is_age_available(db) is True
    finally:
        db.close()
