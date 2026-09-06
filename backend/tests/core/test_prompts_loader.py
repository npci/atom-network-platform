# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""`app.core.prompts.load_prompt` — Phase 0 of the prompt-externalisation plan."""
from __future__ import annotations

import pytest

from app.core.prompts import PROMPTS_DIR, PromptNotFoundError, load_prompt


def test_strips_exactly_one_trailing_newline(tmp_path):
    """The rule the whole migration depends on.

    A Python constant `X = \"\"\"body\"\"\"` has no trailing newline; a text file
    normally does. Loading verbatim would append a byte to every migrated
    prompt and silently miss the Anthropic prompt cache on every request.
    """
    (tmp_path / "one.md").write_text("body\n", encoding="utf-8")
    (tmp_path / "two.md").write_text("body\n\n", encoding="utf-8")
    (tmp_path / "none.md").write_text("body", encoding="utf-8")

    assert load_prompt("one.md", base=tmp_path) == "body"
    # Two newlines survive as one — the escape hatch for a prompt that really
    # must end in a blank line.
    assert load_prompt("two.md", base=tmp_path) == "body\n"
    assert load_prompt("none.md", base=tmp_path) == "body"


def test_missing_prompt_raises_rather_than_returning_empty(tmp_path):
    # Returning "" would send the model a request with no system prompt: no
    # exception anywhere, plausible but ungrounded output.
    with pytest.raises(PromptNotFoundError):
        load_prompt("nope.md", base=tmp_path)


def test_name_cannot_escape_the_prompt_root(tmp_path):
    (tmp_path / "sub").mkdir()
    secret = tmp_path / "secret.md"
    secret.write_text("SECRET\n", encoding="utf-8")
    with pytest.raises(PromptNotFoundError):
        load_prompt("../secret.md", base=tmp_path / "sub")


def test_prompt_files_ship_in_the_image():
    """`.dockerignore` lists `*.md`.

    Nested prompts survive only because Docker matches that pattern against
    root-level files, not recursively. Changing it to `**/*.md` would strip
    every prompt out of the image and surface at runtime as a missing system
    prompt. This asserts the invariant so that edit fails a test instead.
    """
    assert PROMPTS_DIR.is_dir(), f"{PROMPTS_DIR} is absent from the image"
    assert (PROMPTS_DIR / "README.md").is_file(), (
        "app/prompts/README.md did not ship — .dockerignore is almost certainly "
        "excluding markdown recursively now; prompts will not load in production"
    )

    # The Excel engine ships 5 prompts this way; if they vanish, the same
    # .dockerignore change is the cause. (Was 7 until the BRD/TSD-only refactor
    # removed the flow_generator node and the orphaned corpus_miner prompt.)
    engine = PROMPTS_DIR.parent / "excel_testcase_engine" / "prompts"
    assert len(list(engine.glob("*.md"))) == 5, (
        "the Excel engine's prompt files are missing from the image"
    )


# ── render_prompt: the variable contract (Phase 3) ───────────────────────────

def _tpl(tmp_path, body: str):
    (tmp_path / "t.md").write_text(body + "\n", encoding="utf-8")
    return tmp_path


def test_render_substitutes_placeholders(tmp_path):
    from app.core import prompts as P
    base = _tpl(tmp_path, "A {{RULES}} B {{CLAUSE}}")
    P.load_prompt.cache_clear()
    assert P.render_prompt("t.md", base=base, RULES="r", CLAUSE="c") == "A r B c"


def test_render_rejects_a_missing_placeholder(tmp_path):
    from app.core import prompts as P
    base = _tpl(tmp_path, "needs {{RULES}} and {{CLAUSE}}")
    P.load_prompt.cache_clear()
    with pytest.raises(P.PromptRenderError, match="CLAUSE"):
        P.render_prompt("t.md", base=base, RULES="r")


def test_render_rejects_an_unused_name(tmp_path):
    """The renamed-placeholder trap: template keeps the old marker, caller
    passes the new name, and the prompt would ship with a literal hole."""
    from app.core import prompts as P
    base = _tpl(tmp_path, "only {{RULES}}")
    P.load_prompt.cache_clear()
    with pytest.raises(P.PromptRenderError, match="TYPO"):
        P.render_prompt("t.md", base=base, RULES="r", TYPO="x")


def test_render_rejects_a_placeholder_smuggled_in_by_a_value(tmp_path):
    from app.core import prompts as P
    base = _tpl(tmp_path, "outer {{RULES}}")
    P.load_prompt.cache_clear()
    with pytest.raises(P.PromptRenderError, match="LEAKED"):
        P.render_prompt("t.md", base=base, RULES="inner {{LEAKED}}")


def test_json_braces_are_not_placeholders(tmp_path):
    """Prompt bodies are full of JSON. `{"a": {"b": 1}}` must survive rendering
    untouched — this is why the delimiter is `{{NAME}}` and not str.format."""
    from app.core import prompts as P
    body = 'Schema: {"a": {"b": 1}} and {{RULES}}'
    base = _tpl(tmp_path, body)
    P.load_prompt.cache_clear()
    assert P.render_prompt("t.md", base=base, RULES="r") == 'Schema: {"a": {"b": 1}} and r'
