# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

from app.agents.prompt_enhancer import extract_enhanced_prompt, normalize_enhancer_response


def test_normalize_collapses_prompt_ready_questionnaire_to_one_question():
    raw = """
Prompt Ready Instruction Following Priority Order

Question 1
"Can you explain how adding purpose code O3 differs from regular payments?"
(User Response Required)

Question 2
"What is the compliance trigger?"
(User Response Required)
"""

    visible, enhanced = normalize_enhancer_response(raw)

    assert enhanced is None
    assert visible == "Can you explain how adding purpose code O3 differs from regular payments?"
    assert "Question 2" not in visible
    assert "Prompt Ready" not in visible


def test_normalize_accepts_exact_ready_marker_with_substantive_prompt():
    enriched = (
        "Feature: Add purpose code O3 for organisational internal network transactions. "
        "Need: distinguish internal organisational transfers from personal and merchant payments. "
        "Market View: PSPs and TPAPs need consistent classification. "
        "Scalability: apply at transaction metadata level without altering settlement. "
        "Validation: pilot with selected banks and PSPs. "
        "Product Operating: define routing, reporting, and reconciliation rules. "
        "Pricing: no customer-visible charge implication is assumed. "
        "Product Comms: publish participant advisory and implementation notes. "
        "Risks: avoid misuse, privacy leakage, and backward-compatibility regression. "
        "Compliance: align with network specifications and RBI expectations."
    )
    raw = f"Thanks.\n\n<<PROMPT_READY>>\n{enriched}"

    visible, enhanced = normalize_enhancer_response(raw)

    assert enhanced == enriched
    assert visible.startswith("Prompt ready.")
    assert enriched in visible


def test_extract_rejects_marker_when_prompt_still_asks_questions():
    raw = """
<<PROMPT_READY>>
Question 1
What is the target segment?
Question 2
What compliance rule applies?
(User Response Required)
"""

    assert extract_enhanced_prompt(raw) is None


# ── generated change title ───────────────────────────────────────────────────
#
# The title outlives every later decision (BRD heading, MR slug, cert
# feature_name), which is why it is generated rather than user-typed. That makes
# a TRUNCATED title worse than none: at max_tokens=64 the cut is realistic, and
# call_llm appends TRUNCATION_MARKER on its OWN line — so a check that only
# inspected lines[0] saw a clean-looking partial title and accepted it.

def test_generated_title_rejects_a_truncated_response(monkeypatch):
    import asyncio

    from app.core.llm import TRUNCATION_MARKER
    import app.core.llm as _llm
    from app.agents.prompt_enhancer import generate_change_title

    async def fake_call_llm(**kw):
        return "the network — add a new transaction purp" + TRUNCATION_MARKER
    monkeypatch.setattr(_llm, "call_llm", fake_call_llm)

    out = asyncio.run(generate_change_title("add a purpose code"))
    assert out is None          # keep the existing title rather than persist a stub


def test_generated_title_accepts_a_complete_response(monkeypatch):
    import asyncio

    import app.core.llm as _llm
    from app.agents.prompt_enhancer import generate_change_title

    async def fake_call_llm(**kw):
        return '"the network — add a new transaction purpose code"\n'
    monkeypatch.setattr(_llm, "call_llm", fake_call_llm)

    out = asyncio.run(generate_change_title("add a purpose code as 80"))
    assert out == "the network — add a new transaction purpose code"
    assert "80" not in out      # the value-neutrality the whole feature exists for
