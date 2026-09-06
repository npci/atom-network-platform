# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Prompt loader — prompts live as files, not as Python string literals.

Phase 0 of `docs/PROMPT_EXTERNALIZATION_PLAN.md`. Modelled on the mechanism the
Excel testcase engine has used since it shipped
(`excel_testcase_engine/agents/_runtime.load_prompt`), promoted to core as the
go-forward loader for everything else.

The engine has NOT been switched over, deliberately. Its seven prompt files all
end with a trailing newline and its loader does not strip one, so pointing it at
this loader would change all seven prompts by one byte — invalidating a prompt
cache they explicitly opt into (`SystemBlock(..., cache=True)`) for no benefit.
Consolidating it is a follow-up that should be its own decision, not a side
effect of adding this file.

Prompts are repo-versioned FILES, never database rows or user-editable content.
A prompt loaded from a writable store is an injection surface: whoever can edit
the row can rewrite the model's instructions for every future run. Keeping them
in git also means a prompt change is a reviewable diff with an author, which is
the property the hand-maintained catalogue in `docs/SystemPrompts.md` was trying
to approximate.

THE TRAILING-NEWLINE RULE, which matters more than it looks:

    A Python constant written as `X = \"\"\"...text\"\"\"` does NOT end with a
    newline. A text file almost always does — editors add it, and most lint
    setups require it. Loading such a file verbatim would append one byte to
    every migrated prompt.

    One byte is not cosmetic here. `core/prompt_blocks.segments_for_anthropic_cache`
    marks system-prompt segments with `cache_control`, and Anthropic's prompt
    cache keys on the EXACT prefix bytes. A stray newline silently misses the
    cache on every request: no error, no test failure, just a quieter bill that
    got louder.

    So: files end with a trailing newline (well-formed on disk) and the loader
    strips exactly one. A prompt that genuinely must end in a blank line gets
    two newlines in the file.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

# backend/app — prompts live at backend/app/prompts/<area>/<name>.md
APP_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = APP_ROOT / "prompts"


class PromptNotFoundError(FileNotFoundError):
    """Raised when a named prompt file does not exist.

    Loud on purpose. The alternative — returning "" — sends the model a request
    with no system prompt, which does not raise anywhere and produces plausible
    but ungrounded output. That is the single worst failure mode available to a
    prompt loader.
    """


@lru_cache(maxsize=None)
def load_prompt(name: str, *, base: Path | None = None) -> str:
    """Return the text of prompt `name` (e.g. "agents/xsd/assessment.md").

    Cached: prompts are immutable for the life of the process, and several are
    read while building a system prompt on every request.

    `base` resolves at CALL time, not as a default argument. A default of
    `base=PROMPTS_DIR` binds the value when this module is imported, so
    monkeypatching `PROMPTS_DIR` afterwards has no effect and the loader cannot
    be pointed at a fixture directory.
    """
    base = base or PROMPTS_DIR
    path = (base / name).resolve()
    # Containment check: `name` comes from our own call sites today, but a
    # loader that will eventually take a pack- or config-supplied name should
    # not be the thing that turns "../../etc/passwd" into a system prompt.
    if not path.is_relative_to(base.resolve()):
        raise PromptNotFoundError(f"prompt name escapes the prompt root: {name!r}")
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PromptNotFoundError(
            f"no prompt file at {path} (looked up as {name!r})"
        ) from None
    return text[:-1] if text.endswith("\n") else text


# ── Composed prompts (Phase 3) ───────────────────────────────────────────────
#
# Placeholder syntax is `{{NAME}}`, NOT str.format's `{name}`. Prompt bodies are
# full of literal braces — JSON schemas, code examples — so `.format` would
# require escaping every one of them as `{{`/`}}`, changing the text and
# guaranteeing a mistake.
#
# `<<NAME>>` was the first choice and is WRONG here: the code-change protocol
# already speaks it (`<<FILE:`, `<<END_FILE>>`, `<<PROMPT_READY>>` — 16 live
# occurrences), so a renderer using it could substitute or reject real protocol
# markers. `{{NAME}}` occurs nowhere in the corpus, and JSON's incidental `}}`
# cannot match because the pattern demands an uppercase identifier between.
_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")


class PromptRenderError(RuntimeError):
    """A template's variable contract was not met.

    Deliberately strict in BOTH directions. An f-string fails loudly on a typo;
    a template silently ships `{{HARD_RULES}}` to the model as literal text,
    which reads as a bizarre instruction and produces confidently wrong output
    with nothing in the logs. Missing AND unexpected names both raise.
    """


def render_prompt(name: str, /, *, base: Path | None = None, **values: str) -> str:
    """Load prompt `name` and substitute its `{{NAME}}` placeholders.

    The contract is exact: every placeholder in the template must be supplied,
    and every supplied name must appear in the template. The second half is what
    catches a renamed placeholder — otherwise the template keeps its old marker,
    the caller passes the new name, and the prompt ships with a hole.

    `base` is keyword-only and lowercase; placeholder names are uppercase, so it
    cannot collide with a substitution.
    """
    template = load_prompt(name, base=base)
    required = set(_PLACEHOLDER_RE.findall(template))
    supplied = set(values)

    if missing := required - supplied:
        raise PromptRenderError(
            f"{name}: template needs {sorted(missing)} but they were not passed"
        )
    if unexpected := supplied - required:
        raise PromptRenderError(
            f"{name}: passed {sorted(unexpected)}, which the template does not "
            f"use (renamed placeholder?). Template expects {sorted(required)}"
        )

    out = _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)

    # A substituted VALUE could itself contain a placeholder. Nothing in the
    # corpus does today, and if that ever changes it must be a loud failure
    # rather than a literal `{{X}}` reaching the model.
    if leftover := set(_PLACEHOLDER_RE.findall(out)):
        raise PromptRenderError(
            f"{name}: {sorted(leftover)} survived rendering — a substituted "
            f"value contains placeholder syntax"
        )
    return out
