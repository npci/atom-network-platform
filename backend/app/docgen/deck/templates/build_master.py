# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Generate `npci_master.pptx` — a reference deck exercising every layout.

Run (inside the backend container):

    python -m app.docgen.deck.templates.build_master

Outputs `npci_master.pptx` in this directory. Designers should treat
this file as the visual spec for v1; the renderer will match these
layouts exactly because both go through the same builders via
`app.docgen.deck.renderer.render`.
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.docgen.deck.fixtures import sample_embedded_payments
from app.docgen.deck.renderer import render


def main(output_path: Path | None = None) -> Path:
    out = output_path or (Path(__file__).parent / "npci_master.pptx")
    return render(sample_embedded_payments(), out)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    out = main()
    print(f"wrote {out}")
