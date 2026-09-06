# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Cert Triage Agent — analyzes failed certification tests and produces verdicts.

For each failed test, determines the root cause:
  - PARTNER_CODE_BUG: Partner's implementation has a defect
  - TEST_CASE_ISSUE: The test case itself is incorrect or outdated
  - ENV_ISSUE: Environmental problem (connectivity, config, timeout)
"""
import json
from app.core.domain.registry import prompt_block
from app.core.prompts import render_prompt
import logging

from app.core.llm import call_llm
from app.core.json_recovery import parse_llm_json

logger = logging.getLogger(__name__)

# Identity nouns come from the active domain pack; under the default UPI pack
# this renders byte-identically to the previous hardcoded file.
SYSTEM_PROMPT = render_prompt(
    "agents/cert_triage/system_prompt.md",
    AUTHORITY=prompt_block("authority", "the ecosystem authority"),
    AUTHORITY_FULL=prompt_block(
        "authority_full", prompt_block("authority", "the ecosystem authority")),
    DOMAIN_LABEL=prompt_block("domain_name", "partner"),
)


async def triage_failed_tests(failed_results: list[dict]) -> list[dict]:
    """
    Analyze failed certification test results and produce AI verdicts.

    Args:
        failed_results: List of dicts with id, test_case_id, expected_response, actual_response, direction.

    Returns:
        List of {test_result_id, verdict, reasoning} dicts.
    """
    if not failed_results:
        return []

    tests_desc = json.dumps(failed_results, indent=2, default=str)
    logger.info("CertTriageAgent — analyzing %d failed tests", len(failed_results))

    raw = await call_llm(
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Analyze these failed certification tests:\n\n{tests_desc}"}],
        max_tokens=2000,
        agent_name="cert_triage",
    )
    fallback = [{"test_result_id": r["id"], "verdict": "env_issue", "reasoning": "Triage parsing failed"} for r in failed_results]
    verdicts = await parse_llm_json(raw, expect_array=True, fallback=fallback)
    if not isinstance(verdicts, list):
        verdicts = [verdicts] if verdicts else fallback

    logger.info("CertTriageAgent — produced %d verdicts", len(verdicts))
    return verdicts
