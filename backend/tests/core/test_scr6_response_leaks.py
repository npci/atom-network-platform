# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""SCR #6 regression cover for the leaks found by the r3 adversarial review.

Each test pins one site where machine-generated exception text used to reach an
HTTP response body. They are grouped by the SHAPE of the leak, because the shape
is what makes each one hard to see:

  * `TestStoreThenReturn`  — exception text accumulated into a container that a
    route returns hundreds of lines later. No per-handler analysis associates
    the two, which is why the original triage's AST classifier reported these
    handlers as LOG_ONLY.
  * `TestStoreThenServe`   — exception text persisted to a column, then served
    by a different endpoint, possibly much later.
  * `TestEnvironmentDisclosure` — the scrubber's own coverage gaps: filesystem
    paths, credentials embedded in URLs, uploader-supplied archive members.
  * `TestDiagnosticsPreserved` — the other direction. A scrub that destroys
    legitimate operator messaging gets reverted, taking the protection with it,
    so the useful text is pinned too.
"""
from __future__ import annotations

import ast
import errno
import logging
import pathlib
import socket
import types

import pytest

from app.core.error_taxonomy import client_safe_detail, client_safe_message

REPO = pathlib.Path(__file__).resolve().parents[3]


# --------------------------------------------------------------------------
# Realistic exception renderings, captured from the real libraries rather than
# invented, so these tests fail if the scrub stops matching what is actually
# thrown.
# --------------------------------------------------------------------------
SQLALCHEMY_FK = (
    '(psycopg2.errors.ForeignKeyViolation) update or delete on table '
    '"change_requests" violates foreign key constraint '
    '"brds_change_request_id_fkey" on table "brds"\n'
    '[SQL: DELETE FROM change_requests WHERE id = %(id)s]\n'
    "[parameters: {'id': 'abc-123'}]"
)
AGE_CREATE_VLABEL = (
    '(psycopg2.errors.UndefinedFunction) function create_vlabel(unknown, '
    'unknown) does not exist\n'
    "[SQL: SELECT create_vlabel('npci_graph', 'Document');]"
)


class _FakeSAError(Exception):
    """Stands in for a SQLAlchemy error without importing the driver."""
    __module__ = "sqlalchemy.exc"


class TestStoreThenReturn:
    """Container-accumulation leaks: append inside `except`, return far below."""

    def test_change_requests_delete_summary_is_scrubbed(self):
        """`admin_delete_change` returns {"summary": {"errors": [...]}}.

        Eleven handlers fed that list with `str(e)`. The two sibling
        `raise HTTPException` sites in the same function were fixed in an
        earlier round; the accumulator was missed.
        """
        _safe_step_error = _load_safe_step_error()

        out = _safe_step_error("commit-after-manual-deletes", _FakeSAError(SQLALCHEMY_FK))

        assert "commit-after-manual-deletes" in out, "the step label must survive"
        for secret in ("change_requests", "brds", "[SQL:", "psycopg2",
                       "DELETE FROM", "brds_change_request_id_fkey"):
            assert secret not in out, f"{secret!r} leaked into the response body"

    def test_change_requests_delete_summary_hides_server_paths(self):
        """The two `rmtree` handlers also interpolated an absolute server path
        into the LABEL, not just the exception."""
        _safe_step_error = _load_safe_step_error()

        out = _safe_step_error(
            "artifact cleanup (sessions)",
            OSError(errno.EACCES, "Permission denied",
                    "/app/artifacts/sessions/abc-123"),
        )
        assert "/app/artifacts" not in out
        assert "artifact cleanup" in out

    def test_kg_ingest_failure_never_carries_the_cypher_query(self):
        """`POST /api/kg/ingest` returns `report.failures`, which shipped 200
        characters of generated Cypher unconditionally."""
        from app.kg.ingest_from_rag import IngestPlan, execute_plan

        def boom(cypher: str) -> None:
            raise RuntimeError("connection dropped")

        cypher = ("MERGE (n:Document {source_file: '/srv/repos/acme/src/Main.java'}) "
                  "SET n.title = 'x'")
        report = execute_plan(IngestPlan(operations=[("document", cypher)]),
                              run_cypher_fn=boom)

        entry = report.failures[0]
        assert entry["kind"] == "document", "the operation kind is the useful signal"
        assert entry["cypher_excerpt"] == ""
        blob = str(entry)
        for secret in ("MERGE", "source_file", "/srv/repos", "Main.java"):
            assert secret not in blob

    def test_kg_schema_init_failure_hides_sql(self):
        """`POST /api/kg/initialise` returns `failures` as a declared field on
        KgInitResponse; the failing statement is a raw SELECT."""
        out = client_safe_detail(_FakeSAError(AGE_CREATE_VLABEL))
        for secret in ("create_vlabel", "npci_graph", "[SQL:", "psycopg2"):
            assert secret not in out

    def test_cert_sync_failure_hides_upstream_body_and_transport_detail(self):
        """`apply_diff`'s `failed` list is BOTH returned by .../apply and
        persisted into CertSimulatorSyncLog.summary, which .../log re-serves."""
        _safe_sync_failure = _load_safe_sync_failure()

        # (a) upstream response body — could be another service's stack trace
        entry = _safe_sync_failure(
            "TC_01", "add", status=500,
            upstream_body="<html>Traceback (most recent call last): "
                          "File \"/opt/certengine/app.py\", line 42</html>",
        )
        assert entry["status"] == 500, "the status code is safe and useful"
        blob = str(entry)
        for secret in ("Traceback", "/opt/certengine", "app.py"):
            assert secret not in blob

        # (b) transport exception
        entry = _safe_sync_failure(
            "TC_02", "update",
            exc=OSError(errno.ECONNREFUSED,
                        "Connection refused to certengine.internal:8443"),
        )
        assert entry["tc_id"] == "TC_02"
        assert "certengine.internal" not in str(entry)

    def test_partner_connect_hint_classifies_without_echoing(self):
        """`POST /partners/{id}/test` must keep its diagnostic value: the
        operator needs to distinguish DNS from refused from no-route. The errno
        is classified into prose we author instead of echoing the OS string."""
        hint = _load_connect_hint()

        assert "resolved" in hint(socket.gaierror(-2, "Name or service not known"))
        assert "refused" in hint(ConnectionRefusedError(errno.ECONNREFUSED,
                                                        "Connection refused"))
        assert "no route" in hint(OSError(errno.EHOSTUNREACH,
                                          "No route to host")).lower()
        # and none of them echo the OS text
        for exc in (socket.gaierror(-2, "Name or service not known"),
                    ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")):
            out = hint(exc)
            assert "Name or service not known" not in out
            assert "Connection refused" not in out


class TestStoreThenServe:
    """Persisted-then-served leaks via the job registry."""

    def test_pre_truncation_cannot_slice_the_marker_out_of_view(self):
        """`fail_job` scrubs before truncating so a marker past the cut-off is
        still caught. `excel_testcase_engine.adapters.jobs.mark_failed` used to
        pass `error[:1000]`, which defeated exactly that.

        This is the shape that bypassed it: the ONLY leak signal is the
        `[SQL: ...]` tail, sitting beyond character 1000.
        """
        prefix = ("Row validation failed for uploaded workbook. "
                  + "sheet Sheet1 cell A1 mismatch; " * 34)
        full = prefix + " [SQL: SELECT secret_col FROM internal_tbl WHERE id = %(id)s]"
        assert full.find("[SQL:") > 1000, "fixture must place the marker past the cut"

        assert client_safe_message(full) == "an internal processing error occurred"
        # ...and the pre-truncated form is what used to slip through
        assert "secret_col" not in client_safe_message(full)

    def test_mark_failed_forwards_the_whole_string(self):
        """Static guard: the adapter must not re-introduce `error[:1000]`."""
        src = (REPO / "backend/app/excel_testcase_engine/adapters/jobs.py").read_text(
            encoding="utf-8")
        assert "error=error[:1000]" not in src
        assert "error=error," in src


class TestEnvironmentDisclosure:
    """`client_safe_message` receives rendered strings of unknown origin, so it
    scrubs more than `client_safe_detail` does."""

    @pytest.mark.parametrize("text,label", [
        ("No such file or directory: /app/artifacts/sessions/a/plan.json", "posix path"),
        ("failed reading /home/deploy/keys/prod.pem", "home path"),
        ('File "/usr/lib/python3.12/zipfile.py", line 1', "interpreter path"),
        ("cannot open /etc/ssl/private/server.key", "etc path"),
        (r"Permission denied: C:\Users\svc_agent\.ssh\id_rsa", "windows path"),
        (r"\\fileserver\share\secret.docx missing", "unc path"),
        ("redis://:s3cr3tpassw0rd@cache.internal:6379/0 unreachable", "url credentials"),
        ("postgresql://agentnxt:hunter2@db:5432/npci down", "dsn credentials"),
        ("There is no item named '[Content_Types].xml' in the archive", "archive member"),
        ("File is not a zip file", "zip probe"),
    ])
    def test_scrubbed(self, text, label):
        assert client_safe_message(text) == "an internal processing error occurred", label

    def test_detail_still_allows_authored_relative_paths(self):
        """The asymmetry is deliberate: an allowlisted TYPE proves we wrote the
        message, so a path in it is an intentional operator hint."""
        assert "config/app.yaml" in client_safe_detail(
            ValueError("could not parse config/app.yaml"))


class TestDiagnosticsPreserved:
    """A scrub that mangles ordinary messages gets reverted, taking the real
    protection with it. These pin the other direction."""

    @pytest.mark.parametrize("text", [
        "Indexing failed",
        "bank has not declared this case ready",
        "Sheet1: column B must be numeric",
        "Validation failed for config/app.yaml",
        "Row 42 rejected: amount must be > 0",
        "Select at least one bank to ship to",
        "cannot delete from an archived kit",
        "ratio 3/4 exceeded",
        "and/or logic error in rule 7",
        "Timed out after 30s waiting for worker",
        "LLM returned 0 candidates after 3 retries",
    ])
    def test_legitimate_messages_survive(self, text):
        assert client_safe_message(text) == text

    def test_operators_still_learn_which_step_failed(self):
        _safe_step_error = _load_safe_step_error()
        assert _safe_step_error("AGE delete", RuntimeError("x")).startswith("AGE delete:")


def _load_fn(relpath: str, name: str, extra_globals: dict | None = None):
    """Load a single module-level function by AST, without importing its module.

    Several of the modules under test (`api/partners.py`,
    `api/change_requests.py`, `services/tc_store_sync.py`) transitively import
    the PostgreSQL driver, which is not installed on every workstation. The
    helpers being tested are pure functions with no DB dependency, so lifting
    just those out keeps this policy cover running everywhere — the same
    reasoning the existing suite applies via `pytest.importorskip`, but without
    skipping, since these assertions are the whole point of the file.
    """
    src = (REPO / relpath).read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == name)
    mod = types.ModuleType("_probe")
    mod.__dict__.update({
        "errno": errno,
        "socket": socket,
        "client_safe_detail": client_safe_detail,
        "client_safe_message": client_safe_message,
        "logger": logging.getLogger("_probe"),
        "BaseException": BaseException,
    })
    mod.__dict__.update(extra_globals or {})
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<probe>", "exec"),
         mod.__dict__)
    return getattr(mod, name)


def _load_connect_hint():
    return _load_fn("backend/app/api/partners.py", "_connect_hint")


def _load_safe_step_error():
    return _load_fn("backend/app/api/change_requests.py", "_safe_step_error")


def _load_safe_sync_failure():
    return _load_fn("backend/app/services/tc_store_sync.py", "_safe_sync_failure")
