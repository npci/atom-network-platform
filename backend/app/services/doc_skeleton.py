# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Document-skeleton extraction shared by the BRD/TSD generators.

Pulls the binding behavioural spine out of an upstream markdown document so a
downstream document (e.g. the TSD) can be bound to it rather than re-deriving
its flows. Pure stdlib — safe to import from any layer (agents, docgen, api).
"""
from __future__ import annotations


def extract_md_section(md: str, *keywords: str) -> str:
    """Return the first `## ` section (heading + body, through the next `## `)
    whose heading contains any keyword, case-insensitive. '' if not found.

    `### ` subsections are kept; only a sibling `## ` ends the section. Robust
    to heading numbering ("## 6. Functional Requirements" vs "## Functional
    Requirements").
    """
    if not md:
        return ""
    out: list[str] = []
    capturing = False
    for line in md.splitlines():
        is_h2 = line.startswith("## ")
        if is_h2:
            if capturing:
                break  # next sibling section — stop
            if any(k in line.lower() for k in keywords):
                capturing = True
                out.append(line)
            continue
        if capturing:
            out.append(line)
    return "\n".join(out).strip()


def brd_flow_skeleton(brd_md: str) -> str:
    """Pull the binding behavioural spine out of the BRD: its Functional
    Requirements and Technical Architecture Overview. Empty when neither is
    found (e.g. an uploaded BRD with non-standard headings) — callers must
    then inject nothing rather than an empty, misleading block.
    """
    frs = extract_md_section(brd_md, "functional requirement")
    arch = extract_md_section(brd_md, "technical architecture", "architecture overview")
    return "\n\n".join(p for p in (frs, arch) if p)
