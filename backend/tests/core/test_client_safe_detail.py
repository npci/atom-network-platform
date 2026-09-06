# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Regression cover for `client_safe_detail` — SCR #6 (Information Exposure
Through an Error Message).

The Checkmarx retest reported 261 results for this query. The overwhelming
majority are false positives: the exception detail goes to `logger.*`, which is
precisely what the finding's own recommendation asks for. A small number were
genuine — a broad `except Exception` echoing `str(exc)` into an HTTP response
body, where the exception is machine-generated and quotes internals back to the
caller.

These tests pin the distinction so the fix cannot silently regress:

  * messages WE authored (domain/validation errors) still reach the caller,
    because degrading them to "an error occurred" would be a usability
    regression that someone would later revert;
  * machine-generated messages (SQL text, archive member names, filesystem
    paths) never do.
"""
import pytest

from app.core.error_taxonomy import client_safe_detail, client_safe_message


def _first_party(name, base=Exception, module="app.agents.repo_scope"):
    """Build an exception class with the IDENTITY of one of ours.

    The allowlist is module-qualified: a class is trusted only when its
    `__module__` is one we own. Declaring `class RepoSelectionError(ValueError)`
    inside this test file would give it `__module__ == "tests.core...."`, which
    is correctly NOT trusted — so the stand-ins have to carry the real module
    path to exercise the pass-through branch.
    """
    cls = type(name, (base,), {})
    cls.__module__ = module
    return cls


RepoSelectionError = _first_party("RepoSelectionError", ValueError, "app.agents.repo_scope")
WorkspaceError = _first_party("WorkspaceError", RuntimeError, "app.agents.workspace_local")
SsrfBlocked = _first_party("SsrfBlocked", Exception, "app.core.ssrf_guard")


# ── messages we author: must pass through, so the fix stays acceptable ───────

@pytest.mark.parametrize("exc,expected", [
    (ValueError("Section 'Overview' not found in document"),
     "Section 'Overview' not found in document"),
    (RepoSelectionError("repo 42 is not in your selection"),
     "repo 42 is not in your selection"),
    (WorkspaceError("workspace is locked by another run"),
     "workspace is locked by another run"),
    (SsrfBlocked("resolves to a private address"),
     "resolves to a private address"),
])
def test_authored_messages_pass_through(exc, expected):
    assert client_safe_detail(exc) == expected


def test_real_first_party_exception_classes_are_trusted():
    """Guards the allowlist against drift: if one of these classes is moved to a
    different package, the module-prefix check must be updated with it. Importing
    the real classes here is what makes that failure visible.

    `repo_scope`/`governance_bundle` pull in the ORM (and therefore a DB driver)
    at import time, so those two are skipped where the driver is absent rather
    than reported as a policy failure. `ssrf_guard` has no such dependency and
    always runs, so the module-prefix rule is never left completely uncovered.
    """
    from app.core.ssrf_guard import SsrfBlocked as RealSsrfBlocked
    # Real signature is (url, reason) and __str__ renders only `reason`, so the
    # URL is not part of the client-visible text. Asserting both halves keeps
    # that property pinned: if someone later makes __str__ include the URL, the
    # target host would start appearing in response bodies.
    blocked = RealSsrfBlocked("http://169.254.169.254/latest/meta-data", "resolves to a private address")
    assert client_safe_detail(blocked) == "resolves to a private address"
    assert "169.254.169.254" not in client_safe_detail(blocked)

    # Catch ImportError as a family, not just ModuleNotFoundError for the named
    # module: this chain also reaches `pydantic[email]`, which re-raises its own
    # missing dependency as a plain ImportError naming a different package. Which
    # one surfaces depends on what earlier tests have already imported, so
    # `pytest.importorskip` gives an order-dependent result here.
    try:
        from app.agents.governance_bundle import BundleError
        from app.agents.repo_scope import RepoSelectionError
    except ImportError as exc:
        pytest.skip(f"ORM-backed modules need a dependency not installed here: {exc}")

    assert client_safe_detail(RepoSelectionError("repo 7 not selected")) == "repo 7 not selected"
    assert client_safe_detail(BundleError("bundle missing SKILL.md")) == "bundle missing SKILL.md"


# ── machine-generated messages: must NOT reach the caller ────────────────────

def _sqlalchemy_error(msg):
    """SQLAlchemy is duck-typed by module path in the taxonomy, so a stand-in
    with the right __module__ exercises the same branch without the import."""
    cls = type("IntegrityError", (Exception,), {})
    cls.__module__ = "sqlalchemy.exc"
    return cls(msg)


def test_sql_statement_and_schema_never_leak():
    exc = _sqlalchemy_error(
        '(psycopg2.errors.ForeignKeyViolation) update or delete on table '
        '"change_requests" violates foreign key constraint '
        '"agent_jobs_change_request_id_fkey" on table "agent_jobs"\n'
        '[SQL: DELETE FROM change_requests WHERE id = %(id)s]'
    )
    out = client_safe_detail(exc)
    for leaked in ("change_requests", "agent_jobs", "SQL:", "DELETE FROM",
                   "psycopg2", "fkey"):
        assert leaked not in out, f"{leaked!r} leaked into client detail: {out!r}"


def test_uploaded_archive_member_name_never_reflected():
    """openpyxl raises a bare KeyError for a zip that is not a workbook, and its
    text quotes the uploader's own archive member name — attacker-controlled
    content reflected straight back into a 415 response."""
    exc = KeyError('"There is no item named \'evil<script>.xml\' in the archive"')
    out = client_safe_detail(exc)
    assert "evil" not in out
    assert "<script>" not in out
    assert out == "an internal processing error occurred"


def test_filesystem_paths_never_leak():
    exc = OSError("[Errno 2] No such file or directory: '/srv/app/secrets/kek.pem'")
    out = client_safe_detail(exc)
    assert "/srv" not in out and "kek.pem" not in out


def test_fallback_is_used_when_supplied():
    exc = _sqlalchemy_error("[SQL: SELECT * FROM users]")
    assert client_safe_detail(exc, fallback="could not load the record") == \
        "could not load the record"
    assert "SQL" not in client_safe_detail(exc, fallback="could not load the record")


def test_empty_authored_message_falls_back_to_category():
    """An authored exception with no text must not produce an empty detail."""
    out = client_safe_detail(ValueError(""))
    assert out.strip()
    assert out == "an internal processing error occurred"


def test_return_value_is_always_a_nonempty_string():
    for exc in (ValueError("x"), KeyError("y"), OSError("z"),
                RuntimeError(""), _sqlalchemy_error("q")):
        out = client_safe_detail(exc)
        assert isinstance(out, str) and out.strip()


# ── allowlist bypasses found in adversarial review ───────────────────────────

def test_foreign_class_named_like_an_allowlisted_one_is_not_trusted():
    """The allowlist used to match on the bare type NAME.

    That meant any class called `ValueError` was trusted no matter where it came
    from — so a dependency (or an attacker-influenced code path) could hand back
    a `sqlalchemy.exc.ValueError` and have its SQL text echoed verbatim. The
    check is now module-qualified.
    """
    cls = type("ValueError", (Exception,), {})
    cls.__module__ = "sqlalchemy.exc"
    out = client_safe_detail(cls('[SQL: DELETE FROM change_requests] psycopg2 fkey'))
    for leaked in ("SQL:", "DELETE FROM", "change_requests", "psycopg2", "fkey"):
        assert leaked not in out, f"{leaked!r} leaked via a name-collision: {out!r}"


def test_authored_exception_wrapping_a_library_message_is_scrubbed():
    """The wrapping idiom is the realistic version of the same bypass::

        raise ValueError(f"could not save: {sqlalchemy_error}")

    The type check passes — it IS a ValueError we raised — but the text carries
    the SQL statement. The output is scanned for machine-generated markers so
    the interpolated internals are caught regardless of the wrapper's type.
    """
    try:
        try:
            raise RuntimeError("[SQL: SELECT * FROM users] psycopg2 detail")
        except RuntimeError as inner:
            raise ValueError(f"could not save section: {inner}") from inner
    except ValueError as exc:
        out = client_safe_detail(exc)

    for leaked in ("SQL:", "SELECT", "psycopg2", "users"):
        assert leaked not in out, f"{leaked!r} leaked through a wrapper: {out!r}"


@pytest.mark.parametrize("text", [
    "insert into tbl_bank values (1)",
    "Traceback (most recent call last):\n  File x",
    "/usr/lib/python3/site-packages/sqlalchemy/engine.py",
    "[parameters: {'id': 'abc'}]",
])
def test_leak_markers_are_matched_case_insensitively(text):
    assert client_safe_detail(ValueError(text)) == "an internal processing error occurred"


@pytest.mark.parametrize("statement", [
    "SELECT id, name FROM tbl_bank WHERE code = %(code)s",
    "select * from users",
    "UPDATE change_requests SET status = 'x' WHERE id = 1",
    "DELETE FROM tbl_psp_subset WHERE id = %(id)s",
    "INSERT INTO audit_log (a, b) VALUES (1, 2)",
    "[SQL: SELECT\n  a\nFROM b]",          # statements arrive multi-line
])
def test_sql_statements_are_scrubbed_whatever_the_verb(statement):
    assert client_safe_detail(ValueError(statement)) == "an internal processing error occurred"


@pytest.mark.parametrize("prose", [
    # api/phase_c.py:386 verbatim — a real authored message in this codebase.
    "Select at least one bank to ship to",
    "Please select a repository before running the agent",
    "Update the change request before submitting it",
    "Cannot delete from an archived kit",
    "No rows selected from the uploaded workbook",
])
def test_authored_prose_containing_a_sql_verb_is_not_scrubbed(prose):
    """The scrub must key on statement STRUCTURE, not on the bare verb.

    A plain "select " substring match would replace these helpful validation
    messages with a generic label. That reads as a bug to whoever hits it, and a
    scrub that damages normal messages is a scrub that eventually gets reverted
    — taking the real protection with it.
    """
    assert client_safe_detail(ValueError(prose)) == prose
    assert client_safe_message(prose) == prose


# ── client_safe_message: the store-then-serve sibling ────────────────────────

def test_client_safe_message_keeps_human_progress_text():
    """Operator/UI-facing status text must survive — scrubbing it would make
    every failed job read "an internal processing error occurred"."""
    for keep in ("Indexing failed", "bank has not declared this case ready",
                 "Workbook generation failed: no sheets found"):
        assert client_safe_message(keep) == keep


def test_client_safe_message_scrubs_sql_and_tracebacks():
    leaky = ('(psycopg2.errors.ForeignKeyViolation) update or delete on table '
             '"change_requests" ... [SQL: DELETE FROM change_requests]')
    out = client_safe_message(leaky)
    for leaked in ("SQL:", "DELETE FROM", "psycopg2", "change_requests"):
        assert leaked not in out
    assert out == "an internal processing error occurred"


def test_client_safe_message_handles_empty_and_none():
    assert client_safe_message(None).strip()
    assert client_safe_message("").strip()
    assert client_safe_message("   ").strip()


def test_client_safe_message_respects_custom_fallback():
    assert client_safe_message("[SQL: x]", fallback="indexing failed") == "indexing failed"
