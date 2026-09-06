# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Structured error taxonomy for exception handling across the platform.

Closes S2 (ARCHITECTURE_REVIEW_ACTIONS.md — "948 `broad except Exception`
sites: an error taxonomy with safe propagation and telemetry") and
implements EA_Skills.md P8 ("clear business vs technical error taxonomy",
"no swallowed exceptions") and security_architecture_skills.md §14.4
("Applications MUST NOT swallow errors. Errors MUST be categorized, mapped
to taxonomy, emitted with correlation IDs, safe for callers, free of
sensitive data leakage, linked to telemetry and alerting where relevant.").

Why this exists as a NEW, additive module rather than a mass rewrite
---------------------------------------------------------------------
The 948-site count is a HYGIENE BASELINE, not something a single PR should
try to zero out — the vast majority of those sites are deliberate,
documented fail-open guards on non-critical paths (telemetry, transcript
capture, dev-log writes) where the review's own "What Already Passes"
section credits the codebase for exactly this pattern ("EA P8 failure
handling: lease-guarded cooperative cancellation... crash-safe
resumption"). Rewriting all 948 in one pass would itself violate
EA_Skills.md's "smallest change that fully implements the intent"
guidance and risks turning deliberate resilience into accidental
brittleness.

The remediation strategy this module enables (see
docs/ARCHITECTURE_REVIEW_REMEDIATION.md §S2 for the full policy) is:

1. **Classify, don't eliminate.** `classify_exception()` buckets any
   caught exception into one of five categories so telemetry and alerting
   can distinguish "expected, safe to ignore" from "must page someone."
2. **New call sites use `safe_call`/`@safe`** instead of a bare
   `except Exception: pass` — these ALWAYS emit structured telemetry
   (never silently drop an exception) and let the caller declare its own
   fail-open/fail-closed policy explicitly rather than inheriting whatever
   the nearest `except Exception` happened to do.
3. **The hygiene ratchet holds the line going forward**: CI enforcement
   (see docs/ARCHITECTURE_REVIEW_REMEDIATION.md §S2) fails a PR that
   INCREASES the bare-`except Exception`-with-no-taxonomy-reference count
   above the recorded baseline, without requiring the existing baseline to
   be fixed in the same change.
4. **High-risk sites are migrated incrementally**, prioritised by blast
   radius: partner-boundary and payment-adjacent exception handling (A2A
   middleware, HMAC verification, agentic run state transitions) move to
   `safe_call`/`ErrorCategory` FIRST; best-effort telemetry/logging sites
   (which are already correctly fail-open per policy) are lower priority.
"""
from __future__ import annotations

import functools
import logging
import re
from enum import Enum
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ErrorCategory(str, Enum):
    """The five buckets every caught exception should map to.

    Mirrors EA_Skills.md P8's "clear business vs technical error taxonomy"
    and security_architecture_skills.md §5.3's "categorized errors" /
    §6.2's `error_taxonomy` boundary-contract block (business_errors /
    technical_errors / resource_access_errors / security_errors)."""

    BUSINESS = "business"
    # A domain-rule violation the caller should surface to the user/partner
    # as-is (e.g. "unknown_task_type", "partner_inactive"). Never retried
    # blindly — retrying an identical request produces the identical error.

    TECHNICAL = "technical"
    # A bug or unexpected internal state (KeyError on a field that should
    # always exist, a type mismatch). Should be logged with full context and
    # generally should NOT be silently swallowed — these are exactly the
    # bugs a broad `except Exception: pass` hides.

    RESOURCE_ACCESS = "resource_access"
    # DB/network/filesystem/queue failure reaching a dependency. Distinct
    # from BUSINESS/TECHNICAL per EA_Skills.md P10 ("separate resource-
    # access error category... structured exception details for
    # troubleshooting"). Often transient — safe to retry with backoff,
    # subject to the caller's circuit-breaker/bulkhead policy (see
    # core/resilience.py).

    SECURITY = "security"
    # Authentication/authorization/integrity failure (signature mismatch,
    # replay detected, HMAC verification failure). MUST be emitted as
    # structured security telemetry (security_architecture_skills.md
    # §13.2) — never merely logged at INFO/DEBUG and never silently
    # swallowed, even in a "best-effort" code path.

    TELEMETRY = "telemetry"
    # A failure in code whose ENTIRE job is observability/best-effort side
    # effects (metrics emission, transcript capture, dev-log writes, usage-
    # ledger persistence). These are the sites where fail-open + swallow IS
    # the correct, deliberate policy — the exception must never propagate
    # and break the primary operation it is instrumenting. Distinguishing
    # this category from TECHNICAL is what lets the hygiene ratchet (S2)
    # tell "known-good fail-open" apart from "hidden bug" without demanding
    # every one of the 948 sites be rewritten at once.


def classify_exception(exc: BaseException) -> ErrorCategory:
    """Best-effort default classifier. Callers with more context (e.g. "this
    is a Redis client failure in a telemetry path") should pass an explicit
    `category=` to `safe_call`/`@safe` instead of relying on this — the
    classifier is a fallback for exceptions that reach `safe_call` without
    one, not a replacement for call-site knowledge."""
    name = type(exc).__name__
    if name in {"ConnectionError", "TimeoutError", "OSError", "IOError"}:
        return ErrorCategory.RESOURCE_ACCESS
    # SQLAlchemy / httpx / redis exceptions are duck-typed by module path so
    # this module has no hard dependency on those packages.
    mod = type(exc).__module__ or ""
    if any(m in mod for m in ("sqlalchemy", "httpx", "redis", "psycopg", "asyncpg")):
        return ErrorCategory.RESOURCE_ACCESS
    if name in {"PermissionError", "SignatureMismatchError"}:
        return ErrorCategory.SECURITY
    if name in {"ValueError", "TypeError", "KeyError", "AttributeError", "IndexError"}:
        return ErrorCategory.TECHNICAL
    return ErrorCategory.TECHNICAL


# Exception types whose message text is authored by US and is safe to show a
# caller: domain/validation errors raised deliberately with a human-readable
# explanation. Anything not on this list is treated as machine-generated and
# is replaced with a category label.
#
# Deliberately NOT here: SQLAlchemy/psycopg errors (embed table names, column
# names and the full SQL statement), KeyError from library internals (echoes
# attacker-supplied archive member names), OSError (absolute filesystem paths).
#
# Matched on the BARE type name, but only for types whose module passes
# `_is_first_party_module` — see `_is_client_safe_type`. Matching on the bare
# name alone was a real bypass: any third-party class that happens to be called
# `ValueError` (or is deliberately named that) inherited pass-through trust.
_CLIENT_SAFE_EXC_NAMES = frozenset({
    "ValueError",
    "UnicodeDecodeError",
    "RepoSelectionError",
    "WorkspaceError",
    "BundleError",
    "SsrfBlocked",
})

# Builtins are safe to accept from any module because the interpreter owns the
# name — `ValueError` from `builtins` cannot be spoofed by a dependency.
_CLIENT_SAFE_BUILTINS = frozenset({"ValueError", "UnicodeDecodeError"})

# Third-party exception types we accept DESPITE not authoring the message,
# because the text is a fixed, human-readable, non-sensitive library string
# (e.g. "openpyxl does not support binary format .xlsb"). Listed explicitly and
# module-qualified so the exception is auditable rather than implied.
_CLIENT_SAFE_QUALIFIED = frozenset({
    "openpyxl.utils.exceptions.InvalidFileException",
})

# Modules whose exception classes we author. A class named `ValueError` living
# in `sqlalchemy.exc` is NOT ours and must not be trusted.
_FIRST_PARTY_MODULE_PREFIXES = ("app.", "a2a_common.", "backend.app.")

# Substrings that must never appear in a client-visible detail, regardless of
# which exception produced them. This is the backstop for the wrapping case:
#   raise ValueError(f"could not save: {sqlalchemy_error}")
# is an authored ValueError whose text nonetheless carries the SQL statement.
# The type allowlist cannot catch that; scanning the rendered output can.
_LEAK_MARKERS = (
    "[sql:",
    "[parameters:",
    "psycopg2",
    "psycopg",
    "sqlalchemy",
    "asyncpg",
    "traceback (most recent call last)",
    "site-packages",
)

# SQL statements, matched on STRUCTURE rather than on the bare verb.
#
# A plain `"select "` substring is not usable as a marker. This codebase has
# authored messages like "Select at least one bank to ship to"
# (api/phase_c.py:386), and English sentences such as "cannot delete from an
# archived kit" contain the verb clauses too. Scrubbing those would replace a
# helpful validation message with a generic label — a usability regression that
# would eventually get the whole scrub reverted, taking the real protection with
# it.
#
# So a match requires BOTH halves of what makes text a query:
#   1. a verb clause  — SELECT..FROM / INSERT INTO / UPDATE..SET / DELETE FROM
#   2. a syntax signal — WHERE, VALUES, SET, RETURNING, a bind placeholder
#                        (%s, %(name)s, :name, ?), or a statement terminator
# Real leaked SQL carries both; prose essentially never does. Statements that
# somehow carry neither are still caught by `[sql:` and the driver-name markers
# above, which is how SQLAlchemy and psycopg2 actually render them.
_SQL_CLAUSE_RE = re.compile(
    r"\bselect\b.{0,4000}?\bfrom\b"
    r"|\binsert\s+into\b"
    r"|\bupdate\b.{0,4000}?\bset\b"
    r"|\bdelete\s+from\b",
    re.IGNORECASE | re.DOTALL,
)
_SQL_SIGNAL_RE = re.compile(
    r"\b(where|values|set|returning|join|order\s+by|group\s+by)\b"
    r"|select\s+\*"                       # `SELECT *` is never English prose
    r"|%\(\w+\)s|%s|:\w+\s*[,)]|\?\s*[,)]|;\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# ── Filesystem paths, credentials-in-URLs, and archive members ────────────────
#
# These markers exist for `client_safe_message`, which — unlike
# `client_safe_detail` — receives an already-rendered string of UNKNOWN origin
# (see its docstring). The type allowlist cannot be applied there, so structural
# detection is the only gate available.
#
# Each pattern below requires a STRUCTURAL signal, not a keyword, for the same
# reason the SQL rule does: a scrub that mangles ordinary prose gets reverted,
# taking the real protection with it. Specifically:
#
#   * `_ABS_PATH_RE` needs a POSIX path at least two segments deep under a
#     recognised system root, or a Windows drive/UNC path. Bare `/tmp` or a
#     sentence containing a slash does not match. This deliberately does NOT
#     match relative paths ("config/app.yaml"), which are not disclosure.
#   * `_URL_CREDENTIALS_RE` matches only the `scheme://user:pass@host` form.
#     A URL without embedded credentials is not sensitive and is left alone.
#   * `_ARCHIVE_MEMBER_RE` matches openpyxl/zipfile's fixed phrasing, which
#     reflects the UPLOADER'S OWN archive member name back to them.
_ABS_PATH_RE = re.compile(
    # POSIX: /srv/..., /app/..., /home/..., /usr/lib/..., /var/..., /etc/...
    r"(?:^|[\s'\"(\[=:])/(?:app|srv|home|root|usr|var|etc|opt|tmp|mnt|media|proc)/"
    r"[\w.\-]+(?:/[\w.\-]+)*"
    # Windows drive letter: C:\Users\... (single- or double-escaped)
    r"|[A-Za-z]:\\{1,2}[\w.\-]+(?:\\{1,2}[\w.\-]+)+"
    # UNC share: \\server\share\...
    r"|\\{2}[\w.\-]+\\[\w.\-]+",
)
_URL_CREDENTIALS_RE = re.compile(
    r"\b[a-z][a-z0-9+.\-]*://[^/\s:@]*:[^/\s@]+@",
    re.IGNORECASE,
)
_ARCHIVE_MEMBER_RE = re.compile(
    r"there is no item named|is not a zip file|bad magic number for",
    re.IGNORECASE,
)

_CATEGORY_MESSAGES = {
    ErrorCategory.RESOURCE_ACCESS: "a downstream resource was unavailable",
    ErrorCategory.SECURITY: "the request was refused by a security control",
    ErrorCategory.BUSINESS: "the request was not valid for the current state",
    ErrorCategory.TECHNICAL: "an internal processing error occurred",
}


def _is_client_safe_type(exc: BaseException) -> bool:
    """True when `exc`'s TYPE is one whose message we are willing to show.

    Checks the module as well as the name. `type(exc).__name__ in {...}` alone
    was bypassable: a class named `ValueError` defined in `sqlalchemy.exc`
    satisfied the name check and leaked the SQL statement verbatim.
    """
    t = type(exc)
    name = t.__name__
    mod = t.__module__ or ""

    if f"{mod}.{name}" in _CLIENT_SAFE_QUALIFIED:
        return True
    if name not in _CLIENT_SAFE_EXC_NAMES:
        return False
    # Builtins: the interpreter owns the name, so the module check is satisfied
    # by definition.
    if mod == "builtins" and name in _CLIENT_SAFE_BUILTINS:
        return True
    # Our own domain errors, wherever they are defined in the app package.
    if mod.startswith(_FIRST_PARTY_MODULE_PREFIXES):
        return True
    # Subclasses of a builtin we trust, defined in our own code, are covered by
    # the prefix check above. Anything else (a dependency's class that merely
    # shares a name) is not trusted.
    return False


def _contains_leak_marker(text: str) -> bool:
    """True when `text` contains machine-generated internals.

    Applied to the rendered output even for allowlisted types, so an authored
    message that INTERPOLATES a library error is still caught.
    """
    low = text.lower()
    if any(marker in low for marker in _LEAK_MARKERS):
        return True
    return bool(_SQL_CLAUSE_RE.search(text) and _SQL_SIGNAL_RE.search(text))


def _contains_environment_disclosure(text: str) -> bool:
    """True when `text` discloses filesystem layout, embedded credentials, or an
    uploader-supplied archive member name.

    Split out from `_contains_leak_marker` deliberately. It is applied by
    `client_safe_message` — which handles rendered strings of unknown origin —
    but NOT by `client_safe_detail`, whose type allowlist already establishes
    that we authored the message. An authored validation error is allowed to
    name a path the operator needs ("config/app.yaml is malformed"); a string
    arriving from an arbitrary worker exception is not.
    """
    if _URL_CREDENTIALS_RE.search(text):
        return True
    if _ARCHIVE_MEMBER_RE.search(text):
        return True
    return bool(_ABS_PATH_RE.search(text))


def client_safe_detail(exc: BaseException, *, fallback: str | None = None) -> str:
    """Return text describing `exc` that is safe to put in an HTTP response body.

    Checkmarx "Information Exposure Through an Error Message" (SCR #6) fires on
    `HTTPException(500, detail=f"...: {exc}")`. Most of those results are false
    positives — the detail goes to `logger.*`, which is what the finding's own
    recommendation asks for. A handful were genuine: a broad `except Exception`
    echoing `str(exc)` straight into the response body.

    That matters because `str(exc)` on the exceptions those handlers actually
    catch is not a tidy sentence. A SQLAlchemy commit failure renders as::

        (psycopg2.errors.ForeignKeyViolation) update or delete on table
        "change_requests" violates foreign key constraint ...
        [SQL: DELETE FROM change_requests WHERE id = %(id)s]

    — table names, column names and the SQL statement, handed to the caller.
    `openpyxl` on a malformed upload raises `KeyError` whose text quotes the
    attacker's own archive member name straight back.

    Policy, applied in order:

    1. The exception TYPE must be one we author (`_is_client_safe_type` — the
       check is module-qualified, so a dependency's class that merely shares a
       name with one of ours is not trusted).
    2. The rendered text must not contain machine-generated internals
       (`_contains_leak_marker`). This second gate exists because an authored
       exception can still carry a library message::

           raise ValueError(f"could not save section: {sqlalchemy_error}")

       That is a genuine `ValueError` we raised, so the type check passes, but
       its text contains the SQL statement. Scanning the output catches it.

    Anything failing either gate is replaced with a fixed category label. Full
    detail always stays available to operators via `logger.exception(...)` at
    the call site.

    Note this is NOT the global 500 handler's job: `main.py` sanitises
    *unhandled* exceptions, but an explicitly raised `HTTPException` bypasses
    that handler entirely and its `detail` is returned verbatim.
    """
    if _is_client_safe_type(exc):
        text = str(exc).strip()
        if text and not _contains_leak_marker(text):
            return text
    if fallback:
        return fallback
    return _CATEGORY_MESSAGES.get(classify_exception(exc),
                                  "an internal processing error occurred")


def client_safe_message(text: str | None, *, fallback: str = "an internal processing error occurred") -> str:
    """Scrub an already-rendered error STRING before it is stored somewhere a
    client can read it.

    The sibling of `client_safe_detail`, for the store-then-serve shape rather
    than the raise-immediately shape::

        except Exception as exc:
            job_registry.fail_job(db, job_id, error=str(exc))
                -> AgentJob.error_message  (DB column)
                    -> AgentJob.to_dict()  -> GET /api/jobs/{id}
                        -> rendered in the UI

    By the time the text reaches the persistence layer the exception object is
    gone, so the type-based allowlist cannot be applied — only the rendered
    string is available. This keeps human-written progress text ("Indexing
    failed", "bank has not declared this case ready") intact while replacing
    anything carrying SQL statements, driver names or tracebacks.

    Used as a chokepoint inside `job_registry.fail_job` so every one of its
    ~20 call sites is covered without touching each one.

    Scrubs MORE than `client_safe_detail` does, and the asymmetry is
    intentional. There, an allowlisted type proves we authored the text, so a
    path in the message is a deliberate operator hint. Here there is no such
    proof — the string may have come from any worker exception — so filesystem
    layout, embedded URL credentials and uploader-supplied archive member names
    are scrubbed too (`_contains_environment_disclosure`).
    """
    if not text:
        return fallback
    stripped = text.strip()
    if not stripped:
        return fallback
    if _contains_leak_marker(stripped):
        return fallback
    if _contains_environment_disclosure(stripped):
        return fallback
    return stripped


def safe_call(
    fn: Callable[..., T],
    *args: Any,
    category: ErrorCategory | None = None,
    default: T | None = None,
    reraise: bool = False,
    context: str = "",
    correlation_id: str | None = None,
    **kwargs: Any,
) -> T | None:
    """Call `fn(*args, **kwargs)`, catching `Exception` with STRUCTURED
    telemetry instead of a bare `except Exception: pass`.

    This is the preferred replacement for new "best-effort, must never
    break the caller" call sites (the pattern already used ~948 times in
    this codebase, each with its own inline comment explaining why it's
    safe). `safe_call` makes that reasoning machine-checkable:

    - `category` should be set explicitly by the caller when known — a
      telemetry-capture call site should pass `ErrorCategory.TELEMETRY`;
      a DB read should pass `ErrorCategory.RESOURCE_ACCESS`. Omitted,
      falls back to `classify_exception()`.
    - `reraise=True` re-raises AFTER logging — use this for TECHNICAL/
      SECURITY category failures where swallowing would hide a real bug or
      attack; the structured log line is emitted either way so even a
      re-raised exception has a searchable trail (security_architecture_
      skills.md §14.4 "safe for callers... linked to telemetry").
    - Every invocation logs one structured line
      (`ERROR_TAXONOMY category=... context=... correlation_id=... error=...`)
      so a hygiene-ratchet-style CI check or a log-based alert can find
      every use of this helper and audit its category distribution over
      time — the auditability EA_Skills.md P9 and security §13 require.

    Never logs the exception's arguments or return value (only the
    exception's type/message) — avoiding the "large/sensitive payload"
    logging anti-pattern both skill files flag.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 — this IS the taxonomy chokepoint;
        # catching broadly here is the point, provided it always logs.
        cat = category or classify_exception(e)
        level = logging.ERROR if cat in (ErrorCategory.SECURITY, ErrorCategory.TECHNICAL) else logging.WARNING
        logger.log(
            level,
            "ERROR_TAXONOMY category=%s context=%s correlation_id=%s "
            "error_type=%s error=%s",
            cat.value, context or fn.__name__, correlation_id or "-",
            type(e).__name__, str(e)[:500],
        )
        if reraise:
            raise
        return default


def safe(
    category: ErrorCategory | None = None,
    default: Any = None,
    reraise: bool = False,
    context: str = "",
):
    """Decorator form of `safe_call` for wrapping a whole function body.

    Example::

        @safe(category=ErrorCategory.TELEMETRY, context="usage_ledger_write")
        def _persist_usage_row(trace):
            ...

    Prefer `safe_call` for a single risky call inside an otherwise-normal
    function (the existing codebase style — a `try/except` wraps ONE
    operation with surrounding logic outside it); use `@safe` only when the
    ENTIRE function body is the fail-open unit, matching how `_persist_usage_row`
    and similar "whole function is best-effort" helpers are already structured.
    """
    def decorator(fn: Callable[..., T]) -> Callable[..., T | None]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T | None:
            return safe_call(fn, *args, category=category, default=default,
                             reraise=reraise, context=context or fn.__name__, **kwargs)
        return wrapper
    return decorator
