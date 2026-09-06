# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Shared XSD packaging.

The XSD agent (`app/agents/xsd.py`) stores its output as markdown in
`XSD.content` — it may wrap 0, 1, or many ```xml schema blocks. Pulling the
individual `.xsd` files back out is needed in two places that MUST agree:

  * The Authority operator download (`api.change_requests`) — serves a single .xsd
    or a .zip of all schemas, and
  * the partner kit shipment (`services.change_dispatch`) — bundles the same
    schemas so the partner downloads every file, not just the first block.

Keeping the extraction here (one source of truth) stops the two surfaces from
drifting on filename detection.
"""
import re

_XML_FENCE_RE    = re.compile(r"```\s*xml\s*\n(?P<body>.*?)```", re.DOTALL | re.IGNORECASE)
_XSD_FILENAME_RE = re.compile(r"`([A-Za-z0-9._\-]+\.xsd)`")


def extract_xsd_blocks(markdown: str) -> list[tuple[str, str]]:
    """Pull out every ```xml block in `markdown`. Returns a list of
    `(filename, xml_body)` tuples in document order.

    Schema filenames typically appear in a heading like
        1. **`network-annotations.xsd`** — New schema defining ...
    so we take the last `<name>.xsd` token in the ~600 chars before each block;
    positional fallback (`xsd_part_<n>.xsd`) when the agent didn't name it.
    """
    md = markdown or ""
    out: list[tuple[str, str]] = []
    for i, m in enumerate(_XML_FENCE_RE.finditer(md), start=1):
        body = m.group("body").strip()
        prefix = md[max(0, m.start() - 600): m.start()]
        names = _XSD_FILENAME_RE.findall(prefix)
        fname = names[-1] if names else f"xsd_part_{i}.xsd"
        out.append((fname, body))
    return out
