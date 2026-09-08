{
  "apis": [
    {"name": "ReqExample", "response": "RespExample", "initiator": "<initiating participant>", "description": "..."}
  ],
  "request_fields": [
    {"name": "exampleField", "type": "String", "mandatory": true, "dLength": "64", "description": "..."}
  ],
  "response_fields": [
    {"name": "result", "type": "String", "mandatory": true, "dLength": "10", "description": "..."}
  ],
  "error_codes": [
    {"code": "<domain error code>", "td_bd": "<two-letter class>", "entity": "<responsible participant>", "description": "..."}
  ],
  "auth_method": "<how requests are authenticated / authorised, or null>",
  "transaction_limit": "<the governing limit or cap, or null>",
  "flow_sequence": [
    "Step 1: <actor> — <action>",
    "Step 2: <actor> — <action>"
  ],
  "current_state": "Today ...",
  "limitations": "The existing flow suffers from ...",
  "functional_requirements": [
    "FR-01: The system shall ...",
    "FR-02: ...",
    "FR-03: ...",
    "FR-04: ...",
    "FR-05: ...",
    "FR-06: ..."
  ],
  "dispute_framework": "<how exceptions / disputes are handled, or null>",
  "user_journey_plain": [
    "Step 1: <user action>",
    "..."
  ],
  "test_scenarios": [
    {"scenario": "Happy path", "objective": "Verify the primary flow succeeds", "owner": "<participant>"}
  ],
  "policy_rules": [
    "<a governing policy rule>"
  ],
  "failure_scenarios": [
    {"scenario": "<failure>", "behavior": "<expected handling, naming the domain's error code>"}
  ],
  "participant_obligations": {
    "<participant group>": ["<obligation>", "..."]
  },
  "go_live_timeline": "2026-06-30 (or null if undecided)",
  "supersedes_circular": "<superseded reference> (or null)"
}
