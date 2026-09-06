# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""SCR finding #2 (Filtering Sensitive Logs).

Regression test for a real propagation bug in `log_buffer.install()`:
`RedactionFilter` used to be attached via `root.addFilter(...)`. Python's
logging module only runs a logger's OWN filters plus each HANDLER's filters as
a record bubbles up through `Logger.callHandlers` — it does not consult an
ancestor logger's filter list. Since almost every module in this codebase logs
via `logger = logging.getLogger(__name__)` (a named, non-root logger), the
redaction filter attached to `root` itself never ran for those records; only
literal `logging.warning(...)` calls against the root logger were being
scrubbed. Attaching the filter to each handler instead (this test's subject)
fixes that, since handler filters run regardless of which logger emitted the
record.
"""
import logging

from app.core import log_buffer
from app.core.log_buffer import RedactionFilter


def _capture_with_handler_filter(logger_name: str, message: str) -> str:
    """Mirror log_buffer.install()'s current wiring: filter on the handler."""
    stream_logger = logging.getLogger(logger_name)
    stream_logger.setLevel(logging.DEBUG)
    stream_logger.propagate = False  # isolate from any handlers other tests installed

    import io
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(RedactionFilter())
    stream_logger.addHandler(handler)
    try:
        stream_logger.warning(message)
    finally:
        stream_logger.removeHandler(handler)
    return buf.getvalue()


def test_handler_level_filter_redacts_named_logger_records():
    """The actual fix: a filter on the HANDLER catches records from any
    named (non-root) logger — the pattern used by ~every module in this app."""
    out = _capture_with_handler_filter("app.some.module", "API_KEY=sk-supersecretvalue123")
    assert "sk-supersecretvalue123" not in out
    assert "[REDACTED]" in out


def test_bearer_token_redacted_via_handler_filter():
    out = _capture_with_handler_filter("app.a2a_common.client", "Authorization: Bearer abc.def.ghi")
    assert "abc.def.ghi" not in out
    assert "[REDACTED]" in out


def test_logger_level_filter_does_not_reach_named_logger_records():
    """Documents WHY the filter must live on the handler: a filter attached to a
    LOGGER never runs for records emitted by a *different* logger.

    `Logger.callHandlers` walks the ancestor chain invoking each ancestor's
    HANDLERS, but only the originating logger's own filters. So the old
    `root.addFilter(...)` wiring silently skipped every `getLogger(__name__)`
    record — which is essentially all of them.

    Isolation matters here: this test asserts a secret is NOT scrubbed, so it
    must not see the real handlers. Once any earlier test imports `app.main`,
    `log_buffer.install()` has attached properly-filtered handlers to root, and
    propagation to them would (correctly) redact the value and fail this test.
    `propagate = False` pins the record to the local unfiltered handler.
    """
    import io

    named = logging.getLogger("app.some.other.module")
    saved_propagate = named.propagate
    named.propagate = False          # do not reach root's real, filtered handlers
    named.setLevel(logging.DEBUG)

    marker_filter = RedactionFilter()
    named.addFilter(marker_filter)   # old (buggy) wiring: filter on the LOGGER…

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    # …and deliberately NO filter on the handler, reproducing the pre-fix state.
    named.addHandler(handler)
    try:
        # Emitted from a DIFFERENT logger than the one holding the filter, which
        # is the situation the bug turned on.
        logging.getLogger("app.some.other.module.child").warning(
            "SECRET_TOKEN=leaked-value-here"
        )
    finally:
        named.removeHandler(handler)
        named.removeFilter(marker_filter)
        named.propagate = saved_propagate

    # The bug, pinned: an ancestor logger's filter does not scrub a descendant's
    # record. Only a handler-level filter (see the tests above) does.
    assert "leaked-value-here" in buf.getvalue()


# ──────────────────────────────────────────────────────────────────────────────
# Wiring tests — assert what install() ACTUALLY builds.
#
# The three tests above construct their own handlers, so they pass whether or
# not `install()` is wired correctly. That blind spot is exactly how the bug
# came back: the filter sat on `root` again and nothing failed. These tests
# inspect and exercise the real handlers instead.
# ──────────────────────────────────────────────────────────────────────────────

def _install_isolated(monkeypatch, tmp_path):
    """Run install() against a pristine root logger and a temp app.jsonl."""
    root = logging.getLogger()
    saved = (root.handlers[:], root.filters[:], root.level)
    root.handlers.clear()
    root.filters.clear()

    monkeypatch.setattr(log_buffer, "_LOG_DIR", tmp_path)
    monkeypatch.setattr(log_buffer, "_LOG_FILE", tmp_path / "app.jsonl")
    log_buffer.install()
    return root, saved


def _restore(root, saved):
    handlers, filters, level = saved
    root.handlers.clear()
    root.filters.clear()
    root.handlers.extend(handlers)
    root.filters.extend(filters)
    root.setLevel(level)


def test_install_puts_redaction_on_every_handler_not_on_the_root_logger(monkeypatch, tmp_path):
    """The structural guard: a RedactionFilter on `root` is the bug, so it must
    be on each HANDLER and absent from the logger's own filter list."""
    root, saved = _install_isolated(monkeypatch, tmp_path)
    try:
        assert not any(isinstance(f, RedactionFilter) for f in root.filters), \
            "RedactionFilter is back on the root LOGGER — child records bypass it"

        installed = [h for h in root.handlers
                     if isinstance(h, (log_buffer.BufferHandler, logging.StreamHandler))]
        assert installed, "install() attached no handlers"
        for h in installed:
            assert any(isinstance(f, RedactionFilter) for f in h.filters), \
                f"{type(h).__name__} has no RedactionFilter — it will leak secrets"
    finally:
        _restore(root, saved)


def test_install_redacts_a_child_logger_record_in_app_jsonl(monkeypatch, tmp_path):
    """Behavioural end-to-end: a secret logged by a `getLogger(__name__)`-style
    child logger must not reach the app.jsonl sink in the clear."""
    root, saved = _install_isolated(monkeypatch, tmp_path)
    try:
        logging.getLogger("app.services.some_partner").warning(
            "PARTNER_API_KEY=sk-must-not-appear"
        )
    finally:
        _restore(root, saved)

    written = (tmp_path / "app.jsonl").read_text(encoding="utf-8")
    assert "sk-must-not-appear" not in written
    assert "[REDACTED]" in written


# ── URL-embedded credentials, including the password-only form ───────────────
# Added while triaging the Checkmarx "Filtering Sensitive Logs" batch. The
# userinfo half of the URL pattern was `[^:/\s]+` (a PLUS), which cannot match
# an empty username and therefore left `scheme://:password@host` completely
# unredacted. That is the form this project actually deploys: every REDIS_URL in
# docker-compose.yml is `redis://:${REDIS_PASSWORD}@redis:6379/0`. So the single
# most likely credential URL to show up in a connection-error log was the one
# shape the filter did not cover. Widened to `[^:/\s]*`.

import pytest


@pytest.mark.parametrize("url,secret", [
    # The regression: empty username, password present.
    ("redis://:SuperSecretRedisPw999@redis:6379/0",        "SuperSecretRedisPw999"),
    ("postgresql://:PgSecret123@db:5432/app",              "PgSecret123"),
    ("amqp://:BrokerPw55@rabbit:5672//",                   "BrokerPw55"),
    # Forms that already worked — pinned so a future edit cannot trade one for
    # the other.
    ("redis://default:SuperSecretRedisPw999@redis:6379/0", "SuperSecretRedisPw999"),
    ("http://admin:SuperSecretPw123@internal.example/api", "SuperSecretPw123"),
    ("postgresql://u:PgSecret123@db:5432/app",             "PgSecret123"),
])
def test_url_embedded_credentials_are_redacted(url, secret):
    out = _capture_with_handler_filter(
        "app.services.connector", f"connection failed for {url}"
    )
    assert secret not in out, f"password leaked for {url!r}: {out!r}"
    assert "[REDACTED]@" in out


@pytest.mark.parametrize("text", [
    "connect to https://host.local/path?x=1",
    "see http://example.com:8080/api",
    "time is 10:30 and url http://a.b/c",
    "mailto:someone@example.org",
])
def test_url_redaction_leaves_credential_free_text_alone(text):
    """The widened pattern must not start eating ordinary URLs, ports or
    e-mail addresses — an over-broad filter destroys log usefulness and would
    be quietly reverted by the next person who needs to read a log."""
    out = _capture_with_handler_filter("app.services.connector", text)
    assert out.strip() == text, f"filter altered non-credential text: {out!r}"


# ── Scheme-relative credentials: `scheme:user:pass@host` (no `//`) ───────────
# Second pass over the same Checkmarx batch, after asking "what if the URL is
# malformed?" rather than only "what if the username is empty?".
#
# `urlparse` only fills `netloc` when the URL contains `//`. Without it the whole
# authority lands in `path`, so BOTH layers of defence missed it:
#
#   * layer 1, `_sanitize_url_for_log` in core/ssrf_guard.py, rebuilds the URL
#     from `netloc` only, and `parsed.username`/`parsed.password` are also
#     derived from `netloc` — so they were None and no redaction marker was
#     even added;
#   * layer 2, this filter, required the `//` in its pattern.
#
# It is reachable from untrusted input: `_validate_endpoint_url` gates on
# `parsed.scheme != "https"`, and `urlparse("https:u:pw@h")` DOES report scheme
# 'https', so the value passes validation and is then logged on the refusal path.
@pytest.mark.parametrize("url,secret", [
    ("https:svc:SchemeRelPw1@partner.example.com/a2a",  "SchemeRelPw1"),
    ("https:/svc:SchemeRelPw2@partner.example.com/a2a", "SchemeRelPw2"),
    ("http:admin:SchemeRelPw3@internal.example/api",    "SchemeRelPw3"),
    ("redis:default:SchemeRelPw4@redis:6379/0",         "SchemeRelPw4"),
])
def test_scheme_relative_url_credentials_are_redacted(url, secret):
    out = _capture_with_handler_filter(
        "app.services.connector", f"endpoint refused: {url}"
    )
    assert secret not in out, f"password leaked for {url!r}: {out!r}"


@pytest.mark.parametrize("text", [
    # `@` in a path AFTER a real host is legitimate and must survive.
    "probing https://api.example.com/users/@me",
    "ratio 3:1 vs 4:2",
    "note:below and a@b.c",
    'json {"k": "v"}',
])
def test_scheme_relative_pattern_leaves_ordinary_text_alone(text):
    out = _capture_with_handler_filter("app.services.connector", text)
    assert out.strip() == text, f"filter altered non-credential text: {out!r}"


# ── credentials inside an EXCEPTION TRACEBACK ────────────────────────────────
# Third pass over the Checkmarx batch. The first two passes only ever asked what
# happens to `record.msg`. `RedactionFilter` scrubbed the message and nothing
# else, so an exception's own text — a completely independent channel — went to
# the log unfiltered.
#
# This matters because `logger.exception(...)` / `exc_info=True` appears at ~153
# places in this codebase, and an exception message routinely contains the thing
# that failed. A Redis/Postgres/AMQP client raising on a bad connection string
# puts the WHOLE URL, password included, in `str(exc)`:
#
#     ConnectionError: Error connecting to redis://:PASSWORD@redis:6379/0
#
# `BufferHandler.emit` then wrote `entry["exc"]` by re-formatting `exc_info`
# itself, independently of the message, so redacting the message alone left the
# credential sitting in app.jsonl verbatim. Verified against the original code:
# the secret was present; after the fix it is not.
def _capture_exception_with_handler_filter(logger_name: str, message: str,
                                           exc: BaseException) -> str:
    """Capture message + traceback the way a real handler renders them."""
    import io
    stream_logger = logging.getLogger(logger_name)
    stream_logger.setLevel(logging.DEBUG)
    stream_logger.propagate = False

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(RedactionFilter())
    stream_logger.addHandler(handler)
    try:
        try:
            raise exc
        except type(exc):
            stream_logger.exception(message)
    finally:
        stream_logger.removeHandler(handler)
    return buf.getvalue()


@pytest.mark.parametrize("exc_message,secret", [
    ("Error connecting to redis://:TracebackPw1@redis:6379/0. Name or service not known.",
     "TracebackPw1"),
    ("could not connect to postgresql://app:TracebackPw2@db:5432/appdb",
     "TracebackPw2"),
    ("AMQP handshake failed for amqp://guest:TracebackPw3@rabbit:5672//",
     "TracebackPw3"),
    # The no-`//` form, for parity with the sanitizer fix.
    ("refused endpoint https:svc:TracebackPw4@partner.example.com/a2a",
     "TracebackPw4"),
])
def test_credentials_in_exception_traceback_are_redacted(exc_message, secret):
    out = _capture_exception_with_handler_filter(
        "app.services.connector", "connection failed", ConnectionError(exc_message)
    )
    assert secret not in out, f"credential leaked via traceback: {out!r}"


def test_traceback_is_still_diagnostically_useful_after_redaction():
    """Redaction must not throw the traceback away. An operator still needs the
    exception type and the frame list — only the credential should be gone."""
    out = _capture_exception_with_handler_filter(
        "app.services.connector", "connection failed",
        ConnectionError("Error connecting to redis://:KeepMeUseful1@redis:6379/0"),
    )
    assert "KeepMeUseful1" not in out
    assert "Traceback (most recent call last)" in out
    assert "ConnectionError" in out
    assert "[REDACTED]" in out


def test_exception_info_is_consumed_so_later_handlers_cannot_rebuild_it():
    """`RedactionFilter` must leave the record in a state where a downstream
    handler cannot re-derive the raw traceback.

    `Formatter.format` caches its rendering in `record.exc_text` and reuses it,
    so the filter fills that cache with the redacted text and clears
    `record.exc_info`. If `exc_info` survived, `BufferHandler.emit` — which
    formats the traceback itself, separately from the message — would rebuild the
    unredacted version and undo the scrub. That was the actual defect."""
    record = logging.LogRecord(
        name="app.services.connector", level=logging.ERROR,
        pathname=__file__, lineno=1, msg="connection failed", args=(),
        exc_info=None,
    )
    try:
        raise ConnectionError("Error connecting to redis://:CacheCheckPw@redis:6379/0")
    except ConnectionError:
        import sys
        record.exc_info = sys.exc_info()

    assert RedactionFilter().filter(record) is True
    assert record.exc_info is None, "exc_info must be cleared, or it gets re-formatted raw"
    assert record.exc_text is not None
    assert "CacheCheckPw" not in record.exc_text
    assert "[REDACTED]" in record.exc_text


def test_unformattable_args_are_redacted_rather_than_left_raw():
    """If `msg % args` raises (placeholder/arg count mismatch) the filter cannot
    pre-format the message. The args must still be scrubbed: logging's own error
    handler prints `Arguments: (...)` to stderr, which would otherwise carry the
    credential in clear text."""
    record = logging.LogRecord(
        name="app.services.connector", level=logging.ERROR,
        pathname=__file__, lineno=1,
        msg="connect failed %s %s",                       # two placeholders
        args=("redis://:MismatchPw1@redis:6379/0",),       # one arg -> TypeError
        exc_info=None,
    )
    assert RedactionFilter().filter(record) is True
    assert "MismatchPw1" not in repr(record.args)
    assert "MismatchPw1" not in str(record.msg)
