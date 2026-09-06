# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Validation for request-supplied Phase B script paths (build + UAT).

The Build and UAT panels let an operator name WHICH script runs. That input is
untrusted, so it never reaches a shell as-is: it must resolve — symlinks
included — to a regular ``*.sh`` file inside the operator-configured allowlist
root (``PHASE_B_SCRIPT_ROOT``). Anything else is rejected here, before a
subprocess exists. With the root unset the feature is off and callers fall
back to the fixed ``PHASE_B_BUILD_SCRIPT`` exactly as before.

Callers compose the command with :func:`shlex.quote` on the resolved path, so
a filename with spaces stays one argument and no metacharacter survives to the
shell either way.
"""
from __future__ import annotations

from pathlib import Path

from app.core.config import settings

# Longer than any sane relative path under the root; bounds log lines and DB
# columns fed from this value.
_MAX_RAW_LEN = 500


class ScriptPathError(ValueError):
    """Request-supplied script path rejected. Message is safe to show the caller."""


def resolve_operator_script(raw: str | None) -> Path:
    """Resolve *raw* against PHASE_B_SCRIPT_ROOT or raise :class:`ScriptPathError`.

    Contract: root configured; path (absolute or root-relative) resolves to a
    regular ``.sh`` file strictly inside the root after symlink resolution.
    """
    root_raw = (settings.phase_b_script_root or "").strip()
    if not root_raw:
        raise ScriptPathError(
            "script_path is not enabled on this deployment — set PHASE_B_SCRIPT_ROOT "
            "to the directory holding approved scripts")
    raw = (raw or "").strip()
    if not raw:
        raise ScriptPathError("script_path is empty")
    if len(raw) > _MAX_RAW_LEN:
        raise ScriptPathError("script_path is too long")
    if "\x00" in raw or "\n" in raw or "\r" in raw:
        raise ScriptPathError("script_path contains control characters")

    root = Path(root_raw).resolve()
    if not root.is_dir():
        raise ScriptPathError("PHASE_B_SCRIPT_ROOT does not exist on this host")

    candidate = Path(raw) if Path(raw).is_absolute() else root / raw
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ScriptPathError("script_path does not exist under the script root")

    # Containment AFTER symlink resolution — a link inside the root pointing
    # outside it must not smuggle an outside file in.
    if not resolved.is_relative_to(root):
        raise ScriptPathError("script_path escapes the script root")
    if not resolved.is_file():
        raise ScriptPathError("script_path is not a regular file")
    if resolved.suffix != ".sh":
        raise ScriptPathError("script_path must name a .sh script")
    return resolved
