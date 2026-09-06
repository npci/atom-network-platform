# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""IS (Information Security) Review Agent.

Analyses Java source files against OWASP Top 10 and NPCI security standards.
Produces a structured JSON findings list or declares the code secure.
"""
import logging
from collections.abc import AsyncGenerator

from app.agents._prompt_safety import ANTI_INJECTION_CLAUSE, wrap_untrusted
from app.core.llm import call_llm, stream_llm
from app.core.json_recovery import parse_llm_json

logger = logging.getLogger(__name__)

# Domain vocabulary from the active pack (genericisation sweep). OWASP and the
# generic secure-coding rules are platform truth; the org name, the
# domain-specific standards block and the example finding are pack content.
# For UPI the assembled prompt is byte-identical to the previous hardcoded
# text (prompt-snapshot-verified).
from app.core.domain.registry import prompt_block

_ORG = prompt_block("authority", "the platform operator")
_ORG_FULL = prompt_block("authority_full_name", "")
_REVIEWER_ORG = f"{_ORG} ({_ORG_FULL})" if _ORG_FULL else _ORG
_DOMAIN_STANDARDS = prompt_block(
    "infosec_domain_notes",
    "## Platform Security Standards\n"
    "- No hardcoded secrets, API keys, passwords, or tokens anywhere in code\n"
    "- No PII logged in plain text\n"
    "- All external communication must enforce TLS (no http:// URLs for APIs)\n"
    "- No sensitive data in URL query parameters\n"
    "- Proper use of CSRF tokens for state-changing operations\n"
    "- Secure random number generation (SecureRandom, not Random)",
)
_EXAMPLE_FILE = prompt_block("infosec_example_file",
                             "com/example/repository/TransactionRepo.java")
_EXAMPLE_PARAM = prompt_block("infosec_example_param", "userSuppliedText")

SYSTEM_PROMPT = ("""You are a senior Information Security reviewer at """ + _REVIEWER_ORG + """.

You must review the submitted Java source files against two security frameworks:

## OWASP Top 10 for Java
- **A01 Broken Access Control** — missing authorization checks, direct object references
- **A02 Cryptographic Failures** — weak algorithms, hardcoded keys, plain-text sensitive data
- **A03 Injection** — SQL injection, LDAP injection, XML injection, OS command injection
- **A04 Insecure Design** — missing input validation, insecure direct object references
- **A05 Security Misconfiguration** — verbose error messages, default credentials, debug enabled
- **A06 Vulnerable Components** — known vulnerable library versions (flag if you detect version refs)
- **A07 Authentication Failures** — weak password handling, missing rate limiting
- **A08 Data Integrity Failures** — insecure deserialization, unsigned data
- **A09 Logging Failures** — insufficient logging of security events, PII in logs
- **A10 SSRF** — unvalidated URLs in server-side requests

""" + _DOMAIN_STANDARDS + """

## Output Format

You MUST respond with ONLY a JSON object — no markdown, no explanation outside the JSON.

If the code is secure:
```json
{
  "status": "clean",
  "summary": "All files pass OWASP Top 10 and """ + _ORG + """ security checks.",
  "findings": []
}
```

If findings are found:
```json
{
  "status": "issues_found",
  "summary": "Found N security findings across M files.",
  "findings": [
    {
      "cwe": "CWE-89",
      "owasp": "A03:2021 Injection",
      "severity": "critical",
      "file": \"""" + _EXAMPLE_FILE + """",
      "line": 28,
      "description": "SQL query built via string concatenation with user-supplied """ + _EXAMPLE_PARAM + """ parameter",
      "remediation": "Use parameterized query with JPA @Query or PreparedStatement"
    }
  ]
}
```

Severity levels: "critical", "high", "medium", "low", "info"

Be thorough on genuine security issues. Do not flag standard Spring Security patterns
or framework-provided protections. Focus on real vulnerabilities that could be exploited.

""" + ANTI_INJECTION_CLAUSE)


async def run_is_review(files: list[dict]) -> dict:
    """
    Run AI security review on the given file list.

    Args:
        files: List of {"path": "...", "content": "..."} dicts.

    Returns:
        {"status": "clean"|"issues_found", "summary": "...", "findings": [...]}
    """
    if not files:
        return {"status": "clean", "summary": "No files to review.", "findings": []}

    file_sections = []
    for f in files:
        file_sections.append(f"### File: {f['path']}\n{wrap_untrusted(f['content'], 'SOURCE_FILE')}")
    user_message = "Security review the following Java source files:\n\n" + "\n\n".join(file_sections)

    logger.info("ISReviewAgent — reviewing %d files", len(files))

    raw_text = await call_llm(system=SYSTEM_PROMPT, messages=[{"role": "user", "content": user_message}], max_tokens=4000, agent_name="is_review")

    fallback = {
        "status": "issues_found",
        "summary": "IS review completed but response parsing failed. Please retry.",
        "findings": [],
        "raw_response": raw_text[:2000],
    }
    result = await parse_llm_json(raw_text, fallback=fallback)

    findings = result.get("findings", [])
    logger.info("ISReviewAgent — done: status=%s findings=%d", result.get("status"), len(findings))
    return result


async def stream_is_review(files: list[dict]) -> AsyncGenerator[str, None]:
    """Stream the IS review analysis token-by-token for UI display."""
    if not files:
        yield '{"status": "clean", "summary": "No files to review.", "findings": []}'
        return

    file_sections = []
    for f in files:
        file_sections.append(f"### File: {f['path']}\n{wrap_untrusted(f['content'], 'SOURCE_FILE')}")
    user_message = "Security review the following Java source files:\n\n" + "\n\n".join(file_sections)

    logger.info("ISReviewAgent — streaming review for %d files", len(files))

    async for chunk in stream_llm(system=SYSTEM_PROMPT, messages=[{"role": "user", "content": user_message}], max_tokens=4000, agent_name="is_review"):
        yield chunk
