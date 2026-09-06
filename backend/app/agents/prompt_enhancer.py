# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Prompt Enhancer agent.

Conducts a clarifying Q&A with the user to enrich their initial feature idea
into a well-scoped prompt before handing off to the Deep Researcher.

The agent streams responses token-by-token via an async generator so the
WebSocket handler can forward each chunk to the browser in real time.
"""
import logging
from app.core.prompts import load_prompt, render_prompt
import re
from collections.abc import AsyncGenerator

from app.agents._prompt_safety import ANTI_INJECTION_CLAUSE, wrap_untrusted
from app.core.domain.registry import prompt_block
from app.core.llm import stream_llm

logger = logging.getLogger(__name__)

PROMPT_READY_MARKER = "<<PROMPT_READY>>"


async def generate_change_title(enhanced_prompt: str, fallback: str | None = None) -> str | None:
    """AI-generated change TITLE from the enhanced ask — replaces the user-typed one.

    The title is the one artifact that outlives every later decision: it becomes the BRD
    outline heading, the MR/export slug, the cert feature_name and negotiation context —
    and it is never revised when clarification supersedes a value it happens to embed.
    A user-typed title carrying a concrete proposed value ("UPI - Add new purpose code
    as 80") kept re-injecting that value into downstream context after ratification had
    settled on a different one — it was the only surviving wrong-value token in the
    BT/80 incident. So the generated title names the CAPABILITY, never a proposed value.

    Returns the title, or None on any failure — callers then keep the existing title
    (fail-open: availability over neutrality)."""
    text = (enhanced_prompt or "").strip() or (fallback or "").strip()
    if not text:
        return None
    system = (
        "You write the short TITLE for a change request. Output ONLY the title — one "
        "line, at most 12 words, plain language, no quotes, no trailing punctuation.\n"
        "HARD RULE: never embed a specific proposed value, code, number, enum literal or "
        "identifier from the request (a purpose-code value, an error code, a field "
        "length). Name the capability being changed, not the value it will take — values "
        "are decided later at ratification, and a stale value in the title poisons every "
        "downstream document heading that renders it.\n"
        "Example: request 'Add a new priority level as 5' → title "
        "'Add a new priority level'."
        + ANTI_INJECTION_CLAUSE
    )
    try:
        from app.core.llm import call_llm
        raw = await call_llm(
            system=system,
            messages=[{"role": "user", "content": wrap_untrusted(text[:6000], "CHANGE_REQUEST")}],
            max_tokens=64, agent_name="prompt_enhancer",
        )
        # Truncation is checked against the WHOLE response, not lines[0]: call_llm appends
        # TRUNCATION_MARKER on its own line, so a max_tokens cut leaves lines[0] holding a
        # clean-looking PARTIAL title and a lines[0]-only test can never see the marker.
        # At max_tokens=64 that cut is realistic, and this title outlives every later
        # decision (BRD heading / MR slug / cert feature_name) — reject, don't persist.
        from app.core.llm import TRUNCATION_MARKER
        if TRUNCATION_MARKER.strip() in (raw or "") or "OUTPUT TRUNCATED" in (raw or ""):
            logger.warning("change-title generation truncated at max_tokens — keeping the existing title")
            return None
        lines = [ln.strip().strip('"').strip("'") for ln in (raw or "").splitlines() if ln.strip()]
        title = lines[0] if lines else ""
        if not title:
            return None
        return title[:120]
    except Exception as e:  # noqa: BLE001 — fail-open; the existing title stays
        logger.warning("change-title generation failed (%s) — keeping the existing title", e)
        return None

# Domain vocabulary supplied by the active domain pack, not hardcoded here —
# see docs/genericization sweep. `render_prompt`'s contract is exact (every
# `{{NAME}}` in the template must be supplied), so this list must track the
# template's placeholders 1:1.
SYSTEM_PROMPT = render_prompt(
    "agents/prompt_enhancer/system_prompt.md",
    PLATFORM_NAME=prompt_block("platform_name", "this change-management platform"),
    ECOSYSTEM_DESCRIPTION=prompt_block("ecosystem_description", ""),
    COMPLIANCE_NOTE=prompt_block("compliance_note", ""),
    ECOSYSTEM_ACTORS=prompt_block("ecosystem_actors", "the platform's ecosystem"),
    ANTI_INJECTION_CLAUSE=ANTI_INJECTION_CLAUSE,
)

# Appended once the prompt is already ready. Without it the model treats a
# refinement request as just another turn of the clarifying Q&A and replies with
# a question, so the user's edit silently never reaches `cr.enhanced_prompt`.
REFINEMENT_INSTRUCTIONS = load_prompt("agents/prompt_enhancer/refinement_instructions.md")


async def stream_enhancer_turn(
    conversation_history: list[dict],
    new_user_message: str,
    source_material: str = "",
    refining: bool = False,
) -> AsyncGenerator[str, None]:
    """
    Append the user message and stream the assistant's next response.

    Yields text chunks as they arrive from the Claude API.

    Args:
        conversation_history: List of {"role": "user"|"assistant", "content": "..."} dicts
                               representing the conversation so far (excluding new message).
        new_user_message:     The latest message from the user.
        source_material:      Optional pre-wrapped SOURCE DOCUMENT block (the detailed BRD
                              the PM uploaded at change creation — services.source_material).
                              Appended to the system prompt so clarifying questions are
                              generated FROM the document's actual content/gaps instead of
                              re-asking what it already answers.
        refining:             True when an enhanced prompt already exists and this turn is
                              the user asking to change it — switches the agent from "ask
                              the next clarifying question" to "re-emit the full revised
                              prompt".

    Yields:
        str — text chunks of the assistant's response
    """
    messages = conversation_history + [{"role": "user", "content": wrap_untrusted(new_user_message, "USER_MESSAGE")}]

    system = SYSTEM_PROMPT
    if source_material:
        system = (SYSTEM_PROMPT + source_material
                  + "\n\nThe PM uploaded the SOURCE DOCUMENT above with this change. Mine it "
                    "FIRST: never ask a question it already answers; instead, ask about its "
                    "gaps, ambiguities, and conflicts. The enhanced prompt you produce must "
                    "fold in the document's key requirements (fields, limits, flows) so "
                    "later stages inherit them.")
    if refining:
        system = system + REFINEMENT_INSTRUCTIONS

    logger.info("PromptEnhancer — streaming turn, history_len=%d source_chars=%d refining=%s",
                len(messages), len(source_material), refining)

    # max_tokens=4000 matches is_review / cert_triage chat agents. The
    # original 1024 truncated regularly — the final response carries
    # clarifying questions AND the enriched prompt after <<PROMPT_READY>>,
    # which together routinely exceed 800 tokens.
    #
    # temperature=0.3 (API default is 1.0, unset): this is the first turn a PM
    # sees, and at default temperature two identical (system, first-message)
    # pairs produced noticeably different first questions — same domain
    # vocabulary, same topic, but one asked a specific, well-grounded question
    # and the other a more generic one. Lower temperature narrows that spread
    # without making the model deterministic/robotic.
    async for chunk in stream_llm(system=system, messages=messages, max_tokens=4000, agent_name="prompt_enhancer", temperature=0.3):
        yield chunk


def extract_enhanced_prompt(full_response: str) -> str | None:
    """
    If the response contains <<PROMPT_READY>>, extract and return the enriched
    prompt text that follows it.  Returns None if the marker is absent.
    """
    marker = "<<PROMPT_READY>>"
    if marker not in full_response:
        logger.info("PromptEnhancer — no <<PROMPT_READY>> marker in response (len=%d)", len(full_response))
        return None
    parts = full_response.split(marker, 1)
    enriched = parts[1].strip()
    if enriched:
        logger.info("PromptEnhancer — enhanced prompt extracted, len=%d", len(enriched))
    return enriched if enriched else None


def normalize_enhancer_response(full_response: str) -> tuple[str, str | None]:
    """Return the user-visible assistant text and optional enhanced prompt.

    Local/smaller models sometimes imitate the hidden readiness protocol by
    writing "Prompt Ready Instruction" while still asking many questions. That
    should remain a clarification turn, not a completed prompt. This guard keeps
    the product flow deterministic without changing the WebSocket protocol.
    """
    text = (full_response or "").strip()
    enhanced = _extract_enhanced_prompt_strict(text)
    if enhanced:
        return f"Prompt ready.\n\n{enhanced}", enhanced

    if _looks_like_unresolved_questionnaire(text):
        question = _extract_first_question(text)
        logger.warning(
            "PromptEnhancer â€” collapsed invalid multi-question response to one question"
        )
        return question, None

    return text, None


def _extract_enhanced_prompt_strict(full_response: str) -> str | None:
    marker_match = re.search(rf"(?m)^\s*{re.escape(PROMPT_READY_MARKER)}\s*$", full_response or "")
    if not marker_match:
        return None

    enriched = full_response[marker_match.end():].strip()
    if _looks_like_unresolved_questionnaire(enriched):
        logger.warning(
            "PromptEnhancer â€” rejected ready marker because response still asks for user input"
        )
        return None
    if len(enriched) < 120:
        logger.warning(
            "PromptEnhancer â€” rejected ready marker because enriched prompt is too short (len=%d)",
            len(enriched),
        )
        return None
    logger.info("PromptEnhancer â€” enhanced prompt extracted, len=%d", len(enriched))
    return enriched


def _looks_like_unresolved_questionnaire(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    question_count = text.count("?")
    numbered_questions = len(re.findall(r"(?im)^\s*(?:\*\*)?\s*question\s+\d+\b", text))
    return (
        numbered_questions > 1
        or question_count > 2
        or "user response required" in lower
        or "prompt ready instruction" in lower
        or ("prompt ready" in lower and PROMPT_READY_MARKER.lower() not in lower)
    )


def _extract_first_question(text: str) -> str:
    for pattern in (
        r"(?is)question\s+1[^\n]*\n\s*[\"“”']?([^?\n]{12,260}\?)",
        r"(?is)[\"“”']([^?\n]{12,260}\?)[\"“”']",
        r"(?is)([^?\n]{12,260}\?)",
    ):
        match = re.search(pattern, text)
        if match:
            question = re.sub(r"\s+", " ", match.group(1)).strip(" \"'“”")
            question = re.sub(r"^\s*(?:\d+[\).]\s*)+", "", question).strip()
            if question:
                return question
    return "What problem does this change solve, and who is affected by it?"


# Keep the public helper strict for any older import sites/tests.
extract_enhanced_prompt = _extract_enhanced_prompt_strict
