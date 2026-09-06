<!--
GENERATED FILE -- DO NOT EDIT BY HAND.
Your change will be overwritten by the next regeneration, and CI compares
this file against a fresh run.

    Regenerate: bash scripts/wiki/regenerate.sh
    Generator:  scripts/wiki/generate_reference.py
-->
# Data model

> **Generated** from `SQLAlchemy `Base.metadata``, against alembic head `0123_governance_skill_slots`.
> Do not edit by hand -- run `bash scripts/wiki/regenerate.sh`.

84 tables at alembic head `0123_governance_skill_slots`.

Migrations are idempotent and inspector-gated; follow the shape in `alembic/versions/0035_a2a_session_revocation.py` when adding one.

## `a2a_messages`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | yes | FK -> `change_requests.id` |
| `partner_id` | `VARCHAR(36)` | no | FK -> `partner_agents.id` |
| `direction` | `VARCHAR(8)` | no |  |
| `task_type` | `VARCHAR(64)` | no |  |
| `payload` | `JSON` | yes |  |
| `response_body` | `JSON` | yes |  |
| `status` | `VARCHAR(50)` | no |  |
| `created_at` | `DATETIME` | no |  |
| `task_id_a2a` | `VARCHAR(64)` | yes |  |
| `task_state` | `VARCHAR(20)` | yes |  |
| `protocol_ver` | `VARCHAR(20)` | no |  |
| `caller_ip` | `VARCHAR(45)` | yes |  |
| `jwt_sub` | `VARCHAR(64)` | yes |  |
| `jwt_iat` | `DATETIME` | yes |  |
| `jwt_exp` | `DATETIME` | yes |  |
| `latency_ms` | `INTEGER` | yes |  |
| `error_code` | `VARCHAR(40)` | yes |  |
| `client_cert_fingerprint` | `VARCHAR(64)` | yes |  |
| `attempts` | `INTEGER` | no |  |
| `next_retry_at` | `DATETIME` | yes |  |
| `last_error_at` | `DATETIME` | yes |  |

## `a2a_sessions`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `partner_id` | `VARCHAR(36)` | no | FK -> `partner_agents.id` |
| `jwt_token_hash` | `VARCHAR(200)` | no |  |
| `expires_at` | `DATETIME` | no |  |
| `created_at` | `DATETIME` | no |  |
| `revoked_at` | `DATETIME` | yes |  |
| `refresh_token_hash` | `VARCHAR(200)` | yes |  |
| `refreshed_at` | `DATETIME` | yes |  |

## `agent_jobs`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | yes | FK -> `change_requests.id` |
| `module` | `VARCHAR(64)` | no |  |
| `subtype` | `VARCHAR(128)` | yes |  |
| `status` | `VARCHAR(9)` | no |  |
| `progress_pct` | `INTEGER` | yes |  |
| `current_stage` | `VARCHAR(255)` | yes |  |
| `started_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |
| `completed_at` | `DATETIME` | yes |  |
| `started_by_user_id` | `VARCHAR(36)` | yes | FK -> `users.id` |
| `result_payload` | `JSONB` | yes |  |
| `error_message` | `TEXT` | yes |  |
| `metadata_` | `JSONB` | no |  |

## `agentic_events`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `run_id` | `VARCHAR(36)` | no | FK -> `agentic_runs.id` |
| `seq` | `INTEGER` | no |  |
| `kind` | `VARCHAR(64)` | no |  |
| `payload` | `JSON` | yes |  |
| `ts` | `DATETIME` | no |  |

## `agentic_run_repos`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `run_id` | `VARCHAR(36)` | no | FK -> `agentic_runs.id` |
| `repo_id` | `VARCHAR(36)` | no | FK -> `code_repos.id` |
| `base_commit_sha` | `VARCHAR(64)` | yes |  |
| `branch` | `VARCHAR(200)` | yes |  |
| `mr_url` | `VARCHAR(1000)` | yes |  |
| `push_state` | `VARCHAR(40)` | yes |  |
| `pushed_manifest_hash` | `VARCHAR(64)` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

## `agentic_runs`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `phase` | `VARCHAR(40)` | no |  |
| `status` | `VARCHAR(20)` | no |  |
| `kind` | `VARCHAR(8)` | no |  |
| `parent_run_id` | `VARCHAR(36)` | yes | FK -> `agentic_runs.id` |
| `workspace_run_id` | `VARCHAR(36)` | yes |  |
| `handoff_json` | `JSON` | yes |  |
| `attempts_json` | `JSON` | yes |  |
| `selected_repo_ids` | `JSON` | yes |  |
| `progress_ledger_json` | `JSON` | yes |  |
| `lease_owner` | `VARCHAR(64)` | yes |  |
| `lease_expires_at` | `DATETIME` | yes |  |
| `manifest_hash` | `VARCHAR(64)` | yes |  |
| `cancel_requested` | `BOOLEAN` | no |  |
| `platform` | `VARCHAR(20)` | yes |  |
| `error` | `TEXT` | yes |  |
| `error_code` | `VARCHAR(64)` | yes |  |
| `created_by` | `VARCHAR(36)` | yes |  |
| `last_heartbeat_at` | `DATETIME` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

## `api_fields`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `message_id` | `VARCHAR(36)` | no | FK -> `api_messages.id` |
| `parent_field_id` | `VARCHAR(36)` | yes | FK -> `api_fields.id` |
| `position` | `INTEGER` | no |  |
| `depth` | `INTEGER` | no |  |
| `tag_num` | `VARCHAR(30)` | yes |  |
| `xml_tag` | `VARCHAR(200)` | no |  |
| `is_attribute` | `BOOLEAN` | no |  |
| `xpath` | `VARCHAR(1000)` | no |  |
| `message_item` | `TEXT` | yes |  |
| `occurrence` | `VARCHAR(20)` | yes |  |
| `datatype` | `VARCHAR(60)` | yes |  |
| `length_rule` | `VARCHAR(200)` | yes |  |
| `mandatory` | `VARCHAR(5)` | yes |  |
| `condition_text` | `TEXT` | yes |  |
| `rules_ref` | `VARCHAR(500)` | yes |  |
| `pattern_rule` | `VARCHAR(500)` | yes |  |
| `enum_values` | `JSON` | yes |  |
| `constraint_sources` | `JSON` | yes |  |
| `source` | `VARCHAR(60)` | no |  |
| `status` | `VARCHAR(20)` | no |  |
| `introduced_by_change_id` | `VARCHAR(36)` | yes |  |
| `updated_by` | `VARCHAR(200)` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `api_messages`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `api_name` | `VARCHAR(200)` | no |  |
| `direction` | `VARCHAR(20)` | no |  |
| `namespace` | `VARCHAR(500)` | yes |  |
| `description` | `TEXT` | yes |  |
| `sample_xml` | `TEXT` | yes |  |
| `source` | `VARCHAR(60)` | no |  |
| `source_schema_path` | `VARCHAR(1000)` | yes |  |
| `status` | `VARCHAR(20)` | no |  |
| `introduced_by_change_id` | `VARCHAR(36)` | yes |  |
| `updated_by` | `VARCHAR(200)` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `app_configs`

| Column | Type | Null | Key |
|---|---|---|---|
| `key` | `VARCHAR(100)` | no | PK |
| `value` | `TEXT` | no |  |
| `category` | `VARCHAR(50)` | no |  |
| `is_secret` | `BOOLEAN` | no |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `approvals`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `artifact_type` | `VARCHAR(14)` | no |  |
| `artifact_id` | `VARCHAR(36)` | no |  |
| `approver_id` | `VARCHAR(36)` | yes | FK -> `users.id` |
| `reviewer_role` | `VARCHAR(100)` | yes |  |
| `status` | `VARCHAR(8)` | no |  |
| `comments` | `TEXT` | yes |  |
| `responded_at` | `DATETIME` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `assignment_status_history`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `assignment_id` | `VARCHAR(36)` | no | FK -> `change_partner_assignments.id` |
| `from_status` | `VARCHAR(50)` | yes |  |
| `to_status` | `VARCHAR(50)` | no |  |
| `actor_user_id` | `VARCHAR(36)` | yes | FK -> `users.id` |
| `actor_partner_id` | `VARCHAR(36)` | yes | FK -> `partner_agents.id` |
| `reason` | `TEXT` | yes |  |
| `created_at` | `DATETIME` | no |  |

## `blockers`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `partner_id` | `VARCHAR(36)` | no | FK -> `partner_agents.id` |
| `assignment_id` | `VARCHAR(36)` | no | FK -> `change_partner_assignments.id` |
| `blocker_id` | `VARCHAR(64)` | no |  |
| `severity` | `VARCHAR(8)` | no |  |
| `status` | `VARCHAR(8)` | no |  |
| `description` | `TEXT` | no |  |
| `impact` | `TEXT` | yes |  |
| `investigation_done` | `JSON` | yes |  |
| `options_considered` | `JSON` | yes |  |
| `requested_action_from_npci` | `TEXT` | yes |  |
| `payload` | `JSON` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `resolved_at` | `DATETIME` | yes |  |
| `resolved_by` | `VARCHAR(36)` | yes | FK -> `users.id` |
| `resolution_action` | `TEXT` | yes |  |
| `resolution_text` | `TEXT` | yes |  |
| `resolution_artifact_ref` | `VARCHAR(500)` | yes |  |

## `brd_requirements`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `label` | `VARCHAR(200)` | no |  |
| `description` | `TEXT` | yes |  |
| `category` | `VARCHAR(50)` | no |  |
| `is_mandatory` | `BOOLEAN` | no |  |
| `tolerance_config` | `JSON` | yes |  |
| `source` | `VARCHAR(10)` | no |  |
| `ai_rationale` | `TEXT` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

## `brds`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `content` | `TEXT` | yes |  |
| `file_path` | `VARCHAR(1000)` | yes |  |
| `docx_path` | `VARCHAR(500)` | yes |  |
| `version` | `INTEGER` | no |  |
| `status` | `VARCHAR(9)` | no |  |
| `source` | `VARCHAR(9)` | no |  |
| `original_filename` | `VARCHAR(500)` | yes |  |
| `uploaded_by` | `VARCHAR(36)` | yes | FK -> `users.id` |
| `uploaded_at` | `DATETIME` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `build_runs`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `phase_b_run_id` | `VARCHAR(36)` | no | FK -> `phase_b_runs.id` |
| `iteration_number` | `INTEGER` | no |  |
| `jenkins_build_number` | `INTEGER` | yes |  |
| `jenkins_job_name` | `VARCHAR(500)` | yes |  |
| `status` | `VARCHAR(7)` | no |  |
| `build_log` | `TEXT` | yes |  |
| `artifact_path` | `VARCHAR(1000)` | yes |  |
| `triggered_at` | `DATETIME` | no |  |
| `completed_at` | `DATETIME` | yes |  |
| `core_branch` | `VARCHAR(200)` | yes |  |
| `app_branch` | `VARCHAR(200)` | yes |  |
| `host` | `VARCHAR(200)` | yes |  |
| `deploy_log` | `TEXT` | yes |  |
| `startup_log` | `TEXT` | yes |  |
| `deployed_artifacts` | `JSON` | yes |  |
| `services_started` | `JSON` | yes |  |

## `cert_runs`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `partner_id` | `VARCHAR(36)` | no | FK -> `partner_agents.id` |
| `cflow_id` | `VARCHAR(64)` | yes |  |
| `run_number` | `INTEGER` | no |  |
| `total` | `INTEGER` | yes |  |
| `passed` | `INTEGER` | yes |  |
| `failed` | `INTEGER` | yes |  |
| `skipped` | `INTEGER` | yes |  |
| `status` | `VARCHAR(9)` | no |  |
| `started_at` | `DATETIME` | no |  |
| `completed_at` | `DATETIME` | yes |  |
| `completion_signed_off_at` | `DATETIME` | yes |  |

## `cert_simulator_sync_log`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `cert_engine_partner_id` | `VARCHAR(36)` | yes | FK -> `partner_agents.id` |
| `actor_user_id` | `VARCHAR(36)` | yes | FK -> `users.id` |
| `operation` | `VARCHAR(20)` | no |  |
| `summary` | `JSON` | no |  |
| `created_at` | `DATETIME` | no |  |

## `cert_test_results`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `cert_run_id` | `VARCHAR(36)` | no | FK -> `cert_runs.id` |
| `test_case_id` | `VARCHAR(100)` | yes |  |
| `direction` | `VARCHAR(15)` | no |  |
| `status` | `VARCHAR(5)` | no |  |
| `expected_response` | `JSON` | yes |  |
| `actual_response` | `JSON` | yes |  |
| `latency_ms` | `INTEGER` | yes |  |
| `created_at` | `DATETIME` | no |  |

## `cert_triage`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `cert_test_result_id` | `VARCHAR(36)` | no | FK -> `cert_test_results.id` |
| `ai_verdict` | `VARCHAR(16)` | no |  |
| `ai_reasoning` | `TEXT` | yes |  |
| `user_override` | `VARCHAR(50)` | yes |  |
| `final_verdict` | `VARCHAR(50)` | yes |  |
| `created_at` | `DATETIME` | no |  |

## `cert_waivers`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `partner_id` | `VARCHAR(36)` | no | FK -> `partner_agents.id` |
| `cflow_id` | `VARCHAR(64)` | yes |  |
| `case_id` | `VARCHAR(100)` | no |  |
| `category` | `VARCHAR(40)` | yes |  |
| `reason` | `TEXT` | yes |  |
| `status` | `VARCHAR(20)` | no |  |
| `conditions` | `TEXT` | yes |  |
| `valid_until` | `VARCHAR(40)` | yes |  |
| `decided_by` | `VARCHAR(64)` | yes |  |
| `requested_at` | `DATETIME` | no |  |
| `decided_at` | `DATETIME` | yes |  |

## `change_analyses`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `version` | `INTEGER` | no |  |
| `status` | `VARCHAR(32)` | no |  |
| `technical_analysis` | `JSON` | yes |  |
| `functional_plan` | `JSON` | yes |  |
| `flow_spec` | `JSON` | yes |  |
| `analysis_sha` | `JSON` | yes |  |
| `validated_against_brd_id` | `VARCHAR(36)` | yes |  |
| `validated_against_brd_version` | `INTEGER` | yes |  |
| `validated_against_brd_hash` | `VARCHAR(64)` | yes |  |
| `pm_ratified_by` | `VARCHAR(36)` | yes |  |
| `pm_ratified_at` | `DATETIME` | yes |  |
| `tech_ratified_by` | `VARCHAR(36)` | yes |  |
| `tech_ratified_at` | `DATETIME` | yes |  |
| `run_id` | `VARCHAR(36)` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `change_impacted_paths`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `repo_id` | `VARCHAR(36)` | no |  |
| `path` | `VARCHAR(1024)` | no |  |
| `namespace` | `VARCHAR(512)` | yes |  |
| `kind` | `VARCHAR(32)` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `change_manifests`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `run_id` | `VARCHAR(36)` | no | FK -> `agentic_runs.id` |
| `manifest_hash` | `VARCHAR(64)` | no |  |
| `selected_repo_ids` | `JSON` | yes |  |
| `per_repo` | `JSON` | yes |  |
| `operations` | `JSON` | yes |  |
| `diffs` | `JSON` | yes |  |
| `verification` | `JSON` | yes |  |
| `review` | `JSON` | yes |  |
| `plan` | `JSON` | yes |  |
| `approved_at` | `DATETIME` | yes |  |
| `approved_by` | `VARCHAR(36)` | yes | FK -> `users.id` |
| `created_at` | `DATETIME` | no |  |

## `change_partner_assignments`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `partner_id` | `VARCHAR(36)` | no | FK -> `partner_agents.id` |
| `status` | `VARCHAR(23)` | no |  |
| `assigned_at` | `DATETIME` | no |  |
| `blocked_at` | `DATETIME` | yes |  |
| `blocked_reason` | `TEXT` | yes |  |
| `acceptance_meta` | `JSON` | yes |  |

## `change_reports`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `input_hash` | `VARCHAR(64)` | no |  |
| `content` | `JSON` | yes |  |
| `created_at` | `DATETIME` | no |  |

## `change_request_contexts`

| Column | Type | Null | Key |
|---|---|---|---|
| `change_request_id` | `VARCHAR(36)` | no | PK, FK -> `change_requests.id` |
| `taxonomy_primary` | `VARCHAR(64)` | yes |  |
| `taxonomy_labels` | `JSONB` | yes |  |
| `taxonomy_confidence` | `FLOAT` | yes |  |
| `taxonomy_rationale` | `TEXT` | yes |  |
| `retrieved_chunks` | `JSONB` | yes |  |
| `proposals` | `JSONB` | yes |  |
| `proposals_confidence` | `VARCHAR(32)` | yes |  |
| `parties_inference` | `JSONB` | yes |  |
| `last_refreshed_at` | `DATETIME` | no |  |
| `source_version` | `INTEGER` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `change_requests`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `title` | `VARCHAR(500)` | yes |  |
| `initial_prompt` | `TEXT` | no |  |
| `enhanced_prompt` | `TEXT` | yes |  |
| `source_doc_name` | `VARCHAR(500)` | yes |  |
| `source_doc_text` | `TEXT` | yes |  |
| `status` | `VARCHAR(18)` | no |  |
| `created_by` | `VARCHAR(36)` | no | FK -> `users.id` |
| `negotiation_finalized_at` | `DATETIME` | yes |  |
| `negotiation_version` | `INTEGER` | no |  |
| `agentic_enabled` | `BOOLEAN` | no |  |
| `negotiation_frozen_at` | `DATETIME` | yes |  |
| `workflow_version` | `INTEGER` | no |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `clarifications`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `version` | `INTEGER` | no |  |
| `blocking_gap_keys` | `JSONB` | yes |  |
| `assumed_gaps` | `JSONB` | yes |  |
| `questions` | `JSONB` | yes |  |
| `answers` | `JSONB` | yes |  |
| `status` | `VARCHAR(16)` | no |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `code_iterations`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `phase_b_run_id` | `VARCHAR(36)` | no | FK -> `phase_b_runs.id` |
| `iteration_number` | `INTEGER` | no |  |
| `generated_output` | `TEXT` | yes |  |
| `files_changed` | `JSON` | yes |  |
| `user_feedback` | `TEXT` | yes |  |
| `trigger` | `VARCHAR(20)` | no |  |
| `approved` | `BOOLEAN` | no |  |
| `created_at` | `DATETIME` | no |  |

## `code_plans`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `phase_b_run_id` | `VARCHAR(36)` | yes | FK -> `phase_b_runs.id` |
| `status` | `VARCHAR(8)` | no |  |
| `plan_data` | `JSON` | no |  |
| `reviewer_comments` | `TEXT` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

## `code_repo_state`

| Column | Type | Null | Key |
|---|---|---|---|
| `repo_id` | `VARCHAR(36)` | no | PK |
| `last_ingested_sha` | `VARCHAR(64)` | yes |  |
| `last_ingested_at` | `DATETIME` | yes |  |
| `last_ingested_branch` | `VARCHAR(255)` | yes |  |

## `code_repos`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `label` | `VARCHAR(200)` | no |  |
| `gitlab_url` | `VARCHAR(500)` | yes |  |
| `gitlab_repo` | `VARCHAR(500)` | no |  |
| `gitlab_branch` | `VARCHAR(200)` | no |  |
| `last_indexed_at` | `DATETIME` | yes |  |
| `files_count` | `INTEGER` | no |  |
| `chunks_count` | `INTEGER` | no |  |
| `is_registry_baseline` | `BOOLEAN` | no |  |
| `role` | `VARCHAR(20)` | yes |  |
| `depends_on` | `JSON` | yes |  |
| `locations` | `JSON` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `code_review_results`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `code_iteration_id` | `VARCHAR(36)` | no | FK -> `code_iterations.id` |
| `status` | `VARCHAR(12)` | no |  |
| `issues` | `JSON` | yes |  |
| `created_at` | `DATETIME` | no |  |

## `conversations`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `module` | `VARCHAR(15)` | no |  |
| `role` | `VARCHAR(9)` | no |  |
| `content` | `TEXT` | no |  |
| `metadata` | `JSON` | yes |  |
| `created_by` | `VARCHAR(36)` | yes | FK -> `users.id` |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `counter_proposals`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `partner_id` | `VARCHAR(36)` | no | FK -> `partner_agents.id` |
| `assignment_id` | `VARCHAR(36)` | no | FK -> `change_partner_assignments.id` |
| `counter_proposal_id` | `VARCHAR(64)` | no |  |
| `status` | `VARCHAR(14)` | no |  |
| `originator` | `VARCHAR(20)` | no |  |
| `negotiation_round` | `INTEGER` | no |  |
| `justification` | `TEXT` | no |  |
| `valid_until` | `DATETIME` | yes |  |
| `payload` | `JSON` | yes |  |
| `request_category` | `VARCHAR(50)` | yes |  |
| `brd_classification` | `VARCHAR(30)` | yes |  |
| `auto_disposition` | `VARCHAR(30)` | no |  |
| `cluster_id` | `VARCHAR(36)` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `resolved_at` | `DATETIME` | yes |  |
| `resolved_by` | `VARCHAR(36)` | yes | FK -> `users.id` |
| `resolution_text` | `TEXT` | yes |  |

## `decision_ledger_entries`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `question_key` | `VARCHAR(128)` | no |  |
| `kind` | `VARCHAR(32)` | no |  |
| `question` | `TEXT` | yes |  |
| `options` | `JSON` | yes |  |
| `chosen` | `TEXT` | yes |  |
| `evidence` | `JSON` | yes |  |
| `directive` | `TEXT` | yes |  |
| `decided_by` | `VARCHAR(36)` | yes |  |
| `decided_at` | `DATETIME` | yes |  |
| `decided_against` | `JSON` | yes |  |
| `supersedes_id` | `VARCHAR(36)` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `decline_specs`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `spec_json` | `JSONB` | no |  |
| `version` | `INTEGER` | no |  |
| `status` | `VARCHAR(8)` | no |  |
| `approved_by` | `VARCHAR(36)` | yes | FK -> `users.id` |
| `approved_at` | `DATETIME` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `deployment_runs`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `phase_b_run_id` | `VARCHAR(36)` | no | FK -> `phase_b_runs.id` |
| `build_run_id` | `VARCHAR(36)` | yes | FK -> `build_runs.id` |
| `iteration_number` | `INTEGER` | no |  |
| `target_server` | `VARCHAR(500)` | yes |  |
| `status` | `VARCHAR(7)` | no |  |
| `deploy_log` | `TEXT` | yes |  |
| `health_check_url` | `VARCHAR(500)` | yes |  |
| `health_check_passed` | `BOOLEAN` | yes |  |
| `triggered_at` | `DATETIME` | no |  |
| `completed_at` | `DATETIME` | yes |  |

## `doc_code_links`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `doc_chunk_id` | `VARCHAR(36)` | no | FK -> `document_chunks.id` |
| `symbol_chunk_id` | `VARCHAR(36)` | no | FK -> `document_chunks.id` |
| `confidence` | `FLOAT` | no |  |
| `last_checked` | `DATETIME` | no |  |
| `created_at` | `DATETIME` | no |  |

## `document_chunks`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `source_file` | `VARCHAR(1000)` | no |  |
| `doc_category` | `VARCHAR(100)` | no |  |
| `content` | `TEXT` | no |  |
| `embedding` | `VECTOR(768)` | yes |  |
| `chunk_index` | `INTEGER` | no |  |
| `metadata` | `JSON` | yes |  |
| `symbol_kind` | `VARCHAR(50)` | yes |  |
| `symbol_name` | `VARCHAR(500)` | yes |  |
| `signature` | `TEXT` | yes |  |
| `line_start` | `INTEGER` | yes |  |
| `line_end` | `INTEGER` | yes |  |
| `language` | `VARCHAR(30)` | yes |  |
| `view_kind` | `VARCHAR(20)` | yes |  |
| `parent_symbol_id` | `VARCHAR(36)` | yes |  |
| `title_breadcrumb` | `VARCHAR(1000)` | yes |  |
| `last_modified` | `DATETIME` | yes |  |
| `author` | `VARCHAR(200)` | yes |  |
| `product_area` | `VARCHAR(100)` | yes |  |
| `freshness_score` | `FLOAT` | yes |  |
| `deprecated` | `BOOLEAN` | yes |  |
| `parent_chunk_id` | `VARCHAR(36)` | yes |  |
| `imports` | `JSON` | yes |  |
| `inherits` | `VARCHAR(500)` | yes |  |
| `implements` | `JSON` | yes |  |
| `calls` | `JSON` | yes |  |
| `called_by` | `JSON` | yes |  |
| `cross_file_calls` | `JSON` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `document_reconciliations`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `doc_kind` | `VARCHAR(32)` | no |  |
| `doc_id` | `VARCHAR(36)` | yes |  |
| `doc_version` | `INTEGER` | yes |  |
| `plan_version_before` | `INTEGER` | yes |  |
| `status` | `VARCHAR(16)` | no |  |
| `conflicts` | `JSON` | yes |  |
| `resolutions` | `JSON` | yes |  |
| `grounding` | `JSON` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `emergency_issues`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `partner_id` | `VARCHAR(36)` | no | FK -> `partner_agents.id` |
| `issue_id` | `VARCHAR(64)` | yes |  |
| `severity` | `VARCHAR(16)` | no |  |
| `status` | `VARCHAR(16)` | no |  |
| `title` | `VARCHAR(300)` | no |  |
| `description` | `TEXT` | no |  |
| `npci_resolution_text` | `TEXT` | yes |  |
| `resolved_by` | `VARCHAR(36)` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `resolved_at` | `DATETIME` | yes |  |

## `escalation_tickets`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `partner_id` | `VARCHAR(36)` | no | FK -> `partner_agents.id` |
| `a2a_message_id` | `VARCHAR(36)` | yes | FK -> `a2a_messages.id` |
| `cluster_id` | `VARCHAR(36)` | yes |  |
| `team` | `VARCHAR(16)` | no |  |
| `status` | `VARCHAR(16)` | no |  |
| `question_text` | `TEXT` | no |  |
| `escalation_reason` | `TEXT` | yes |  |
| `ai_suggestion` | `TEXT` | yes |  |
| `ai_comment_draft` | `TEXT` | yes |  |
| `team_response_text` | `TEXT` | yes |  |
| `responded_by` | `VARCHAR(36)` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `responded_at` | `DATETIME` | yes |  |

## `eval_policy_audit`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `checkpoint_id` | `VARCHAR(128)` | no |  |
| `old_policy_mode` | `VARCHAR(32)` | no |  |
| `new_policy_mode` | `VARCHAR(32)` | no |  |
| `actor_user_id` | `VARCHAR(36)` | yes | FK -> `users.id` |
| `actor_username` | `VARCHAR(128)` | no |  |
| `reason` | `TEXT` | no |  |
| `app_env` | `VARCHAR(32)` | no |  |
| `created_at` | `DATETIME` | no |  |

## `eval_verdicts`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `checkpoint_id` | `VARCHAR(128)` | no |  |
| `from_stage` | `VARCHAR(64)` | no |  |
| `to_stage` | `VARCHAR(64)` | no |  |
| `verdict` | `VARCHAR(16)` | no |  |
| `passed` | `BOOLEAN` | no |  |
| `policy_mode` | `VARCHAR(32)` | no |  |
| `confidence` | `FLOAT` | yes |  |
| `scores_json` | `JSONB` | no |  |
| `hard_fail_codes` | `JSONB` | no |  |
| `warn_codes` | `JSONB` | no |  |
| `reasons_json` | `JSONB` | no |  |
| `source_artifact_ids` | `JSONB` | no |  |
| `target_artifact_ids` | `JSONB` | no |  |
| `rubric_version` | `VARCHAR(64)` | no |  |
| `deterministic_version` | `VARCHAR(64)` | no |  |
| `critic_model` | `VARCHAR(128)` | yes |  |
| `judge_model` | `VARCHAR(128)` | yes |  |
| `latency_ms` | `INTEGER` | no |  |
| `retry_recommended` | `BOOLEAN` | no |  |
| `is_override` | `BOOLEAN` | no |  |
| `override_actor` | `VARCHAR(128)` | yes |  |
| `override_reason` | `TEXT` | yes |  |
| `previous_verdict_id` | `VARCHAR(36)` | yes | FK -> `eval_verdicts.id` |
| `created_at` | `DATETIME` | no |  |

## `feedback`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `module` | `VARCHAR(100)` | no |  |
| `artifact_id` | `VARCHAR(36)` | yes |  |
| `content` | `TEXT` | no |  |
| `created_by` | `VARCHAR(36)` | no | FK -> `users.id` |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `flow_context`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `repo_id` | `VARCHAR(36)` | no | FK -> `code_repos.id` |
| `summary` | `TEXT` | yes |  |
| `transaction_apis` | `JSON` | yes |  |
| `meta_apis` | `JSON` | yes |  |
| `flows` | `JSON` | yes |  |
| `entry_points` | `JSON` | yes |  |
| `base_commit_sha` | `VARCHAR(64)` | yes |  |
| `generated_at` | `DATETIME` | no |  |

## `git_events`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `phase_b_run_id` | `VARCHAR(36)` | no | FK -> `phase_b_runs.id` |
| `branch_name` | `VARCHAR(500)` | yes |  |
| `commit_sha` | `VARCHAR(100)` | yes |  |
| `mr_url` | `VARCHAR(1000)` | yes |  |
| `mr_iid` | `INTEGER` | yes |  |
| `status` | `VARCHAR(14)` | no |  |
| `created_at` | `DATETIME` | no |  |

## `governance_skills`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `skill_type` | `VARCHAR(16)` | no |  |
| `version` | `INTEGER` | no |  |
| `name` | `VARCHAR(120)` | no |  |
| `enabled` | `BOOLEAN` | no |  |
| `content` | `TEXT` | no |  |
| `checksum` | `VARCHAR(64)` | no |  |
| `filename` | `VARCHAR(255)` | yes |  |
| `bundle_bytes` | `BLOB` | yes |  |
| `bundle_sha256` | `VARCHAR(64)` | yes |  |
| `bundle_filename` | `VARCHAR(255)` | yes |  |
| `manifest_json` | `JSON` | yes |  |
| `exec_manifest_json` | `JSON` | yes |  |
| `safety_warnings_json` | `JSON` | yes |  |
| `provenance_json` | `JSON` | yes |  |
| `smoke_status` | `VARCHAR(16)` | yes |  |
| `smoke_detail_json` | `JSON` | yes |  |
| `rules_json` | `JSON` | yes |  |
| `uploaded_by` | `VARCHAR(36)` | yes |  |
| `created_at` | `DATETIME` | no |  |

## `is_review_results`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `code_iteration_id` | `VARCHAR(36)` | no | FK -> `code_iterations.id` |
| `status` | `VARCHAR(12)` | no |  |
| `findings` | `JSON` | yes |  |
| `created_at` | `DATETIME` | no |  |

## `kit_publications`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `negotiation_version` | `INTEGER` | no |  |
| `envelope` | `JSONB` | no |  |
| `envelope_sha256` | `VARCHAR(64)` | no |  |
| `source_doc_versions` | `JSONB` | no |  |
| `revision_reason` | `TEXT` | yes |  |
| `resolver_action` | `VARCHAR(50)` | yes |  |
| `published_at` | `DATETIME` | no |  |
| `published_by` | `VARCHAR(36)` | yes | FK -> `users.id` |

## `kit_revision_plans`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `target_version` | `INTEGER` | no |  |
| `status` | `VARCHAR(20)` | no |  |
| `items` | `JSON` | yes |  |
| `summary` | `TEXT` | yes |  |
| `created_by` | `VARCHAR(36)` | yes |  |
| `updated_by` | `VARCHAR(36)` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `llm_usage_records`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `ts` | `DATETIME` | no |  |
| `change_request_id` | `VARCHAR(36)` | yes |  |
| `run_id` | `VARCHAR(36)` | yes |  |
| `kind` | `VARCHAR(32)` | yes |  |
| `section` | `VARCHAR(64)` | yes |  |
| `model` | `VARCHAR(80)` | yes |  |
| `input_tokens` | `INTEGER` | no |  |
| `output_tokens` | `INTEGER` | no |  |
| `cache_read_tokens` | `INTEGER` | no |  |
| `cache_write_tokens` | `INTEGER` | no |  |
| `cost_usd` | `FLOAT` | yes |  |

## `module_context`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `repo_id` | `VARCHAR(36)` | no | FK -> `code_repos.id` |
| `module_path` | `VARCHAR(1000)` | no |  |
| `parent_module_path` | `VARCHAR(1000)` | yes |  |
| `depth` | `INTEGER` | no |  |
| `summary` | `TEXT` | yes |  |
| `key_types` | `JSON` | yes |  |
| `entry_points` | `JSON` | yes |  |
| `functional_flow` | `TEXT` | yes |  |
| `conventions` | `TEXT` | yes |  |
| `gotchas` | `TEXT` | yes |  |
| `why` | `TEXT` | yes |  |
| `java_version` | `VARCHAR(20)` | yes |  |
| `depends_on` | `JSON` | yes |  |
| `base_commit_sha` | `VARCHAR(64)` | yes |  |
| `generated_at` | `DATETIME` | no |  |

## `negotiation_cluster_members`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `cluster_id` | `VARCHAR(36)` | no | FK -> `negotiation_clusters.id` |
| `counter_proposal_id` | `VARCHAR(36)` | no | FK -> `counter_proposals.id` |
| `partner_id` | `VARCHAR(36)` | no | FK -> `partner_agents.id` |
| `added_at` | `DATETIME` | no |  |

## `negotiation_clusters`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `cluster_key` | `VARCHAR(200)` | no |  |
| `category` | `VARCHAR(50)` | no |  |
| `topic_summary` | `VARCHAR(500)` | yes |  |
| `partner_count` | `INTEGER` | no |  |
| `ai_summary` | `TEXT` | yes |  |
| `ai_recommendation` | `VARCHAR(20)` | yes |  |
| `confidence_score` | `FLOAT` | yes |  |
| `pm_decision` | `VARCHAR(20)` | no |  |
| `pm_decision_text` | `TEXT` | yes |  |
| `pm_modified_value` | `JSON` | yes |  |
| `pm_decided_at` | `DATETIME` | yes |  |
| `pm_decided_by` | `VARCHAR(36)` | yes | FK -> `users.id` |
| `conflict_with_cluster_id` | `VARCHAR(36)` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

## `negotiation_messages`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `thread_id` | `VARCHAR(36)` | no | FK -> `negotiation_threads.id` |
| `role` | `VARCHAR(11)` | no |  |
| `content` | `TEXT` | no |  |
| `ai_draft` | `TEXT` | yes |  |
| `approved_by` | `VARCHAR(36)` | yes |  |
| `correlation_id` | `VARCHAR(36)` | yes |  |
| `counter_proposal_id` | `VARCHAR(36)` | yes | FK -> `counter_proposals.id` |
| `blocker_id` | `VARCHAR(36)` | yes | FK -> `blockers.id` |
| `event_kind` | `VARCHAR(40)` | yes |  |
| `created_at` | `DATETIME` | no |  |

## `negotiation_round_states`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `partner_id` | `VARCHAR(36)` | no | FK -> `partner_agents.id` |
| `round_number` | `INTEGER` | no |  |
| `started_at` | `DATETIME` | no |  |
| `deadline_at` | `DATETIME` | no |  |
| `status` | `VARCHAR(30)` | no |  |
| `closed_at` | `DATETIME` | yes |  |
| `silent_acceptance_cp_id` | `VARCHAR(36)` | yes |  |
| `created_at` | `DATETIME` | no |  |

## `negotiation_threads`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `partner_id` | `VARCHAR(36)` | no | FK -> `partner_agents.id` |
| `kind` | `VARCHAR(20)` | no |  |
| `status` | `VARCHAR(8)` | no |  |
| `created_at` | `DATETIME` | no |  |

## `notifications`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `user_id` | `VARCHAR(36)` | no | FK -> `users.id` |
| `title` | `VARCHAR(500)` | no |  |
| `message` | `TEXT` | no |  |
| `type` | `VARCHAR(19)` | no |  |
| `related_id` | `VARCHAR(36)` | yes |  |
| `is_read` | `BOOLEAN` | no |  |
| `email_sent` | `BOOLEAN` | no |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `npci_policy`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `INTEGER` | no | PK |
| `content` | `TEXT` | no |  |
| `updated_by` | `VARCHAR(36)` | yes |  |
| `updated_at` | `DATETIME` | no |  |

## `partner_agents`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `name` | `VARCHAR(200)` | no |  |
| `partner_type` | `JSON` | no |  |
| `endpoint_url` | `VARCHAR(1000)` | yes |  |
| `api_key` | `VARCHAR(200)` | yes |  |
| `api_key_hash` | `VARCHAR(200)` | yes |  |
| `cert_agent_bank_id` | `VARCHAR(50)` | yes |  |
| `status` | `VARCHAR(9)` | no |  |
| `agent_card_url` | `VARCHAR(1000)` | yes |  |
| `metadata` | `JSON` | yes |  |
| `protocol_version` | `VARCHAR(20)` | no |  |
| `jwt_signing_secret` | `VARCHAR(128)` | yes |  |
| `signing_secret` | `VARCHAR(128)` | yes |  |
| `tls_tier` | `VARCHAR(20)` | no |  |
| `client_cert_fingerprint` | `VARCHAR(64)` | yes |  |
| `ssl_verify` | `BOOLEAN` | yes |  |
| `ca_cert_pem` | `TEXT` | yes |  |
| `max_inline_attachment_bytes` | `INTEGER` | yes |  |
| `allowed_cidrs` | `JSON` | yes |  |
| `rate_limit_rps` | `INTEGER` | no |  |
| `previous_jwt_signing_secret` | `VARCHAR(128)` | yes |  |
| `previous_signing_secret` | `VARCHAR(128)` | yes |  |
| `secret_rotated_at` | `DATETIME` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `partner_progress`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `assignment_id` | `VARCHAR(36)` | no | FK -> `change_partner_assignments.id` |
| `step` | `VARCHAR(17)` | no |  |
| `notes` | `TEXT` | yes |  |
| `reported_at` | `DATETIME` | no |  |

## `phase_b_run_repos`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `run_id` | `VARCHAR(36)` | no | FK -> `phase_b_runs.id` |
| `repo_id` | `VARCHAR(36)` | no | FK -> `code_repos.id` |
| `branch` | `VARCHAR(200)` | no |  |
| `mr_url` | `VARCHAR(1000)` | yes |  |
| `mr_iid` | `INTEGER` | yes |  |
| `mr_state` | `VARCHAR(40)` | yes |  |
| `pushed_content_hash` | `VARCHAR(64)` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

## `phase_b_runs`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `status` | `VARCHAR(11)` | no |  |
| `current_step` | `VARCHAR(11)` | no |  |
| `iteration_count` | `INTEGER` | no |  |
| `gitlab_repo` | `VARCHAR(500)` | yes |  |
| `gitlab_branch` | `VARCHAR(200)` | yes |  |
| `started_at` | `DATETIME` | no |  |
| `completed_at` | `DATETIME` | yes |  |

## `product_canvases`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `content` | `TEXT` | yes |  |
| `file_path` | `VARCHAR(1000)` | yes |  |
| `docx_path` | `VARCHAR(500)` | yes |  |
| `version` | `INTEGER` | no |  |
| `status` | `VARCHAR(8)` | no |  |
| `approved_by` | `VARCHAR(36)` | yes | FK -> `users.id` |
| `approved_at` | `DATETIME` | yes |  |
| `source` | `VARCHAR(9)` | no |  |
| `original_filename` | `VARCHAR(500)` | yes |  |
| `uploaded_by` | `VARCHAR(36)` | yes | FK -> `users.id` |
| `uploaded_at` | `DATETIME` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `product_kit_documents`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `doc_type` | `VARCHAR(17)` | no |  |
| `content` | `TEXT` | yes |  |
| `file_path` | `VARCHAR(1000)` | yes |  |
| `docx_path` | `VARCHAR(500)` | yes |  |
| `pptx_path` | `VARCHAR(500)` | yes |  |
| `version` | `INTEGER` | no |  |
| `negotiation_version` | `INTEGER` | no |  |
| `status` | `VARCHAR(8)` | no |  |
| `approved_by` | `VARCHAR(36)` | yes | FK -> `users.id` |
| `approved_at` | `DATETIME` | yes |  |
| `source` | `VARCHAR(9)` | no |  |
| `original_filename` | `VARCHAR(500)` | yes |  |
| `uploaded_by` | `VARCHAR(36)` | yes | FK -> `users.id` |
| `uploaded_at` | `DATETIME` | yes |  |
| `script_json` | `JSONB` | yes |  |
| `video_provider` | `VARCHAR(40)` | yes |  |
| `video_model` | `VARCHAR(80)` | yes |  |
| `video_duration_sec` | `INTEGER` | yes |  |
| `override_path` | `VARCHAR(500)` | yes |  |
| `override_filename` | `VARCHAR(255)` | yes |  |
| `override_sha256` | `VARCHAR(64)` | yes |  |
| `override_size_bytes` | `BIGINT` | yes |  |
| `override_mime_type` | `VARCHAR(120)` | yes |  |
| `override_uploaded_at` | `DATETIME` | yes |  |
| `override_uploaded_by` | `VARCHAR(36)` | yes | FK -> `users.id` |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `repo_path_context`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `repo_id` | `VARCHAR(36)` | no | FK -> `code_repos.id` |
| `path` | `VARCHAR(1000)` | no |  |
| `content` | `TEXT` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

## `research_outputs`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `market_research` | `TEXT` | yes |  |
| `product_knowledge` | `TEXT` | yes |  |
| `rbi_compliance` | `TEXT` | yes |  |
| `combined_report` | `TEXT` | yes |  |
| `version` | `INTEGER` | no |  |
| `status` | `VARCHAR(8)` | no |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `resolver_recommendations`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `partner_id` | `VARCHAR(36)` | no | FK -> `partner_agents.id` |
| `a2a_message_id` | `VARCHAR(36)` | no | FK -> `a2a_messages.id` |
| `message_type` | `VARCHAR(20)` | no |  |
| `version` | `INTEGER` | no |  |
| `content` | `TEXT` | no |  |
| `model_used` | `VARCHAR(100)` | yes |  |
| `created_at` | `DATETIME` | no |  |

## `review_findings`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `run_id` | `VARCHAR(36)` | no | FK -> `agentic_runs.id` |
| `round` | `INTEGER` | no |  |
| `severity` | `VARCHAR(20)` | yes |  |
| `category` | `VARCHAR(20)` | yes |  |
| `repo_id` | `VARCHAR(36)` | yes |  |
| `file` | `VARCHAR(1000)` | yes |  |
| `line` | `INTEGER` | yes |  |
| `why` | `TEXT` | yes |  |
| `suggested_fix` | `TEXT` | yes |  |
| `blocking` | `BOOLEAN` | no |  |
| `reviewer_model` | `VARCHAR(100)` | yes |  |
| `created_at` | `DATETIME` | no |  |

## `tech_specs`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `content` | `TEXT` | yes |  |
| `file_path` | `VARCHAR(1000)` | yes |  |
| `docx_path` | `VARCHAR(500)` | yes |  |
| `version` | `INTEGER` | no |  |
| `status` | `VARCHAR(8)` | no |  |
| `approved_by` | `VARCHAR(36)` | yes | FK -> `users.id` |
| `approved_at` | `DATETIME` | yes |  |
| `source` | `VARCHAR(9)` | no |  |
| `original_filename` | `VARCHAR(500)` | yes |  |
| `uploaded_by` | `VARCHAR(36)` | yes | FK -> `users.id` |
| `uploaded_at` | `DATETIME` | yes |  |
| `override_path` | `VARCHAR(500)` | yes |  |
| `override_filename` | `VARCHAR(255)` | yes |  |
| `override_sha256` | `VARCHAR(64)` | yes |  |
| `override_size_bytes` | `BIGINT` | yes |  |
| `override_mime_type` | `VARCHAR(120)` | yes |  |
| `override_uploaded_at` | `DATETIME` | yes |  |
| `override_uploaded_by` | `VARCHAR(36)` | yes | FK -> `users.id` |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `uat_test_cases`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `phase_b_run_id` | `VARCHAR(36)` | no | FK -> `phase_b_runs.id` |
| `suite_version` | `INTEGER` | no |  |
| `test_id` | `VARCHAR(50)` | yes |  |
| `category` | `VARCHAR(11)` | no |  |
| `title` | `VARCHAR(500)` | no |  |
| `description` | `TEXT` | yes |  |
| `preconditions` | `TEXT` | yes |  |
| `http_method` | `VARCHAR(10)` | yes |  |
| `endpoint` | `VARCHAR(500)` | yes |  |
| `request_headers` | `JSON` | yes |  |
| `request_payload` | `JSON` | yes |  |
| `expected_status` | `INTEGER` | yes |  |
| `expected_response` | `JSON` | yes |  |
| `pass_criteria` | `TEXT` | yes |  |
| `is_active` | `BOOLEAN` | no |  |
| `created_at` | `DATETIME` | no |  |

## `uat_test_results`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `test_run_id` | `VARCHAR(36)` | no | FK -> `uat_test_runs.id` |
| `test_case_id` | `VARCHAR(36)` | no | FK -> `uat_test_cases.id` |
| `status` | `VARCHAR(5)` | no |  |
| `actual_status` | `INTEGER` | yes |  |
| `actual_response` | `JSON` | yes |  |
| `latency_ms` | `INTEGER` | yes |  |
| `error_message` | `TEXT` | yes |  |
| `executed_at` | `DATETIME` | no |  |

## `uat_test_runs`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `phase_b_run_id` | `VARCHAR(36)` | no | FK -> `phase_b_runs.id` |
| `suite_version` | `INTEGER` | no |  |
| `iteration_number` | `INTEGER` | no |  |
| `base_url` | `VARCHAR(500)` | yes |  |
| `total` | `INTEGER` | yes |  |
| `passed` | `INTEGER` | yes |  |
| `failed` | `INTEGER` | yes |  |
| `skipped` | `INTEGER` | yes |  |
| `status` | `VARCHAR(9)` | no |  |
| `started_at` | `DATETIME` | no |  |
| `completed_at` | `DATETIME` | yes |  |

## `uat_triage_results`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `test_run_id` | `VARCHAR(36)` | no | FK -> `uat_test_runs.id` |
| `test_result_id` | `VARCHAR(36)` | no | FK -> `uat_test_results.id` |
| `verdict` | `VARCHAR(15)` | no |  |
| `ai_reasoning` | `TEXT` | yes |  |
| `user_override` | `VARCHAR(15)` | yes |  |
| `final_verdict` | `VARCHAR(15)` | yes |  |
| `created_at` | `DATETIME` | no |  |

## `upi_xml_templates`

| Column | Type | Null | Key |
|---|---|---|---|
| `api_name` | `VARCHAR(64)` | no | PK |
| `flow_code` | `VARCHAR(32)` | no |  |
| `xml_template` | `TEXT` | no |  |
| `placeholders_used` | `JSON` | no |  |
| `source` | `VARCHAR(16)` | no |  |
| `approved_by` | `VARCHAR(64)` | yes |  |
| `approved_at` | `DATETIME` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

## `user_roles`

| Column | Type | Null | Key |
|---|---|---|---|
| `user_id` | `VARCHAR(36)` | no | PK, FK -> `users.id` |
| `role` | `VARCHAR(16)` | no | PK |

## `users`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `username` | `VARCHAR(100)` | no |  |
| `email` | `VARCHAR(255)` | no |  |
| `password_hash` | `VARCHAR(255)` | no |  |
| `full_name` | `VARCHAR(255)` | yes |  |
| `role` | `VARCHAR(16)` | no |  |
| `is_active` | `BOOLEAN` | no |  |
| `auth_source` | `VARCHAR(16)` | no |  |
| `mfa_enabled` | `BOOLEAN` | no |  |
| `mfa_secret` | `VARCHAR(255)` | yes |  |
| `mfa_backup_codes` | `JSON` | yes |  |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

## `verification_runs`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `run_id` | `VARCHAR(36)` | no | FK -> `agentic_runs.id` |
| `round` | `INTEGER` | no |  |
| `exit_code` | `INTEGER` | yes |  |
| `timed_out` | `BOOLEAN` | no |  |
| `smoke_passed` | `BOOLEAN` | yes |  |
| `raw_output` | `TEXT` | yes |  |
| `llm_reasoning` | `TEXT` | yes |  |
| `decision` | `VARCHAR(20)` | yes |  |
| `plan` | `JSON` | yes |  |
| `gates` | `JSON` | yes |  |
| `created_at` | `DATETIME` | no |  |

## `xsd_java_links`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `repo_id` | `VARCHAR(36)` | no | FK -> `code_repos.id` |
| `node_id` | `VARCHAR(36)` | yes | FK -> `xsd_schema_nodes.id` |
| `xpath` | `VARCHAR(1000)` | yes |  |
| `symbol_chunk_id_or_path` | `VARCHAR(1000)` | yes |  |
| `source` | `VARCHAR(40)` | yes |  |
| `confidence` | `FLOAT` | yes |  |
| `base_commit_sha` | `VARCHAR(64)` | yes |  |
| `evidence_json` | `JSON` | yes |  |
| `created_at` | `DATETIME` | no |  |

## `xsd_schema_edges`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `from_node_id` | `VARCHAR(36)` | no | FK -> `xsd_schema_nodes.id` |
| `to_node_id` | `VARCHAR(36)` | yes | FK -> `xsd_schema_nodes.id` |
| `edge_type` | `VARCHAR(20)` | no |  |
| `schema_location` | `VARCHAR(1000)` | yes |  |
| `namespace` | `VARCHAR(500)` | yes |  |

## `xsd_schema_nodes`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `repo_id` | `VARCHAR(36)` | no | FK -> `code_repos.id` |
| `path` | `VARCHAR(1000)` | no |  |
| `target_namespace` | `VARCHAR(500)` | yes |  |
| `base_commit_sha` | `VARCHAR(64)` | yes |  |
| `content_hash` | `VARCHAR(64)` | yes |  |
| `created_at` | `DATETIME` | no |  |

## `xsds`

| Column | Type | Null | Key |
|---|---|---|---|
| `id` | `VARCHAR(36)` | no | PK |
| `change_request_id` | `VARCHAR(36)` | no | FK -> `change_requests.id` |
| `content` | `TEXT` | yes |  |
| `file_path` | `VARCHAR(1000)` | yes |  |
| `docx_path` | `VARCHAR(500)` | yes |  |
| `version` | `INTEGER` | no |  |
| `is_required` | `BOOLEAN` | no |  |
| `status` | `VARCHAR(10)` | no |  |
| `source` | `VARCHAR(9)` | no |  |
| `original_filename` | `VARCHAR(500)` | yes |  |
| `uploaded_by` | `VARCHAR(36)` | yes | FK -> `users.id` |
| `uploaded_at` | `DATETIME` | yes |  |
| `override_path` | `VARCHAR(500)` | yes |  |
| `override_filename` | `VARCHAR(255)` | yes |  |
| `override_sha256` | `VARCHAR(64)` | yes |  |
| `override_size_bytes` | `BIGINT` | yes |  |
| `override_mime_type` | `VARCHAR(120)` | yes |  |
| `override_uploaded_at` | `DATETIME` | yes |  |
| `override_uploaded_by` | `VARCHAR(36)` | yes | FK -> `users.id` |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | yes |  |

