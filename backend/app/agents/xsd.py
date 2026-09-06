# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""XSD Generator agent.

Determines whether schema changes are required for a UPI feature, then generates
the updated XSD with diff annotations. Handles iterative feedback.
"""
import logging
from app.core.prompts import render_prompt
from collections.abc import AsyncGenerator

from app.core.llm import stream_llm
from app.core.domain.registry import prompt_block

# Supplied by the active domain pack, not imported from a UPI module.
NETWORK_HARD_RULES = prompt_block("hard_rules")
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE

logger = logging.getLogger(__name__)

ASSESSMENT_SYSTEM_PROMPT = render_prompt(
    "agents/xsd/assessment_system_prompt.md",
    NETWORK_HARD_RULES=NETWORK_HARD_RULES, ANTI_INJECTION_CLAUSE=ANTI_INJECTION_CLAUSE,
    PLATFORM_NAME=prompt_block("platform_name", "this change-management platform"),
    DOMAIN_NAME=prompt_block("domain_name", "this domain"),
)

XSD_GENERATION_SYSTEM_PROMPT = render_prompt(
    "agents/xsd/xsd_generation_system_prompt.md",
    NETWORK_HARD_RULES=NETWORK_HARD_RULES, ANTI_INJECTION_CLAUSE=ANTI_INJECTION_CLAUSE,
    PLATFORM_NAME=prompt_block("platform_name", "this change-management platform"),
    DOMAIN_NAME=prompt_block("domain_name", "this domain"),
    AUTHORITY=prompt_block("authority", "the platform operator"),
)


async def stream_xsd_assessment(
    tech_spec_content: str,
    brd_content: str,
) -> AsyncGenerator[str, None]:
    """Assess whether XSD changes are required."""
    message = f"""Analyse the following Technical Specification and BRD to determine
whether XSD changes are required.

TECHNICAL SPECIFICATION:
{wrap_untrusted(tech_spec_content[:4000] + ('...' if len(tech_spec_content) > 4000 else ''), "TECH_SPEC_CONTENT")}

---
BRD (summary):
{wrap_untrusted(brd_content[:2000] + ('...' if len(brd_content) > 2000 else ''), "BRD_CONTENT")}
"""
    logger.info("XSDAgent — streaming assessment")

    async for chunk in stream_llm(system=ASSESSMENT_SYSTEM_PROMPT, messages=[{"role": "user", "content": message}], max_tokens=2048, agent_name="xsd"):
        yield chunk


async def stream_xsd_turn(
    tech_spec_content: str,
    brd_content: str,
    conversation_history: list[dict],
    new_user_message: str,
) -> AsyncGenerator[str, None]:
    """Stream an XSD generation or feedback refinement turn."""
    context = f"""TECHNICAL SPECIFICATION:
{wrap_untrusted(tech_spec_content[:4000] + ('...' if len(tech_spec_content) > 4000 else ''), "TECH_SPEC_CONTENT")}

---
BRD (summary):
{wrap_untrusted(brd_content[:2000] + ('...' if len(brd_content) > 2000 else ''), "BRD_CONTENT")}
"""
    if len(conversation_history) == 0:
        messages = [{"role": "user", "content": f"{context}\n\n---\n{wrap_untrusted(new_user_message, 'USER_MESSAGE')}"}]
    else:
        messages = conversation_history + [{"role": "user", "content": wrap_untrusted(new_user_message, "USER_MESSAGE")}]

    logger.info("XSDAgent — streaming turn, history_len=%d", len(messages))

    # max_tokens bumped 8096 → 24000 (2026-05-04, Layer-3 of truncation fix).
    # XSD outputs include full schema body + commentary; previously hit cap.
    async for chunk in stream_llm(system=XSD_GENERATION_SYSTEM_PROMPT, messages=messages, max_tokens=24000, agent_name="xsd"):
        yield chunk
