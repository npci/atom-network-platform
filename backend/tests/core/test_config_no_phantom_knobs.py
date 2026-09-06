# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""QUAL-7 — every knob read off the host Settings must actually exist.

`Settings` is configured `extra="ignore"`, so an env var with no matching field
is dropped in silence. Combined with the codebase's `getattr(settings, "name",
default)` idiom that produces a PHANTOM KNOB: the code reads it, an operator can
set it, and the default wins forever. Nothing raises, nothing logs, no test
fails — the knob simply does not work.

Two of these existed when this test was written:

  * `engine_test_case_cap` — the Excel engine documented it as an override
    ("Override via `engine_test_case_cap` host setting") and five of the six
    sibling knobs in the same block WERE declared. This one was missed.
  * `agentic_require_critical_decisions` — the ratify-time critical-decision
    gate was permanently on, with no way to disable it for a bisect.

This is the reason QUAL-7's suggested fix (regrouping Settings into nested
models) was NOT taken: 81 declared fields are reached through this string-keyed
idiom, and nesting turns every one into a phantom knob at once, silently.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from app.core.config import Settings

APP = Path(__file__).resolve().parents[2] / "app"

# The one module that shadows the name `settings` with a DIFFERENT object
# (app.docgen.config), so its getattr calls resolve against that model instead.
FOREIGN_SETTINGS_MODULES = {"docgen/agents/pipeline.py"}


def _host_settings_files() -> list[Path]:
    """Files that bind the name `settings` to app.core.config.settings."""
    out = []
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP).as_posix()
        if rel in FOREIGN_SETTINGS_MODULES:
            continue
        if "from app.core.config import settings" in path.read_text(errors="replace"):
            out.append(path)
    return out


def _dynamic_reads(path: Path) -> set[str]:
    """Names read as getattr(settings, "<literal>", ...) in this file."""
    names: set[str] = set()
    try:
        tree = ast.parse(path.read_text(errors="replace"))
    except SyntaxError:  # pragma: no cover - the suite would fail elsewhere
        return names
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "getattr"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Name):
            continue
        if node.args[0].id != "settings" or len(node.args) < 2:
            continue
        key = node.args[1]
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            names.add(key.value)
    return names


def test_no_phantom_settings_knobs():
    declared = set(Settings.model_fields)
    phantom: dict[str, str] = {}
    for path in _host_settings_files():
        for name in _dynamic_reads(path) - declared:
            phantom.setdefault(name, path.relative_to(APP).as_posix())

    assert not phantom, (
        "getattr(settings, ...) reads a name that Settings does not declare. "
        "Because Settings uses extra=\"ignore\", the matching env var is dropped "
        "silently and the getattr default always wins — the knob looks "
        "configurable but is not. Declare the field in app/core/config.py with "
        "the SAME default the getattr call passes (behaviour-preserving), or "
        "drop the getattr if the value was never meant to be tunable.\n  "
        + "\n  ".join(f"{k}  (read in {v})" for k, v in sorted(phantom.items()))
    )


def test_settings_env_prefix_free_names_are_unique_case_insensitively():
    """Settings is `case_sensitive=False`, so two fields differing only by case
    would collide on the same env var and one would silently shadow the other."""
    seen: dict[str, str] = {}
    clashes: list[str] = []
    for name in Settings.model_fields:
        key = name.lower()
        if key in seen and seen[key] != name:
            clashes.append(f"{seen[key]} vs {name}")
        seen[key] = name
    assert not clashes, f"case-insensitive field collisions: {clashes}"


def test_declared_fields_are_reachable_by_env_var():
    """Field names must be valid env-var identifiers.

    `.env` / compose set these as UPPERCASE names; a field whose name is not a
    plain identifier could never be populated from the environment.
    """
    bad = [n for n in Settings.model_fields if not re.fullmatch(r"[a-z][a-z0-9_]*", n)]
    assert not bad, f"fields unreachable as env vars: {bad}"
