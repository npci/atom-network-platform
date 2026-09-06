# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Diagram rendering for product-deck slides.

Pipeline:
    DeckSlide.diagram_text  →  graphviz `dot -Tpng`  →  PNG bytes  →  embedded image

graphviz only — mermaid was deferred (Chromium dep too heavy for the
backend image). The slide schema's `diagram_kind` is `Literal["graphviz"]`
so we never receive anything else.

Failures here are non-fatal at the renderer level — the caller catches
`DiagramRenderError` and substitutes a code-block fallback so a
malformed diagram doesn't take down the whole deck. See D4 renderer.
"""
from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)

DOT_BIN = "dot"
RENDER_TIMEOUT_S = 15


class DiagramRenderError(RuntimeError):
    """Raised when `dot` fails. Caller should fall back to a text box."""


def render_graphviz_to_png(dot_source: str) -> bytes:
    """Run `dot -Tpng` against the given source and return PNG bytes.

    Args:
        dot_source: Valid graphviz DOT syntax (`digraph G { … }`).

    Returns:
        Raw PNG bytes.

    Raises:
        DiagramRenderError: dot exited non-zero, timed out, or produced
            output that isn't a valid PNG. The exception message
            includes dot's stderr (truncated) so failures are
            diagnosable from the log alone.
    """
    if not dot_source or not dot_source.strip():
        raise DiagramRenderError("empty dot source")

    try:
        proc = subprocess.run(
            [DOT_BIN, "-Tpng"],
            input=dot_source.encode("utf-8"),
            capture_output=True,
            timeout=RENDER_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DiagramRenderError(
            f"dot timed out after {RENDER_TIMEOUT_S}s"
        ) from exc
    except FileNotFoundError as exc:
        raise DiagramRenderError(f"`{DOT_BIN}` not on PATH") from exc

    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")[:500]
        raise DiagramRenderError(f"dot exited {proc.returncode}: {stderr}")

    png = proc.stdout
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise DiagramRenderError(
            f"dot stdout is not a PNG (first 8 bytes: {png[:8]!r})"
        )
    return png
