# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Certification Testing Agent — executes bidirectional tests between the Authority and partners.

Parses the cert_test_cases document from Phase A Product Kit, creates structured test cases,
and simulates execution with pass/fail results.
"""
import logging
import re

from app.core.llm import call_llm
from app.core.json_recovery import parse_llm_json
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a certification testing engine for network ecosystem.

Given a certification test cases document (from Phase A Product Kit), parse it into structured test cases
and simulate execution results.

For each test case, produce a JSON result with:
- test_case_id: e.g. "TC-001"
- title: short description
- direction: "npci_to_partner" or "partner_to_npci"
- http_method: GET/POST/PUT etc
- endpoint: the API endpoint being tested
- request_payload: sample request (JSON)
- expected_response: what the correct response should contain
- actual_response: simulated actual response (match expected for pass, differ for fail)
- status: "pass" or "fail" (make ~80% pass, ~20% fail to simulate realistic results)
- latency_ms: random between 50-500ms

Respond with ONLY a JSON array of test results. No markdown, no explanation.

Example:
[
  {
    "test_case_id": "TC-001",
    "title": "the network Pay Request - Happy Path",
    "direction": "npci_to_partner",
    "status": "pass",
    "expected_response": {"status": "SUCCESS", "responseCode": "00"},
    "actual_response": {"status": "SUCCESS", "responseCode": "00"},
    "latency_ms": 120
  }
]

Generate 10-15 realistic test cases based on the provided test document. Make results realistic —
most should pass, a few should fail with plausible error responses.

""" + ANTI_INJECTION_CLAUSE


async def run_certification_tests(cert_test_doc: str, change_title: str) -> list[dict]:
    """
    Parse cert test cases document and simulate test execution.

    Args:
        cert_test_doc: The cert_test_cases document content from Phase A Product Kit.
        change_title: Title of the change request for context.

    Returns:
        List of test result dicts.
    """
    logger.info("CertTestingAgent — generating test results for '%s'", change_title)

    raw = await call_llm(
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"Feature: {wrap_untrusted(change_title, 'CHANGE_TITLE')}\n\n"
                f"Certification Test Cases Document:\n{wrap_untrusted(cert_test_doc[:6000], 'CERT_TEST_DOC')}"
            ),
        }],
        max_tokens=4000,
        agent_name="cert_testing",
    )

    results = await parse_llm_json(raw, expect_array=True, fallback=[])
    if not isinstance(results, list):
        results = [results] if results else []

    logger.info("CertTestingAgent — generated %d test results", len(results))
    return results
