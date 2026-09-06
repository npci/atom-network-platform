# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Every username written to a log by the LOGIN path must be masked.

Added while triaging the Checkmarx "Filtering Sensitive Logs" batch (P3, P41).
Checkmarx did not report this; it was found by reading the surrounding code.

`app/api/auth.py` defines `_mask_username()` and used it on six of the eight log
lines in the login path. The two that did not were:

  * line 445, "Login success" — so the ONE case that wrote a full username to the
    log file was a *successful* login, i.e. the common case, on every sign-in;
  * line 346, the LDAP "no local account" denial.

Both now mask. This test pins that, and it is deliberately written as a SOURCE
check rather than a behavioural one: the property being defended is "no line in
this region logs a bare username", which is a statement about all present and
future lines, not about one call. A behavioural test would only cover the branches
it happens to exercise, and would keep passing when someone adds a ninth log line
with an unmasked username.

`_mask_username` itself is also tested behaviourally below, since the source check
cannot show that the masking function actually masks.
"""
import ast
import re
from pathlib import Path

import pytest

AUTH_PY = Path(__file__).resolve().parents[2] / "app" / "api" / "auth.py"

# The login path: `login()` plus the LDAP helper it delegates to. Anything
# outside this is a different context (e.g. an authenticated user acting on their
# OWN account, where the username is not a third party's identifier).
LOGIN_FUNCTIONS = {"login", "_ldap_login", "_sync_ldap_user", "_ldap_user_or_none"}


def _source() -> str:
    return AUTH_PY.read_text(encoding="utf-8")


def test_mask_username_shortens_and_obscures():
    """The masking helper must actually hide most of the name."""
    ns: dict = {}
    tree = ast.parse(_source())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_mask_username":
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<m>", "exec"), ns)
            break
    mask = ns.get("_mask_username")
    assert mask is not None, "_mask_username not found in auth.py"

    assert mask("keyur.doshi") == "ke***"
    assert mask("admin") == "ad***"
    assert mask("bob") == "b***"          # <= 4 chars: only 1 char kept
    assert mask("") == "***"              # empty: nothing kept
    # The property that matters: the full name never survives.
    for name in ("keyur.doshi", "priya.sharma@npci.org.in", "administrator"):
        assert name not in mask(name)


def _login_path_log_calls():
    """Yield (lineno, source) for every logger.* call inside the login path."""
    tree = ast.parse(_source())
    lines = _source().split("\n")
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name not in LOGIN_FUNCTIONS:
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (isinstance(f, ast.Attribute)
                    and isinstance(f.value, ast.Name)
                    and f.value.id == "logger"):
                continue
            seg = "\n".join(lines[node.lineno - 1:(node.end_lineno or node.lineno)])
            yield node, seg


def test_login_path_has_log_calls_to_check():
    """Guard against the AST walk silently matching nothing — a test that
    asserts over an empty set passes for the wrong reason."""
    assert len(list(_login_path_log_calls())) >= 6


@pytest.mark.parametrize("case", list(_login_path_log_calls()),
                         ids=lambda c: f"line{c[0].lineno}")
def test_login_path_never_logs_a_bare_username(case):
    """No logger call in the login path may pass a raw `*.username`.

    A username must arrive via `_mask_username(...)`. This is what regressed
    before: `payload.username` was passed straight through on the success path.
    """
    node, seg = case
    if "username" not in seg:
        pytest.skip("this log call does not involve a username")

    # Any `<something>.username` NOT wrapped in _mask_username( is a violation.
    unwrapped = re.findall(r"(?<!_mask_username\()\b(\w+)\.username\b", seg)
    masked = re.findall(r"_mask_username\(\s*(\w+)\.username\s*\)", seg)
    offenders = [u for u in unwrapped if u not in masked]

    assert not offenders, (
        f"auth.py line {node.lineno} logs an unmasked username "
        f"({', '.join(o + '.username' for o in offenders)}).\n"
        f"Wrap it in _mask_username(...) like the other login-path log lines.\n"
        f"Offending call:\n{seg}"
    )
