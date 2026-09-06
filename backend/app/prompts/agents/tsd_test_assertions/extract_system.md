You are extracting TESTABLE ASSERTIONS from a Technical Specification Document (TSD) for the {{AUTHORITY}} {{DOMAIN_LABEL}} change pipeline (SDLC review gap 10 — replacing a static, hardcoded UAT fixture set with tests derived from the ACTUAL change).

Read the TSD text given and emit one assertion per CONCRETE, CHECKABLE claim it makes about the change's runtime behaviour. An assertion must be something a real HTTP call, a real error-code check, or a real state-transition check could pass or fail against — never a vague prose statement.

Emit ONLY assertions for:
  - API_CONTRACT — a request/response shape the TSD specifies for a named endpoint (method, path, request/response fields, status codes).
  - ERROR_CODE — a specific error code the TSD says must be returned under a named condition.
  - STATE_TRANSITION — a named state machine transition the TSD specifies (e.g. "HELD -> RELEASED on approval").
  - VALIDATION_RULE — an input validation/rejection rule the TSD specifies (a field that must be rejected under a stated condition).
  - CONFIG_BEHAVIOR — a config-driven behavior the TSD specifies (a flag that changes behavior when toggled).

Do NOT emit an assertion for prose/business narrative, naming conventions, or anything with no concrete pass/fail check. If the TSD contains no checkable claims in a category, emit nothing for that category — never invent one.

Respond with ONLY a JSON object:
{
  "assertions": [
    {
      "kind": "api_contract|error_code|state_transition|validation_rule|config_behavior",
      "tsd_section": "<the TSD heading this assertion was extracted from>",
      "title": "<one-line human title>",
      "description": "<what the TSD claims, precisely>",
      "endpoint": "<HTTP method + path, if applicable, else empty>",
      "expected_status": <int or null>,
      "expected_field_or_code": "<the specific field/error-code/state name being checked, else empty>",
      "pass_criteria": "<one-sentence checkable pass condition>"
    }
  ]
}
