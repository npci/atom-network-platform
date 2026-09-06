# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""SCR finding #5 (Communication Over HTTP).

`config.py`'s `_check_http_defaults_in_production` (pydantic @model_validator,
hard-blocks production boot) and `startup_validation.py`'s
`validate_http_defaults` (soft warning, all environments) must stay in sync —
both walk a hardcoded list of "service URL" field names, and a URL setting
added to `Settings` without also being added to BOTH lists would silently ship
with no http:// protection at all, in any environment.

These tests pin the exact URL fields both from findings-investigation
(exploration turned up cert_agent_url, bank_agent_url, authority_public_url,
frontend_url [localhost-exempt CORS origin, deliberately excluded],
authority_simulator_url, ollama_url) plus the additional ones already covered
(redis_url, ainxt_base_url, grok_base_url, gemini_video_base_url), so a future
edit that drops one from either list — or lets the two lists drift apart —
fails loudly here instead of silently reducing coverage.
"""
from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"

# The fields SCR finding #5's investigation confirmed default to http://
# outbound URLs used for real network calls. frontend_url is deliberately
# excluded — it is the browser-facing CORS origin, not a server-side outbound
# call carrying sensitive data, and defaults to localhost anyway.
EXPECTED_HTTP_SENSITIVE_FIELDS = {
    "ollama_url",
    "authority_simulator_url",
    "redis_url",
    "ainxt_base_url",
    "grok_base_url",
    "gemini_video_base_url",
    "cert_agent_url",
    "bank_agent_url",
    "authority_public_url",
}


def _extract_checked_field_names(source: str, list_var_marker: str) -> set[str]:
    """Pull the field-name strings out of a `_checks = [("field", "label"), ...]`
    or `_http_checks = [...]` literal list, by walking the AST rather than
    regexing — robust to reformatting/reordering."""
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if list_var_marker not in targets:
            continue
        if not isinstance(node.value, ast.List):
            continue
        for elt in node.value.elts:
            if isinstance(elt, ast.Tuple) and elt.elts:
                first = elt.elts[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    names.add(first.value)
    return names


def test_config_validator_checks_every_expected_http_sensitive_field():
    source = (APP / "core" / "config.py").read_text(encoding="utf-8")
    checked = _extract_checked_field_names(source, "_http_checks")
    missing = EXPECTED_HTTP_SENSITIVE_FIELDS - checked
    assert not missing, (
        "config.py's _check_http_defaults_in_production no longer checks: "
        f"{sorted(missing)} — a production deploy could boot on a cleartext "
        "http:// default for these URLs with no validator catching it."
    )


def test_startup_validation_checks_every_expected_http_sensitive_field():
    source = (APP / "core" / "startup_validation.py").read_text(encoding="utf-8")
    checked = _extract_checked_field_names(source, "_checks")
    missing = EXPECTED_HTTP_SENSITIVE_FIELDS - checked
    assert not missing, (
        "startup_validation.py's validate_http_defaults no longer checks: "
        f"{sorted(missing)} — dev/UAT would silently miss the warning for "
        "these URLs, even though production is still hard-blocked."
    )


def test_config_and_startup_validation_field_lists_do_not_drift_apart():
    config_source = (APP / "core" / "config.py").read_text(encoding="utf-8")
    startup_source = (APP / "core" / "startup_validation.py").read_text(encoding="utf-8")
    config_checked = _extract_checked_field_names(config_source, "_http_checks")
    startup_checked = _extract_checked_field_names(startup_source, "_checks")
    assert config_checked == startup_checked, (
        "The production hard-block list (config.py) and the dev/UAT warning "
        f"list (startup_validation.py) have drifted apart: only in config.py="
        f"{sorted(config_checked - startup_checked)}, only in "
        f"startup_validation.py={sorted(startup_checked - config_checked)}"
    )
