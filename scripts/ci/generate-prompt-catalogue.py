#!/usr/bin/env python3
# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Generate docs/SystemPrompts.md from the prompt files — Phase 5.

The catalogue used to be maintained by hand, reproducing each prompt verbatim.
That is a duplicate of the source of truth, so it rots the first time a prompt
changes, and it had already corrupted its own structure: prompt bodies contain
`##` headings, so pasting them raw turned "## 5. Compliance Analysis" from
inside a prompt into a document section. Generated bodies are fenced instead.

Coverage is deliberately two-tier, because generating from files can only see
file-backed prompts:

  * FULL TEXT for prompts under app/prompts/ (69 today)
  * AN INDEX ONLY for prompts still built inline in Python — no body, but the
    reader learns they exist and where. The hand-written version claimed to
    catalogue everything, which is exactly the promise that made it rot.

Usage:  python scripts/ci/generate-prompt-catalogue.py [--check]
        --check exits 1 if the committed catalogue is out of date.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
APP = BACKEND / "app"
PROMPTS = APP / "prompts"
OUT = ROOT / "docs" / "SystemPrompts.md"

BANNER = """<!-- GENERATED FILE — DO NOT EDIT BY HAND.
     Regenerate:  python scripts/ci/generate-prompt-catalogue.py
     Source of truth is backend/app/prompts/**.md and the code that loads it.
     Edits here are overwritten and, worse, silently disagree with what the
     platform actually sends the model. -->
"""


def _literal_size(node) -> int:
    """Literal chars in a Constant / f-string / `+`-concatenated string tree."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return len(node.value)
    if isinstance(node, ast.JoinedStr):
        return sum(len(p.value) for p in node.values
                   if isinstance(p, ast.Constant) and isinstance(p.value, str))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_size(node.left) + _literal_size(node.right)
    return 0


def call_sites() -> dict[str, list[str]]:
    """prompt-path -> ["module:CONSTANT", ...] by scanning load/render calls."""
    sites: dict[str, list[str]] = {}
    for path in sorted(APP.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        module = ".".join(path.relative_to(APP.parent).with_suffix("").parts)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in {"load_prompt", "render_prompt"}:
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            name = node.args[0].value
            owner = module
            # Walk up for the assigned constant name, if any.
            for anc in ast.walk(tree):
                if isinstance(anc, ast.Assign) and anc.value is node:
                    tgt = anc.targets[0]
                    if isinstance(tgt, ast.Name):
                        owner = f"{module}:{tgt.id}"
            sites.setdefault(name, []).append(owner)
    return sites


def inline_prompts() -> list[tuple[str, str, int]]:
    """(module, constant-or-function, chars) for prompts STILL in Python."""
    out: list[tuple[str, str, int]] = []
    for path in sorted(APP.rglob("*.py")):
        if path.is_relative_to(PROMPTS):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        module = ".".join(path.relative_to(APP.parent).with_suffix("").parts)
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            tgt = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if not isinstance(tgt, ast.Name):
                continue
            # Counts Constant, f-string AND `+`-concatenated prompts. The last
            # kind was invisible to the first version of this script and to the
            # snapshot test, which is how 47 prompts (~89K chars) went unlisted.
            size = _literal_size(node.value)
            if size >= 200:
                out.append((module, tgt.id, size))
    return out


def build() -> str:
    sites = call_sites()
    files = sorted(p for p in PROMPTS.rglob("*.md") if p.name != "README.md")

    lines = [BANNER, "# LLM system-prompt catalogue\n",
             "Every prompt the platform sends, generated from the prompt files "
             "themselves so it cannot drift from what actually ships.\n"]

    lines.append(f"\n**{len(files)} file-backed prompts** "
                 f"({sum(len(p.read_text(encoding='utf-8')) for p in files):,} chars). "
                 "Bodies are fenced verbatim below.\n")

    lines.append("\n## Index\n")
    lines.append("| Prompt file | Loaded by | Chars |")
    lines.append("|---|---|---:|")
    for p in files:
        rel = p.relative_to(PROMPTS).as_posix()
        owners = ", ".join(f"`{o}`" for o in sorted(set(sites.get(rel, [])))) or "—"
        lines.append(f"| [`{rel}`](../backend/app/prompts/{rel}) | {owners} | "
                     f"{len(p.read_text(encoding='utf-8')):,} |")

    lines.append("\n---\n\n## Prompts\n")
    for p in files:
        rel = p.relative_to(PROMPTS).as_posix()
        owners = ", ".join(f"`{o}`" for o in sorted(set(sites.get(rel, [])))) or "_unreferenced_"
        body = p.read_text(encoding="utf-8").rstrip("\n")
        # Fence must outlast any backtick run inside the prompt body.
        fence = "`" * max(3, (max((len(m) for m in re.findall(r"`+", body)), default=0) + 1))
        lines.append(f"\n### `{rel}`\n")
        lines.append(f"Loaded by {owners}.\n")
        lines.append(f"{fence}text\n{body}\n{fence}\n")

    inline = inline_prompts()
    lines.append("\n---\n\n## Still inline in Python (index only)\n")
    lines.append(
        "These are prompt-sized strings that have NOT been externalised. Bodies "
        "are not reproduced — a copy here would rot exactly like the hand-written "
        "catalogue this replaces. Read them at the source.\n"
    )
    if inline:
        lines.append("\n| Module | Constant | Chars |")
        lines.append("|---|---|---:|")
        for module, name, size in sorted(inline, key=lambda r: -r[2]):
            lines.append(f"| `{module}` | `{name}` | {size:,} |")
    else:
        lines.append("\n_None._\n")

    lines.append(
        "\nPrompts assembled inside functions (roughly 467 sites across 161 "
        "files) are out of scope by design — see "
        "`docs/PROMPT_EXTERNALIZATION_PLAN.md` §4.\n"
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    text = build()
    if "--check" in sys.argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print("docs/SystemPrompts.md is stale — regenerate it "
                  "(python scripts/ci/generate-prompt-catalogue.py)")
            sys.exit(1)
        print("prompt catalogue up to date")
        return
    OUT.write_text(text, encoding="utf-8")
    n = len(list(p for p in PROMPTS.rglob("*.md") if p.name != "README.md"))
    print(f"wrote {OUT.relative_to(ROOT)} ({n} file-backed prompts)")


if __name__ == "__main__":
    main()
