# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Code Review Agent.

Analyses Java source files against SonarQube + PMD rule sets using Claude AI.
Produces a structured JSON issue list or declares the code clean.
"""
import logging
from collections.abc import AsyncGenerator

from app.core.llm import call_llm, stream_llm
from app.core.json_recovery import parse_llm_json
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a senior Java code quality reviewer at the Authority.

You must review the submitted Java source files against two rulesets:

## SonarQube Rules
- Cyclomatic complexity > 10 per method
- Code duplication (identical blocks ≥ 6 lines)
- Naming conventions (camelCase for methods/variables, PascalCase for classes, UPPER_SNAKE for constants)
- Unused imports and variables
- Method length > 50 lines
- Class coupling > 10 external dependencies
- Empty methods without documentation
- Missing null checks on method parameters

## PMD Rules
- Empty catch blocks (must at least log the exception)
- Unnecessary object creation (use primitive where possible)
- God classes (> 500 lines or > 20 methods)
- Magic numbers (numeric literals instead of named constants)
- String concatenation in loops (use StringBuilder)
- Deeply nested if statements (> 3 levels)
- Overly complex boolean expressions
- Missing @Override annotation on overridden methods

## Output Format

You MUST respond with ONLY a JSON object — no markdown, no explanation outside the JSON.

ALWAYS include the "rules_checked" object showing how many rules were checked per ruleset,
and the "stats" object with issue counts broken down by ruleset and severity.

If the code is clean:
```json
{
  "status": "clean",
  "summary": "All files pass SonarQube and PMD quality checks.",
  "rules_checked": {
    "sonarqube": {"total": 8, "rules": ["CyclomaticComplexity", "CodeDuplication", "NamingConvention", "UnusedImports", "MethodLength", "ClassCoupling", "EmptyMethod", "NullCheck"]},
    "pmd": {"total": 8, "rules": ["EmptyCatchBlock", "UnnecessaryObjectCreation", "GodClass", "MagicNumber", "StringConcatInLoop", "DeeplyNestedIf", "ComplexBooleanExpression", "MissingOverride"]}
  },
  "stats": {
    "files_reviewed": 3,
    "sonarqube_issues": 0,
    "pmd_issues": 0,
    "by_severity": {}
  },
  "issues": []
}
```

If issues are found:
```json
{
  "status": "issues_found",
  "summary": "Found N issues across M files.",
  "rules_checked": {
    "sonarqube": {"total": 8, "rules": ["CyclomaticComplexity", "CodeDuplication", "NamingConvention", "UnusedImports", "MethodLength", "ClassCoupling", "EmptyMethod", "NullCheck"]},
    "pmd": {"total": 8, "rules": ["EmptyCatchBlock", "UnnecessaryObjectCreation", "GodClass", "MagicNumber", "StringConcatInLoop", "DeeplyNestedIf", "ComplexBooleanExpression", "MissingOverride"]}
  },
  "stats": {
    "files_reviewed": 3,
    "sonarqube_issues": 2,
    "pmd_issues": 1,
    "by_severity": {"major": 2, "minor": 1}
  },
  "issues": [
    {
      "ruleset": "sonarqube",
      "rule": "CyclomaticComplexity",
      "severity": "major",
      "file": "com/example/network/service/PaymentService.java",
      "line": 42,
      "message": "Method processPayment has cyclomatic complexity of 15 (threshold: 10)",
      "fix": "Extract conditional branches into separate private methods"
    }
  ]
}
```

Each issue MUST include "ruleset" field with value "sonarqube" or "pmd".
Severity levels: "blocker", "critical", "major", "minor", "info"

Be thorough but fair. Only flag genuine violations — do not flag standard framework patterns
(e.g. Spring @Autowired fields, Lombok annotations, standard Builder patterns).
Focus on real maintainability and correctness issues.

IMPORTANT: If the code was previously reviewed and issues were sent back for fixing,
you will receive the previous issues list. In that case, focus on verifying whether
those specific issues have been addressed. If they have been fixed, respond with "clean".
Only flag NEW issues that were not in the previous review. Do NOT re-flag issues that
the developer has already addressed — even if the fix is a reasonable alternative approach
to what you originally suggested.

""" + ANTI_INJECTION_CLAUSE


async def run_code_review(files: list[dict], previous_issues: list[dict] | None = None) -> dict:
    """
    Run AI code review on the given file list.

    Args:
        files:           List of {"path": "...", "content": "..."} dicts.
        previous_issues: Issues from a prior review round (if this is a re-review after loop-back).

    Returns:
        {"status": "clean"|"issues_found", "summary": "...", "issues": [...]}
    """
    if not files:
        return {"status": "clean", "summary": "No files to review.", "issues": []}

    # Build the user message with all files
    file_sections = []
    for f in files:
        file_sections.append(f"### File: {f['path']}\n{wrap_untrusted(f['content'], 'SOURCE_FILE')}")
    user_message = "Review the following Java source files:\n\n" + "\n\n".join(file_sections)

    if previous_issues:
        issue_summary = "\n".join(
            f"- [{iss.get('severity', 'major')}] {iss.get('file', '?')}:{iss.get('line', '?')} — {iss.get('message', iss.get('description', ''))}"
            for iss in previous_issues
        )
        user_message += (
            f"\n\n---\n## PREVIOUS REVIEW ISSUES (already sent back for fixing)\n"
            f"The developer was asked to fix these issues. Verify they are addressed. "
            f"If all are fixed, respond with status 'clean'. Only flag genuinely NEW issues.\n\n"
            f"{wrap_untrusted(issue_summary, 'PREVIOUS_ISSUES')}"
        )

    logger.info("CodeReviewAgent — reviewing %d files, previous_issues=%d", len(files), len(previous_issues or []))

    # max_tokens 4000 → 12000 (2026-05-04, Layer-3 of truncation fix).
    # Reviews of 8+ files with detailed JSON issue lists exceed 4k tokens.
    raw_text = await call_llm(system=SYSTEM_PROMPT, messages=[{"role": "user", "content": user_message}], max_tokens=12000, agent_name="code_review")

    fallback = {
        "status": "issues_found",
        "summary": "Code review completed but response parsing failed. Please retry.",
        "issues": [],
        "raw_response": raw_text[:2000],
    }
    result = await parse_llm_json(raw_text, fallback=fallback)

    stats = result.get("stats", {})
    issues = result.get("issues", [])
    logger.info(
        "CodeReviewAgent — done: status=%s issues=%d sonarqube=%d pmd=%d files_reviewed=%d",
        result.get("status"), len(issues),
        stats.get("sonarqube_issues", 0), stats.get("pmd_issues", 0),
        stats.get("files_reviewed", len(files)),
    )
    return result


async def stream_code_review(files: list[dict]) -> AsyncGenerator[str, None]:
    """
    Stream the code review analysis token-by-token for UI display.
    The final result is the full response text (JSON).
    """
    if not files:
        yield '{"status": "clean", "summary": "No files to review.", "issues": []}'
        return

    file_sections = []
    for f in files:
        file_sections.append(f"### File: {f['path']}\n{wrap_untrusted(f['content'], 'SOURCE_FILE')}")
    user_message = "Review the following Java source files:\n\n" + "\n\n".join(file_sections)

    logger.info("CodeReviewAgent — streaming review for %d files", len(files))

    # max_tokens 4000 → 12000 (2026-05-04, Layer-3 of truncation fix).
    async for chunk in stream_llm(system=SYSTEM_PROMPT, messages=[{"role": "user", "content": user_message}], max_tokens=12000, agent_name="code_review"):
        yield chunk
