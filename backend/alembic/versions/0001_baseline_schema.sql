-- No WITH SCHEMA: ag_catalog is excluded from this dump, so nothing here
-- creates it. The age extension is non-relocatable and creates ag_catalog
-- itself, which lands it in exactly the same place pg_dump recorded.
CREATE EXTENSION IF NOT EXISTS age;

COMMENT ON EXTENSION age IS 'AGE database extension';

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';

CREATE TYPE public.agent_job_status AS ENUM (
    'pending',
    'running',
    'succeeded',
    'failed',
    'cancelled'
);

CREATE TYPE public.approvalartifacttype AS ENUM (
    'brd',
    'tech_spec',
    'xsd',
    'product_canvas'
);

CREATE TYPE public.approvalstatus AS ENUM (
    'pending',
    'approved',
    'rejected'
);

CREATE TYPE public.artifactstatus AS ENUM (
    'draft',
    'approved'
);

CREATE TYPE public.artifactstatus2 AS ENUM (
    'draft',
    'approved'
);

CREATE TYPE public.artifactstatus3 AS ENUM (
    'draft',
    'approved'
);

CREATE TYPE public.artifactstatus4 AS ENUM (
    'draft',
    'approved'
);

CREATE TYPE public.blockerseverity AS ENUM (
    'critical',
    'high',
    'medium',
    'low'
);

CREATE TYPE public.blockerstatus AS ENUM (
    'open',
    'resolved',
    'wontfix'
);

CREATE TYPE public.brdstatus AS ENUM (
    'draft',
    'submitted',
    'revision',
    'approved'
);

CREATE TYPE public.buildrunstatus AS ENUM (
    'queued',
    'running',
    'success',
    'failure'
);

CREATE TYPE public.changestatus AS ENUM (
    'prompt_enhancement',
    'research',
    'canvas',
    'brd',
    'tech_spec',
    'xsd',
    'product_kit',
    'completed',
    'clarification'
);

CREATE TYPE public.conversationmodule AS ENUM (
    'prompt_enhancer',
    'researcher',
    'canvas',
    'brd',
    'tech_spec',
    'xsd',
    'product_kit'
);

CREATE TYPE public.counterproposalstatus AS ENUM (
    'open',
    'accepted',
    'rejected',
    'withdrawn',
    'countered_back'
);

CREATE TYPE public.deployrunstatus AS ENUM (
    'running',
    'success',
    'failure'
);

CREATE TYPE public.documentsource AS ENUM (
    'generated',
    'uploaded'
);

CREATE TYPE public.giteventstatus AS ENUM (
    'branch_created',
    'committed',
    'mr_raised',
    'merged'
);

CREATE TYPE public.isreviewstatus AS ENUM (
    'clean',
    'issues_found'
);

CREATE TYPE public.iterationtrigger AS ENUM (
    'initial',
    'user_feedback',
    'code_review_feedback',
    'is_review_feedback',
    'build_failure',
    'deploy_failure',
    'uat_failure'
);

CREATE TYPE public.messagerole AS ENUM (
    'user',
    'assistant'
);

CREATE TYPE public.notificationtype AS ENUM (
    'approval_request',
    'approval_done',
    'revision_ready',
    'info',
    'delivery_failed',
    'mandatory_rejection'
);

CREATE TYPE public.phasebrunstatus AS ENUM (
    'in_progress',
    'completed',
    'blocked'
);

CREATE TYPE public.phasebstep AS ENUM (
    'code_change',
    'code_review',
    'is_review',
    'git',
    'build',
    'deploy',
    'test_gen',
    'test_exec',
    'triage',
    'completed'
);

CREATE TYPE public.productkitdoctype AS ENUM (
    'product_doc',
    'product_deck',
    'promo_video',
    'explainer_video',
    'faq',
    'cert_test_cases',
    'circular',
    'manifest',
    'prototype_screens',
    'product_note'
);

CREATE TYPE public.reviewstatus AS ENUM (
    'clean',
    'issues_found'
);

CREATE TYPE public.testcasecategory AS ENUM (
    'new_feature',
    'regression'
);

CREATE TYPE public.testresultstatus AS ENUM (
    'pass',
    'fail',
    'skip',
    'error'
);

CREATE TYPE public.testrunstatus AS ENUM (
    'running',
    'completed'
);

CREATE TYPE public.triagefinalverdict AS ENUM (
    'code_bug',
    'test_case_issue',
    'env_issue'
);

CREATE TYPE public.triageuseroverride AS ENUM (
    'code_bug',
    'test_case_issue',
    'env_issue'
);

CREATE TYPE public.triageverdict AS ENUM (
    'code_bug',
    'test_case_issue',
    'env_issue'
);

CREATE TYPE public.userrole AS ENUM (
    'product_owner',
    'product_manager',
    'tech_lead',
    'infosec_reviewer',
    'risk_reviewer',
    'admin'
);

CREATE TYPE public.xsdstatus AS ENUM (
    'draft',
    'downloaded'
);

CREATE TEXT SEARCH CONFIGURATION public.simple_code (
    PARSER = pg_catalog."default" );

ALTER TEXT SEARCH CONFIGURATION public.simple_code
    ADD MAPPING FOR asciiword WITH simple;

ALTER TEXT SEARCH CONFIGURATION public.simple_code
    ADD MAPPING FOR word WITH simple;

ALTER TEXT SEARCH CONFIGURATION public.simple_code
    ADD MAPPING FOR numword WITH simple;

ALTER TEXT SEARCH CONFIGURATION public.simple_code
    ADD MAPPING FOR email WITH simple;

ALTER TEXT SEARCH CONFIGURATION public.simple_code
    ADD MAPPING FOR url WITH simple;

ALTER TEXT SEARCH CONFIGURATION public.simple_code
    ADD MAPPING FOR host WITH simple;

ALTER TEXT SEARCH CONFIGURATION public.simple_code
    ADD MAPPING FOR sfloat WITH simple;

ALTER TEXT SEARCH CONFIGURATION public.simple_code
    ADD MAPPING FOR version WITH simple;

ALTER TEXT SEARCH CONFIGURATION public.simple_code
    ADD MAPPING FOR hword_numpart WITH simple;

ALTER TEXT SEARCH CONFIGURATION public.simple_code
    ADD MAPPING FOR hword_part WITH simple;

ALTER TEXT SEARCH CONFIGURATION public.simple_code
    ADD MAPPING FOR hword_asciipart WITH simple;

ALTER TEXT SEARCH CONFIGURATION public.simple_code
    ADD MAPPING FOR numhword WITH simple;

ALTER TEXT SEARCH CONFIGURATION public.simple_code
    ADD MAPPING FOR asciihword WITH simple;

ALTER TEXT SEARCH CONFIGURATION public.simple_code
    ADD MAPPING FOR hword WITH simple;

ALTER TEXT SEARCH CONFIGURATION public.simple_code
    ADD MAPPING FOR url_path WITH simple;

ALTER TEXT SEARCH CONFIGURATION public.simple_code
    ADD MAPPING FOR file WITH simple;

ALTER TEXT SEARCH CONFIGURATION public.simple_code
    ADD MAPPING FOR "float" WITH simple;

ALTER TEXT SEARCH CONFIGURATION public.simple_code
    ADD MAPPING FOR "int" WITH simple;

ALTER TEXT SEARCH CONFIGURATION public.simple_code
    ADD MAPPING FOR uint WITH simple;

CREATE TABLE public.a2a_messages (
    id character varying(36) NOT NULL,
    change_request_id character varying(36),
    partner_id character varying(36) NOT NULL,
    direction character varying(20) NOT NULL,
    task_type character varying(50) NOT NULL,
    payload json,
    status character varying(50) DEFAULT 'sent'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    task_id_a2a character varying(64),
    task_state character varying(20),
    protocol_ver character varying(20) DEFAULT 'legacy'::character varying NOT NULL,
    caller_ip inet,
    jwt_sub character varying(64),
    jwt_iat timestamp with time zone,
    jwt_exp timestamp with time zone,
    latency_ms integer,
    error_code character varying(40),
    client_cert_fingerprint character varying(64),
    response_body jsonb,
    attempts integer DEFAULT 0 NOT NULL,
    next_retry_at timestamp with time zone,
    last_error_at timestamp with time zone,
    payload_sha256 character varying(64),
    hmac_signature character varying(64),
    hmac_key_version integer
);

CREATE TABLE public.a2a_sessions (
    id character varying(36) NOT NULL,
    partner_id character varying(36) NOT NULL,
    jwt_token_hash character varying(200) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone,
    refresh_token_hash character varying(200),
    refreshed_at timestamp with time zone
);

CREATE TABLE public.admin_action_audit (
    id character varying(36) NOT NULL,
    user_id character varying(36) NOT NULL,
    username character varying(150),
    action character varying(80) NOT NULL,
    resource_type character varying(64),
    resource_id character varying(36),
    before json,
    after json,
    ip character varying(64),
    detail text,
    http_method character varying(10),
    path character varying(500),
    status_code integer,
    source character varying(20),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone
);

CREATE TABLE public.agent_jobs (
    id character varying(36) NOT NULL,
    change_request_id character varying(36),
    module character varying(64) NOT NULL,
    subtype character varying(128),
    status public.agent_job_status DEFAULT 'pending'::public.agent_job_status NOT NULL,
    progress_pct integer,
    current_stage character varying(255),
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    started_by_user_id character varying(36),
    result_payload jsonb,
    error_message text,
    metadata_ jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE TABLE public.agentic_events (
    id character varying(36) NOT NULL,
    run_id character varying(36) NOT NULL,
    seq integer NOT NULL,
    kind character varying(64) NOT NULL,
    payload json,
    ts timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.agentic_run_repos (
    id character varying(36) NOT NULL,
    run_id character varying(36) NOT NULL,
    repo_id character varying(36) NOT NULL,
    base_commit_sha character varying(64),
    branch character varying(200),
    mr_url character varying(1000),
    push_state character varying(40),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    pushed_manifest_hash character varying(64)
);

CREATE TABLE public.agentic_runs (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    phase character varying(40) DEFAULT 'pending'::character varying NOT NULL,
    status character varying(20) DEFAULT 'active'::character varying NOT NULL,
    attempts_json json,
    selected_repo_ids json,
    lease_owner character varying(64),
    lease_expires_at timestamp with time zone,
    manifest_hash character varying(64),
    cancel_requested boolean DEFAULT false NOT NULL,
    platform character varying(20),
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    kind character varying(8) DEFAULT 'full'::character varying NOT NULL,
    parent_run_id character varying(36),
    workspace_run_id character varying(36),
    handoff_json json,
    created_by character varying(36),
    error_code character varying(64),
    last_heartbeat_at timestamp with time zone,
    progress_ledger_json json,
    tsd_version_locked integer
);

CREATE TABLE public.api_fields (
    id character varying(36) NOT NULL,
    message_id character varying(36) NOT NULL,
    parent_field_id character varying(36),
    "position" integer DEFAULT 0 NOT NULL,
    depth integer DEFAULT 0 NOT NULL,
    tag_num character varying(30),
    xml_tag character varying(200) NOT NULL,
    is_attribute boolean DEFAULT false NOT NULL,
    xpath character varying(1000) NOT NULL,
    message_item text,
    occurrence character varying(20),
    datatype character varying(60),
    length_rule character varying(200),
    mandatory character varying(5),
    condition_text text,
    rules_ref character varying(500),
    enum_values json,
    constraint_sources json,
    source character varying(60) DEFAULT 'xsd_parse'::character varying NOT NULL,
    status character varying(20) DEFAULT 'active'::character varying NOT NULL,
    introduced_by_change_id character varying(36),
    updated_by character varying(200),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone,
    pattern_rule character varying(500)
);

CREATE TABLE public.api_messages (
    id character varying(36) NOT NULL,
    api_name character varying(200) NOT NULL,
    direction character varying(20) DEFAULT 'other'::character varying NOT NULL,
    namespace character varying(500),
    description text,
    sample_xml text,
    source character varying(60) DEFAULT 'xsd_parse'::character varying NOT NULL,
    source_schema_path character varying(1000),
    status character varying(20) DEFAULT 'active'::character varying NOT NULL,
    introduced_by_change_id character varying(36),
    updated_by character varying(200),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone
);

CREATE TABLE public.app_configs (
    key character varying(100) NOT NULL,
    value text DEFAULT ''::text NOT NULL,
    category character varying(50) DEFAULT 'general'::character varying NOT NULL,
    is_secret boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone
);

CREATE TABLE public.approvals (
    id character varying(36) NOT NULL,
    artifact_type public.approvalartifacttype NOT NULL,
    artifact_id character varying(36) NOT NULL,
    approver_id character varying(36),
    status public.approvalstatus NOT NULL,
    comments text,
    responded_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone,
    reviewer_role character varying(100)
);

CREATE TABLE public.artifact_cold_storage (
    id character varying(36) NOT NULL,
    source_table character varying(64) NOT NULL,
    source_id character varying(36) NOT NULL,
    change_request_id character varying(36),
    coldstore_path character varying(500) NOT NULL,
    original_size_bytes integer,
    compressed_size_bytes integer,
    compressed_at timestamp with time zone NOT NULL,
    ready_for_archive boolean DEFAULT false NOT NULL,
    archived_at timestamp with time zone
);

CREATE TABLE public.assignment_status_history (
    id character varying(36) NOT NULL,
    assignment_id character varying(36) NOT NULL,
    from_status character varying(50),
    to_status character varying(50) NOT NULL,
    actor_user_id character varying(36),
    actor_partner_id character varying(36),
    reason text,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE TABLE public.auth_audit (
    id character varying(36) NOT NULL,
    event character varying(48) NOT NULL,
    username character varying(150),
    user_id character varying(36),
    ip character varying(64),
    detail text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone
);

CREATE TABLE public.authority_policy (
    id integer NOT NULL,
    content text DEFAULT ''::text NOT NULL,
    updated_by character varying(36),
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_authority_policy_singleton CHECK ((id = 1))
);

CREATE TABLE public.blockers (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    partner_id character varying(36) NOT NULL,
    assignment_id character varying(36) NOT NULL,
    blocker_id character varying(64) NOT NULL,
    severity public.blockerseverity NOT NULL,
    status public.blockerstatus DEFAULT 'open'::public.blockerstatus NOT NULL,
    description text NOT NULL,
    impact text,
    investigation_done jsonb,
    options_considered jsonb,
    requested_action_from_authority text,
    payload jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_at timestamp with time zone,
    resolved_by character varying(36),
    resolution_action text,
    resolution_text text,
    resolution_artifact_ref character varying(500)
);

CREATE TABLE public.brd_requirements (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    label character varying(200) NOT NULL,
    description text,
    category character varying(50) DEFAULT 'general'::character varying NOT NULL,
    is_mandatory boolean DEFAULT false NOT NULL,
    tolerance_config json,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    source character varying(10) DEFAULT 'manual'::character varying NOT NULL,
    ai_rationale text
);

CREATE TABLE public.brds (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    content text,
    file_path character varying(1000),
    version integer NOT NULL,
    status public.brdstatus NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone,
    docx_path character varying(500),
    source public.documentsource DEFAULT 'generated'::public.documentsource NOT NULL,
    original_filename character varying(500),
    uploaded_by character varying(36),
    uploaded_at timestamp with time zone
);

CREATE TABLE public.build_runs (
    id character varying(36) NOT NULL,
    phase_b_run_id character varying(36) NOT NULL,
    iteration_number integer NOT NULL,
    jenkins_build_number integer,
    jenkins_job_name character varying(500),
    status public.buildrunstatus DEFAULT 'queued'::public.buildrunstatus NOT NULL,
    build_log text,
    artifact_path character varying(1000),
    triggered_at timestamp with time zone NOT NULL,
    completed_at timestamp with time zone,
    core_branch character varying(200),
    app_branch character varying(200),
    host character varying(200),
    deploy_log text,
    startup_log text,
    deployed_artifacts json,
    services_started json,
    script_path character varying(1000)
);

CREATE TABLE public.cert_case_specs (
    id character varying(36) NOT NULL,
    cflow_id character varying(64) NOT NULL,
    run_number integer NOT NULL,
    case_id character varying(100) NOT NULL,
    variant_id character varying(36),
    api_message_id character varying(36),
    api_field_id character varying(36),
    field_path character varying(1000),
    assertion_kind character varying(20) NOT NULL,
    expected json NOT NULL,
    origin character varying(20) NOT NULL,
    wire_format character varying(20) DEFAULT 'xml'::character varying NOT NULL,
    authority_data json,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone
);

CREATE TABLE public.cert_flow_states (
    cflow_id character varying(64) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    partner_id character varying(36) NOT NULL,
    phase character varying(30) DEFAULT 'NOT_STARTED'::character varying NOT NULL,
    current_round integer DEFAULT 1 NOT NULL,
    history json NOT NULL,
    halted_reason character varying(200),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone
);

CREATE TABLE public.cert_request_variants (
    id character varying(36) NOT NULL,
    cflow_id character varying(64) NOT NULL,
    run_number integer NOT NULL,
    case_id character varying(100) NOT NULL,
    variant_id character varying(64) NOT NULL,
    api_message_id character varying(36),
    initiator character varying(10) DEFAULT 'authority'::character varying NOT NULL,
    wire_format character varying(20) DEFAULT 'xml'::character varying NOT NULL,
    input_data json,
    fixture_ref character varying(200),
    expected json NOT NULL,
    strategy character varying(30) NOT NULL,
    covered_rules json,
    is_negative boolean DEFAULT false NOT NULL,
    fault_key character varying(200),
    provenance json,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone
);

CREATE TABLE public.cert_runs (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    partner_id character varying(36) NOT NULL,
    run_number integer DEFAULT 1 NOT NULL,
    total integer,
    passed integer,
    failed integer,
    skipped integer,
    status character varying(20) DEFAULT 'running'::character varying NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    partner_acknowledged_at timestamp with time zone,
    completion_signed_off_at timestamp with time zone,
    cflow_id character varying(64),
    dispatched_by character varying(20),
    previous_run_id character varying(36),
    fix_notification_message_id character varying(36),
    pack_ref character varying(120),
    pack_id character varying(80),
    authority_mode character varying(20),
    partner_mode character varying(20),
    coverage json
);

CREATE TABLE public.cert_simulator_sync_log (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    cert_engine_partner_id character varying(36),
    actor_user_id character varying(36),
    operation character varying(20) NOT NULL,
    summary json DEFAULT '{}'::json NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE TABLE public.cert_test_results (
    id character varying(36) NOT NULL,
    cert_run_id character varying(36) NOT NULL,
    test_case_id character varying(100),
    direction character varying(30) NOT NULL,
    status character varying(20) NOT NULL,
    expected_response json,
    actual_response json,
    latency_ms integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    pack_ref character varying(120),
    pack_id character varying(80),
    authority_mode character varying(20),
    partner_mode character varying(20)
);

CREATE TABLE public.cert_triage (
    id character varying(36) NOT NULL,
    cert_test_result_id character varying(36) NOT NULL,
    ai_verdict character varying(30) NOT NULL,
    ai_reasoning text,
    user_override character varying(50),
    final_verdict character varying(50),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.cert_waivers (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    partner_id character varying(36) NOT NULL,
    cflow_id character varying(64),
    case_id character varying(100) NOT NULL,
    category character varying(40),
    reason text,
    status character varying(20) DEFAULT 'requested'::character varying NOT NULL,
    conditions text,
    valid_until character varying(40),
    decided_by character varying(64),
    requested_at timestamp with time zone NOT NULL,
    decided_at timestamp with time zone
);

CREATE TABLE public.change_analyses (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    status character varying(32) DEFAULT 'draft'::character varying NOT NULL,
    technical_analysis json,
    functional_plan json,
    flow_spec json,
    analysis_sha json,
    validated_against_brd_id character varying(36),
    validated_against_brd_version integer,
    validated_against_brd_hash character varying(64),
    pm_ratified_by character varying(36),
    pm_ratified_at timestamp with time zone,
    tech_ratified_by character varying(36),
    tech_ratified_at timestamp with time zone,
    run_id character varying(36),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone
);

CREATE TABLE public.change_impacted_paths (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    repo_id character varying(36) NOT NULL,
    path character varying(1024) NOT NULL,
    namespace character varying(512),
    kind character varying(32),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone
);

CREATE TABLE public.change_manifests (
    id character varying(36) NOT NULL,
    run_id character varying(36) NOT NULL,
    manifest_hash character varying(64) NOT NULL,
    selected_repo_ids json,
    per_repo json,
    operations json,
    verification json,
    review json,
    approved_at timestamp with time zone,
    approved_by character varying(36),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    diffs json,
    plan json
);

CREATE TABLE public.change_partner_assignments (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    partner_id character varying(36) NOT NULL,
    status character varying(30) DEFAULT 'assigned'::character varying NOT NULL,
    assigned_at timestamp with time zone DEFAULT now() NOT NULL,
    blocked_at timestamp with time zone,
    blocked_reason text,
    acceptance_meta jsonb
);

CREATE TABLE public.change_reports (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    input_hash character varying(64) NOT NULL,
    content json,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.change_request_contexts (
    change_request_id character varying(36) NOT NULL,
    taxonomy_primary character varying(64),
    taxonomy_labels jsonb,
    taxonomy_confidence double precision,
    taxonomy_rationale text,
    retrieved_chunks jsonb,
    proposals jsonb,
    proposals_confidence character varying(32),
    last_refreshed_at timestamp with time zone DEFAULT now() NOT NULL,
    source_version integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone,
    parties_inference jsonb
);

CREATE TABLE public.change_requests (
    id character varying(36) NOT NULL,
    title character varying(500),
    initial_prompt text NOT NULL,
    enhanced_prompt text,
    status public.changestatus NOT NULL,
    created_by character varying(36) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone,
    negotiation_finalized_at timestamp with time zone,
    negotiation_version integer DEFAULT 1 NOT NULL,
    negotiation_frozen_at timestamp with time zone,
    agentic_enabled boolean DEFAULT false NOT NULL,
    workflow_version integer DEFAULT 2 NOT NULL,
    source_doc_name character varying(500),
    source_doc_text text
);

CREATE TABLE public.clarifications (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    version integer NOT NULL,
    blocking_gap_keys jsonb,
    assumed_gaps jsonb,
    questions jsonb,
    answers jsonb,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone
);

CREATE TABLE public.code_iterations (
    id character varying(36) NOT NULL,
    phase_b_run_id character varying(36) NOT NULL,
    iteration_number integer NOT NULL,
    generated_output text,
    files_changed json,
    user_feedback text,
    trigger public.iterationtrigger DEFAULT 'initial'::public.iterationtrigger NOT NULL,
    approved boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone NOT NULL
);

CREATE TABLE public.code_plans (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    phase_b_run_id character varying(36),
    status character varying(20) DEFAULT 'draft'::character varying NOT NULL,
    plan_data json NOT NULL,
    reviewer_comments text,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE TABLE public.code_repo_file_state (
    id character varying(36) NOT NULL,
    repo_id character varying(36) NOT NULL,
    source_file character varying(1000) NOT NULL,
    content_hash character varying(64) NOT NULL,
    language character varying(30),
    last_indexed_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE TABLE public.code_repo_state (
    repo_id character varying(36) NOT NULL,
    last_ingested_sha character varying(64),
    last_ingested_at timestamp with time zone,
    last_ingested_branch character varying(255)
);

CREATE TABLE public.code_repos (
    id character varying(36) NOT NULL,
    label character varying(200) NOT NULL,
    gitlab_url character varying(500),
    gitlab_repo character varying(500) NOT NULL,
    gitlab_branch character varying(200) DEFAULT 'main'::character varying NOT NULL,
    last_indexed_at timestamp with time zone,
    files_count integer DEFAULT 0 NOT NULL,
    chunks_count integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone,
    role character varying(20),
    depends_on json,
    locations json,
    is_registry_baseline boolean DEFAULT false NOT NULL
);

CREATE TABLE public.code_review_results (
    id character varying(36) NOT NULL,
    code_iteration_id character varying(36) NOT NULL,
    status public.reviewstatus NOT NULL,
    issues json,
    created_at timestamp with time zone NOT NULL
);

CREATE TABLE public.conversations (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    module public.conversationmodule NOT NULL,
    role public.messagerole NOT NULL,
    content text NOT NULL,
    metadata json,
    created_by character varying(36),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone
);

CREATE TABLE public.counter_proposals (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    partner_id character varying(36) NOT NULL,
    assignment_id character varying(36) NOT NULL,
    counter_proposal_id character varying(64) NOT NULL,
    status public.counterproposalstatus DEFAULT 'open'::public.counterproposalstatus NOT NULL,
    negotiation_round integer DEFAULT 1 NOT NULL,
    justification text NOT NULL,
    valid_until timestamp with time zone,
    payload jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_at timestamp with time zone,
    resolved_by character varying(36),
    resolution_text text,
    originator character varying(20) DEFAULT 'partner'::character varying NOT NULL,
    request_category character varying(50),
    brd_classification character varying(30),
    auto_disposition character varying(30) DEFAULT 'pending'::character varying,
    cluster_id character varying(36)
);

CREATE TABLE public.decision_ledger_entries (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    question_key character varying(128) NOT NULL,
    kind character varying(32) NOT NULL,
    question text,
    options json,
    chosen text,
    evidence json,
    directive text,
    decided_by character varying(36),
    decided_at timestamp with time zone,
    decided_against json,
    supersedes_id character varying(36),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone
);

CREATE TABLE public.decline_specs (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    spec_json jsonb NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    status public.artifactstatus DEFAULT 'draft'::public.artifactstatus NOT NULL,
    approved_by character varying(36),
    approved_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone
);

CREATE TABLE public.deployment_runs (
    id character varying(36) NOT NULL,
    phase_b_run_id character varying(36) NOT NULL,
    build_run_id character varying(36),
    iteration_number integer NOT NULL,
    target_server character varying(500),
    status public.deployrunstatus DEFAULT 'running'::public.deployrunstatus NOT NULL,
    deploy_log text,
    health_check_url character varying(500),
    health_check_passed boolean,
    triggered_at timestamp with time zone NOT NULL,
    completed_at timestamp with time zone
);

CREATE TABLE public.doc_code_links (
    id character varying(36) NOT NULL,
    doc_chunk_id character varying(36) NOT NULL,
    symbol_chunk_id character varying(36) NOT NULL,
    confidence double precision NOT NULL,
    last_checked timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE TABLE public.document_chunks (
    id character varying(36) NOT NULL,
    source_file character varying(1000) NOT NULL,
    doc_category character varying(100) NOT NULL,
    content text NOT NULL,
    embedding public.vector(768),
    chunk_index integer NOT NULL,
    metadata json,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone,
    symbol_kind character varying(50),
    symbol_name character varying(500),
    signature text,
    line_start integer,
    line_end integer,
    language character varying(30),
    view_kind character varying(20),
    parent_symbol_id character varying(36),
    title_breadcrumb character varying(1000),
    last_modified timestamp with time zone,
    author character varying(200),
    product_area character varying(100),
    freshness_score double precision,
    deprecated boolean,
    parent_chunk_id character varying(36),
    imports json,
    inherits character varying(500),
    implements json,
    calls json,
    called_by json,
    cross_file_calls json,
    content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english'::regconfig, COALESCE(content, ''::text))) STORED
);

CREATE TABLE public.document_reconciliations (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    doc_kind character varying(32) NOT NULL,
    doc_id character varying(36),
    doc_version integer,
    plan_version_before integer,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    conflicts json,
    resolutions json,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone,
    grounding json
);

CREATE TABLE public.embedding_cache (
    content_sha256 character(64) NOT NULL,
    model text NOT NULL,
    view_kind text DEFAULT ''::text NOT NULL,
    embedding public.vector(768) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.emergency_issues (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    partner_id character varying(36) NOT NULL,
    issue_id character varying(64),
    severity character varying(16) DEFAULT 'critical'::character varying NOT NULL,
    status character varying(16) DEFAULT 'open'::character varying NOT NULL,
    title character varying(300) NOT NULL,
    description text NOT NULL,
    authority_resolution_text text,
    resolved_by character varying(36),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_at timestamp with time zone
);

CREATE TABLE public.escalation_tickets (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    partner_id character varying(36) NOT NULL,
    a2a_message_id character varying(36),
    cluster_id character varying(36),
    team character varying(16) NOT NULL,
    status character varying(16) DEFAULT 'open'::character varying NOT NULL,
    question_text text NOT NULL,
    escalation_reason text,
    team_response_text text,
    responded_by character varying(36),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    responded_at timestamp with time zone,
    ai_suggestion text,
    ai_comment_draft text
);

CREATE TABLE public.eval_policy_audit (
    id character varying(36) NOT NULL,
    checkpoint_id character varying(128) NOT NULL,
    old_policy_mode character varying(32) NOT NULL,
    new_policy_mode character varying(32) NOT NULL,
    actor_user_id character varying(36),
    actor_username character varying(128) NOT NULL,
    reason text NOT NULL,
    app_env character varying(32) DEFAULT 'development'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.eval_verdicts (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    checkpoint_id character varying(128) NOT NULL,
    from_stage character varying(64) NOT NULL,
    to_stage character varying(64) NOT NULL,
    verdict character varying(16) NOT NULL,
    passed boolean NOT NULL,
    policy_mode character varying(32) NOT NULL,
    confidence double precision,
    scores_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    hard_fail_codes jsonb DEFAULT '[]'::jsonb NOT NULL,
    warn_codes jsonb DEFAULT '[]'::jsonb NOT NULL,
    reasons_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    source_artifact_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    target_artifact_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    rubric_version character varying(64) NOT NULL,
    deterministic_version character varying(64) NOT NULL,
    critic_model character varying(128),
    judge_model character varying(128),
    latency_ms integer DEFAULT 0 NOT NULL,
    retry_recommended boolean DEFAULT false NOT NULL,
    is_override boolean DEFAULT false NOT NULL,
    override_actor character varying(128),
    override_reason text,
    previous_verdict_id character varying(36),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.feedback (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    module character varying(100) NOT NULL,
    artifact_id character varying(36),
    content text NOT NULL,
    created_by character varying(36) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone
);

CREATE TABLE public.flow_context (
    id character varying(36) NOT NULL,
    repo_id character varying(36) NOT NULL,
    summary text,
    transaction_apis json,
    meta_apis json,
    flows json,
    entry_points json,
    base_commit_sha character varying(64),
    generated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.git_events (
    id character varying(36) NOT NULL,
    phase_b_run_id character varying(36) NOT NULL,
    branch_name character varying(500),
    commit_sha character varying(100),
    mr_url character varying(1000),
    mr_iid integer,
    status public.giteventstatus DEFAULT 'branch_created'::public.giteventstatus NOT NULL,
    created_at timestamp with time zone NOT NULL
);

CREATE TABLE public.governance_skills (
    id character varying(36) NOT NULL,
    skill_type character varying(16) NOT NULL,
    version integer NOT NULL,
    content text NOT NULL,
    checksum character varying(64) NOT NULL,
    filename character varying(255),
    rules_json json,
    uploaded_by character varying(36),
    created_at timestamp with time zone NOT NULL,
    bundle_bytes bytea,
    bundle_sha256 character varying(64),
    bundle_filename character varying(255),
    manifest_json json,
    exec_manifest_json json,
    safety_warnings_json json,
    provenance_json json,
    smoke_status character varying(16),
    smoke_detail_json json,
    name character varying(120) DEFAULT 'default'::character varying NOT NULL,
    enabled boolean DEFAULT true NOT NULL
);

CREATE TABLE public.integration_exchanges (
    id character varying(36) NOT NULL,
    exchange_id character varying(64) NOT NULL,
    direction character varying(10) NOT NULL,
    alias character varying(100) NOT NULL,
    method character varying(10) NOT NULL,
    path character varying(1000) NOT NULL,
    status integer,
    error_code character varying(40),
    request_bytes integer DEFAULT 0 NOT NULL,
    response_bytes integer DEFAULT 0 NOT NULL,
    elapsed_ms integer,
    dropped_headers json,
    correlation_id character varying(64),
    cert_context json,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone,
    query character varying(1000)
);

CREATE TABLE public.is_review_results (
    id character varying(36) NOT NULL,
    code_iteration_id character varying(36) NOT NULL,
    status public.isreviewstatus NOT NULL,
    findings json,
    created_at timestamp with time zone NOT NULL
);

CREATE TABLE public.kit_publications (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    negotiation_version integer NOT NULL,
    envelope jsonb NOT NULL,
    envelope_sha256 character varying(64) NOT NULL,
    source_doc_versions jsonb NOT NULL,
    revision_reason text,
    resolver_action character varying(50),
    published_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    published_by character varying(36)
);

CREATE TABLE public.kit_revision_plans (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    target_version integer NOT NULL,
    status character varying(20) DEFAULT 'draft'::character varying NOT NULL,
    items json,
    summary text,
    created_by character varying(36),
    updated_by character varying(36),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone
);

CREATE TABLE public.llm_usage_records (
    id character varying(36) NOT NULL,
    ts timestamp with time zone NOT NULL,
    change_request_id character varying(36),
    run_id character varying(36),
    kind character varying(32),
    section character varying(64),
    model character varying(80),
    input_tokens integer DEFAULT 0 NOT NULL,
    output_tokens integer DEFAULT 0 NOT NULL,
    cache_read_tokens integer DEFAULT 0 NOT NULL,
    cache_write_tokens integer DEFAULT 0 NOT NULL,
    cost_usd double precision
);

CREATE TABLE public.module_context (
    id character varying(36) NOT NULL,
    repo_id character varying(36) NOT NULL,
    module_path character varying(1000) NOT NULL,
    parent_module_path character varying(1000),
    depth integer DEFAULT 0 NOT NULL,
    summary text,
    key_types json,
    entry_points json,
    functional_flow text,
    conventions text,
    gotchas text,
    why text,
    java_version character varying(20),
    depends_on json,
    base_commit_sha character varying(64),
    generated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.negotiation_cluster_members (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    counter_proposal_id character varying(36) NOT NULL,
    partner_id character varying(36) NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.negotiation_clusters (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    cluster_key character varying(200) NOT NULL,
    category character varying(50) NOT NULL,
    topic_summary character varying(500),
    partner_count integer DEFAULT 0 NOT NULL,
    ai_summary text,
    ai_recommendation character varying(20),
    confidence_score double precision,
    pm_decision character varying(20) DEFAULT 'pending'::character varying NOT NULL,
    pm_decision_text text,
    pm_modified_value json,
    pm_decided_at timestamp with time zone,
    pm_decided_by character varying(36),
    conflict_with_cluster_id character varying(36),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.negotiation_messages (
    id character varying(36) NOT NULL,
    thread_id character varying(36) NOT NULL,
    role character varying(20) NOT NULL,
    content text NOT NULL,
    ai_draft text,
    approved_by character varying(36),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    correlation_id character varying(36),
    counter_proposal_id character varying(36),
    blocker_id character varying(36),
    event_kind character varying(40)
);

CREATE TABLE public.negotiation_round_states (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    partner_id character varying(36) NOT NULL,
    round_number integer NOT NULL,
    started_at timestamp with time zone NOT NULL,
    deadline_at timestamp with time zone NOT NULL,
    status character varying(30) DEFAULT 'open'::character varying NOT NULL,
    closed_at timestamp with time zone,
    silent_acceptance_cp_id character varying(36),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.negotiation_threads (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    partner_id character varying(36) NOT NULL,
    status character varying(20) DEFAULT 'open'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    kind character varying(20) DEFAULT 'general'::character varying NOT NULL
);

CREATE TABLE public.network_xml_templates (
    api_name character varying(64) NOT NULL,
    flow_code character varying(32) NOT NULL,
    xml_template text NOT NULL,
    placeholders_used jsonb NOT NULL,
    source character varying(16) NOT NULL,
    approved_by character varying(64),
    approved_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.notifications (
    id character varying(36) NOT NULL,
    user_id character varying(36) NOT NULL,
    title character varying(500) NOT NULL,
    message text NOT NULL,
    type public.notificationtype NOT NULL,
    related_id character varying(36),
    is_read boolean NOT NULL,
    email_sent boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone
);

CREATE SEQUENCE public.npci_policy_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.npci_policy_id_seq OWNED BY public.authority_policy.id;

CREATE TABLE public.partner_agents (
    id character varying(36) NOT NULL,
    name character varying(200) NOT NULL,
    partner_type jsonb NOT NULL,
    endpoint_url character varying(1000),
    api_key text,
    api_key_hash character varying(200),
    status character varying(20) DEFAULT 'active'::character varying NOT NULL,
    agent_card_url character varying(1000),
    metadata json,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone,
    protocol_version character varying(20) DEFAULT 'legacy'::character varying NOT NULL,
    jwt_signing_secret text,
    signing_secret text,
    tls_tier character varying(20) DEFAULT 'jwt'::character varying NOT NULL,
    client_cert_fingerprint character varying(64),
    allowed_cidrs jsonb,
    rate_limit_rps integer DEFAULT 100 NOT NULL,
    cert_agent_partner_id character varying(50),
    previous_jwt_signing_secret text,
    previous_signing_secret text,
    secret_rotated_at timestamp with time zone,
    ssl_verify boolean,
    ca_cert_pem text,
    max_inline_attachment_bytes integer,
    signing_secret_version integer DEFAULT 1 NOT NULL
);

CREATE TABLE public.partner_progress (
    id character varying(36) NOT NULL,
    assignment_id character varying(36) NOT NULL,
    step character varying(30) NOT NULL,
    notes text,
    reported_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.phase_b_run_repos (
    id character varying(36) NOT NULL,
    run_id character varying(36) NOT NULL,
    repo_id character varying(36) NOT NULL,
    branch character varying(200) NOT NULL,
    mr_url character varying(1000),
    mr_iid integer,
    mr_state character varying(40),
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    pushed_content_hash character varying(64)
);

CREATE TABLE public.phase_b_runs (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    status public.phasebrunstatus DEFAULT 'in_progress'::public.phasebrunstatus NOT NULL,
    current_step public.phasebstep DEFAULT 'code_change'::public.phasebstep NOT NULL,
    iteration_count integer DEFAULT 0 NOT NULL,
    gitlab_repo character varying(500),
    gitlab_branch character varying(200) DEFAULT 'main'::character varying,
    started_at timestamp with time zone NOT NULL,
    completed_at timestamp with time zone
);

CREATE TABLE public.phase_b_triage_reports (
    id character varying(36) NOT NULL,
    phase_b_run_id character varying(36) NOT NULL,
    build_run_id character varying(36),
    uat_test_run_id character varying(36),
    report json,
    walkthrough json,
    created_by character varying(36),
    created_at timestamp with time zone NOT NULL
);

CREATE TABLE public.process_execution_audit (
    id character varying(36) NOT NULL,
    command character varying(64) NOT NULL,
    args_digest character varying(256),
    cwd character varying(500),
    run_id character varying(64),
    actor character varying(128),
    exit_code integer,
    duration_ms double precision,
    timed_out boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.product_canvases (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    content text,
    version integer NOT NULL,
    status public.artifactstatus2 NOT NULL,
    approved_by character varying(36),
    approved_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone,
    docx_path character varying(500),
    file_path character varying(1000),
    source public.documentsource DEFAULT 'generated'::public.documentsource NOT NULL,
    original_filename character varying(500),
    uploaded_by character varying(36),
    uploaded_at timestamp with time zone
);

CREATE TABLE public.product_kit_documents (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    doc_type public.productkitdoctype NOT NULL,
    content text,
    file_path character varying(1000),
    version integer NOT NULL,
    status public.artifactstatus4 NOT NULL,
    approved_by character varying(36),
    approved_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone,
    docx_path character varying(500),
    pptx_path character varying(500),
    negotiation_version integer DEFAULT 1 NOT NULL,
    source public.documentsource DEFAULT 'generated'::public.documentsource NOT NULL,
    original_filename character varying(500),
    uploaded_by character varying(36),
    uploaded_at timestamp with time zone,
    script_json jsonb,
    video_provider character varying(40),
    video_model character varying(80),
    video_duration_sec integer,
    override_path character varying(500),
    override_filename character varying(255),
    override_sha256 character varying(64),
    override_size_bytes bigint,
    override_mime_type character varying(120),
    override_uploaded_at timestamp with time zone,
    override_uploaded_by character varying(36)
);

CREATE TABLE public.repo_path_context (
    id character varying(36) NOT NULL,
    repo_id character varying(36) NOT NULL,
    path character varying(1000) NOT NULL,
    content text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.research_outputs (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    market_research text,
    product_knowledge text,
    regulatory_compliance text,
    combined_report text,
    version integer NOT NULL,
    status public.artifactstatus NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone
);

CREATE TABLE public.resolver_recommendations (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    partner_id character varying(36) NOT NULL,
    a2a_message_id character varying(36) NOT NULL,
    message_type character varying(20) NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    content text NOT NULL,
    model_used character varying(100),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.review_findings (
    id character varying(36) NOT NULL,
    run_id character varying(36) NOT NULL,
    round integer DEFAULT 0 NOT NULL,
    severity character varying(20),
    category character varying(20),
    repo_id character varying(36),
    file character varying(1000),
    line integer,
    why text,
    suggested_fix text,
    blocking boolean DEFAULT false NOT NULL,
    reviewer_model character varying(100),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.sim_pack_publications (
    id character varying(36) NOT NULL,
    pack_ref character varying(120) NOT NULL,
    pack_id character varying(80) NOT NULL,
    target character varying(500) NOT NULL,
    response_status integer,
    echoed_pack_id character varying(80),
    published_by character varying(200),
    published_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.sim_packs (
    id character varying(36) NOT NULL,
    pack_ref character varying(120) NOT NULL,
    pack_id character varying(80) NOT NULL,
    change_request_id character varying(36),
    base_pack_ref character varying(120),
    engine_min character varying(20) DEFAULT '1.0'::character varying NOT NULL,
    requires json,
    content json NOT NULL,
    coverage json,
    status character varying(20) DEFAULT 'draft'::character varying NOT NULL,
    created_by character varying(200),
    published_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone
);

CREATE TABLE public.tech_specs (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    content text,
    file_path character varying(1000),
    version integer NOT NULL,
    status public.artifactstatus3 NOT NULL,
    approved_by character varying(36),
    approved_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone,
    docx_path character varying(500),
    source public.documentsource DEFAULT 'generated'::public.documentsource NOT NULL,
    original_filename character varying(500),
    uploaded_by character varying(36),
    uploaded_at timestamp with time zone,
    override_path character varying(500),
    override_filename character varying(255),
    override_sha256 character varying(64),
    override_size_bytes bigint,
    override_mime_type character varying(120),
    override_uploaded_at timestamp with time zone,
    override_uploaded_by character varying(36)
);

CREATE TABLE public.uat_test_cases (
    id character varying(36) NOT NULL,
    phase_b_run_id character varying(36) NOT NULL,
    suite_version integer DEFAULT 1 NOT NULL,
    test_id character varying(50),
    category public.testcasecategory DEFAULT 'new_feature'::public.testcasecategory NOT NULL,
    title character varying(500) NOT NULL,
    description text,
    preconditions text,
    http_method character varying(10),
    endpoint character varying(500),
    request_headers json,
    request_payload json,
    expected_status integer,
    expected_response json,
    pass_criteria text,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone NOT NULL
);

CREATE TABLE public.uat_test_results (
    id character varying(36) NOT NULL,
    test_run_id character varying(36) NOT NULL,
    test_case_id character varying(36) NOT NULL,
    status public.testresultstatus NOT NULL,
    actual_status integer,
    actual_response json,
    latency_ms integer,
    error_message text,
    executed_at timestamp with time zone NOT NULL
);

CREATE TABLE public.uat_test_runs (
    id character varying(36) NOT NULL,
    phase_b_run_id character varying(36) NOT NULL,
    suite_version integer DEFAULT 1 NOT NULL,
    iteration_number integer NOT NULL,
    base_url character varying(500),
    total integer,
    passed integer,
    failed integer,
    skipped integer,
    status public.testrunstatus DEFAULT 'running'::public.testrunstatus NOT NULL,
    started_at timestamp with time zone NOT NULL,
    completed_at timestamp with time zone,
    script_path character varying(1000),
    log text
);

CREATE TABLE public.uat_triage_results (
    id character varying(36) NOT NULL,
    test_run_id character varying(36) NOT NULL,
    test_result_id character varying(36) NOT NULL,
    verdict public.triageverdict NOT NULL,
    ai_reasoning text,
    user_override public.triageuseroverride,
    final_verdict public.triagefinalverdict,
    created_at timestamp with time zone NOT NULL
);

CREATE TABLE public.user_roles (
    user_id character varying(36) NOT NULL,
    role public.userrole NOT NULL
);

CREATE TABLE public.users (
    id character varying(36) NOT NULL,
    username character varying(100) NOT NULL,
    email character varying(255) NOT NULL,
    password_hash character varying(255) NOT NULL,
    full_name character varying(255),
    role public.userrole NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone,
    mfa_enabled boolean DEFAULT false NOT NULL,
    mfa_secret text,
    mfa_backup_codes json,
    auth_source character varying(16) DEFAULT 'local'::character varying NOT NULL
);

CREATE TABLE public.verification_runs (
    id character varying(36) NOT NULL,
    run_id character varying(36) NOT NULL,
    round integer DEFAULT 0 NOT NULL,
    exit_code integer,
    timed_out boolean DEFAULT false NOT NULL,
    smoke_passed boolean,
    raw_output text,
    llm_reasoning text,
    decision character varying(20),
    plan json,
    gates json,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.xsd_java_links (
    id character varying(36) NOT NULL,
    repo_id character varying(36) NOT NULL,
    node_id character varying(36),
    xpath character varying(1000),
    symbol_chunk_id_or_path character varying(1000),
    source character varying(40),
    confidence double precision,
    base_commit_sha character varying(64),
    evidence_json json,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.xsd_schema_edges (
    id character varying(36) NOT NULL,
    from_node_id character varying(36) NOT NULL,
    to_node_id character varying(36),
    edge_type character varying(20) NOT NULL,
    schema_location character varying(1000),
    namespace character varying(500)
);

CREATE TABLE public.xsd_schema_nodes (
    id character varying(36) NOT NULL,
    repo_id character varying(36) NOT NULL,
    path character varying(1000) NOT NULL,
    target_namespace character varying(500),
    base_commit_sha character varying(64),
    content_hash character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.xsds (
    id character varying(36) NOT NULL,
    change_request_id character varying(36) NOT NULL,
    content text,
    file_path character varying(1000),
    version integer NOT NULL,
    is_required boolean NOT NULL,
    status public.xsdstatus NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone,
    docx_path character varying(500),
    source public.documentsource DEFAULT 'generated'::public.documentsource NOT NULL,
    original_filename character varying(500),
    uploaded_by character varying(36),
    uploaded_at timestamp with time zone,
    override_path character varying(500),
    override_filename character varying(255),
    override_sha256 character varying(64),
    override_size_bytes bigint,
    override_mime_type character varying(120),
    override_uploaded_at timestamp with time zone,
    override_uploaded_by character varying(36)
);

ALTER TABLE ONLY public.authority_policy ALTER COLUMN id SET DEFAULT nextval('public.npci_policy_id_seq'::regclass);

ALTER TABLE ONLY public.a2a_messages
    ADD CONSTRAINT a2a_messages_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.a2a_sessions
    ADD CONSTRAINT a2a_sessions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.admin_action_audit
    ADD CONSTRAINT admin_action_audit_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.agent_jobs
    ADD CONSTRAINT agent_jobs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.agentic_events
    ADD CONSTRAINT agentic_events_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.agentic_run_repos
    ADD CONSTRAINT agentic_run_repos_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.agentic_runs
    ADD CONSTRAINT agentic_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.api_fields
    ADD CONSTRAINT api_fields_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.api_messages
    ADD CONSTRAINT api_messages_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.app_configs
    ADD CONSTRAINT app_configs_pkey PRIMARY KEY (key);

ALTER TABLE ONLY public.approvals
    ADD CONSTRAINT approvals_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.artifact_cold_storage
    ADD CONSTRAINT artifact_cold_storage_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assignment_status_history
    ADD CONSTRAINT assignment_status_history_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.auth_audit
    ADD CONSTRAINT auth_audit_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.blockers
    ADD CONSTRAINT blockers_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.brd_requirements
    ADD CONSTRAINT brd_requirements_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.brds
    ADD CONSTRAINT brds_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.build_runs
    ADD CONSTRAINT build_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.cert_case_specs
    ADD CONSTRAINT cert_case_specs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.cert_flow_states
    ADD CONSTRAINT cert_flow_states_pkey PRIMARY KEY (cflow_id);

ALTER TABLE ONLY public.cert_request_variants
    ADD CONSTRAINT cert_request_variants_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.cert_runs
    ADD CONSTRAINT cert_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.cert_simulator_sync_log
    ADD CONSTRAINT cert_simulator_sync_log_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.cert_test_results
    ADD CONSTRAINT cert_test_results_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.cert_triage
    ADD CONSTRAINT cert_triage_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.cert_waivers
    ADD CONSTRAINT cert_waivers_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.change_analyses
    ADD CONSTRAINT change_analyses_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.change_impacted_paths
    ADD CONSTRAINT change_impacted_paths_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.change_manifests
    ADD CONSTRAINT change_manifests_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.change_partner_assignments
    ADD CONSTRAINT change_partner_assignments_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.change_reports
    ADD CONSTRAINT change_reports_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.change_request_contexts
    ADD CONSTRAINT change_request_contexts_pkey PRIMARY KEY (change_request_id);

ALTER TABLE ONLY public.change_requests
    ADD CONSTRAINT change_requests_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.clarifications
    ADD CONSTRAINT clarifications_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.code_iterations
    ADD CONSTRAINT code_iterations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.code_plans
    ADD CONSTRAINT code_plans_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.code_repo_file_state
    ADD CONSTRAINT code_repo_file_state_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.code_repo_state
    ADD CONSTRAINT code_repo_state_pkey PRIMARY KEY (repo_id);

ALTER TABLE ONLY public.code_repos
    ADD CONSTRAINT code_repos_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.code_review_results
    ADD CONSTRAINT code_review_results_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT conversations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.counter_proposals
    ADD CONSTRAINT counter_proposals_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.decision_ledger_entries
    ADD CONSTRAINT decision_ledger_entries_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.decline_specs
    ADD CONSTRAINT decline_specs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.deployment_runs
    ADD CONSTRAINT deployment_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.doc_code_links
    ADD CONSTRAINT doc_code_links_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.document_chunks
    ADD CONSTRAINT document_chunks_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.document_reconciliations
    ADD CONSTRAINT document_reconciliations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.embedding_cache
    ADD CONSTRAINT embedding_cache_pkey PRIMARY KEY (content_sha256, model, view_kind);

ALTER TABLE ONLY public.emergency_issues
    ADD CONSTRAINT emergency_issues_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.escalation_tickets
    ADD CONSTRAINT escalation_tickets_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.eval_policy_audit
    ADD CONSTRAINT eval_policy_audit_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.eval_verdicts
    ADD CONSTRAINT eval_verdicts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.feedback
    ADD CONSTRAINT feedback_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.flow_context
    ADD CONSTRAINT flow_context_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.git_events
    ADD CONSTRAINT git_events_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.governance_skills
    ADD CONSTRAINT governance_skills_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.integration_exchanges
    ADD CONSTRAINT integration_exchanges_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.is_review_results
    ADD CONSTRAINT is_review_results_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.kit_publications
    ADD CONSTRAINT kit_publications_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.kit_revision_plans
    ADD CONSTRAINT kit_revision_plans_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.llm_usage_records
    ADD CONSTRAINT llm_usage_records_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.module_context
    ADD CONSTRAINT module_context_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.negotiation_cluster_members
    ADD CONSTRAINT negotiation_cluster_members_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.negotiation_clusters
    ADD CONSTRAINT negotiation_clusters_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.negotiation_messages
    ADD CONSTRAINT negotiation_messages_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.negotiation_round_states
    ADD CONSTRAINT negotiation_round_states_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.negotiation_threads
    ADD CONSTRAINT negotiation_threads_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.authority_policy
    ADD CONSTRAINT npci_policy_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.partner_agents
    ADD CONSTRAINT partner_agents_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.partner_progress
    ADD CONSTRAINT partner_progress_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.phase_b_run_repos
    ADD CONSTRAINT phase_b_run_repos_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.phase_b_runs
    ADD CONSTRAINT phase_b_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.phase_b_triage_reports
    ADD CONSTRAINT phase_b_triage_reports_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.process_execution_audit
    ADD CONSTRAINT process_execution_audit_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.product_canvases
    ADD CONSTRAINT product_canvases_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.product_kit_documents
    ADD CONSTRAINT product_kit_documents_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.repo_path_context
    ADD CONSTRAINT repo_path_context_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.research_outputs
    ADD CONSTRAINT research_outputs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.resolver_recommendations
    ADD CONSTRAINT resolver_recommendations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.review_findings
    ADD CONSTRAINT review_findings_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.sim_pack_publications
    ADD CONSTRAINT sim_pack_publications_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.sim_packs
    ADD CONSTRAINT sim_packs_pack_ref_key UNIQUE (pack_ref);

ALTER TABLE ONLY public.sim_packs
    ADD CONSTRAINT sim_packs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.tech_specs
    ADD CONSTRAINT tech_specs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.uat_test_cases
    ADD CONSTRAINT uat_test_cases_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.uat_test_results
    ADD CONSTRAINT uat_test_results_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.uat_test_runs
    ADD CONSTRAINT uat_test_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.uat_triage_results
    ADD CONSTRAINT uat_triage_results_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.network_xml_templates
    ADD CONSTRAINT upi_xml_templates_pkey PRIMARY KEY (api_name);

ALTER TABLE ONLY public.cert_request_variants
    ADD CONSTRAINT uq_cert_request_variants_round_case_variant UNIQUE (cflow_id, run_number, case_id, variant_id);

ALTER TABLE ONLY public.clarifications
    ADD CONSTRAINT uq_clarifications_change_version UNIQUE (change_request_id, version);

ALTER TABLE ONLY public.code_repo_file_state
    ADD CONSTRAINT uq_code_repo_file_state_pair UNIQUE (repo_id, source_file);

ALTER TABLE ONLY public.doc_code_links
    ADD CONSTRAINT uq_doc_code_links_pair UNIQUE (doc_chunk_id, symbol_chunk_id);

ALTER TABLE ONLY public.governance_skills
    ADD CONSTRAINT uq_governance_skill_version UNIQUE (skill_type, version);

ALTER TABLE ONLY public.kit_publications
    ADD CONSTRAINT uq_kit_publications_change_version UNIQUE (change_request_id, negotiation_version);

ALTER TABLE ONLY public.phase_b_run_repos
    ADD CONSTRAINT uq_phase_b_run_repos_run_repo UNIQUE (run_id, repo_id);

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_pkey PRIMARY KEY (user_id, role);

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_email_key UNIQUE (email);

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_username_key UNIQUE (username);

ALTER TABLE ONLY public.verification_runs
    ADD CONSTRAINT verification_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.xsd_java_links
    ADD CONSTRAINT xsd_java_links_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.xsd_schema_edges
    ADD CONSTRAINT xsd_schema_edges_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.xsd_schema_nodes
    ADD CONSTRAINT xsd_schema_nodes_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.xsds
    ADD CONSTRAINT xsds_pkey PRIMARY KEY (id);

CREATE INDEX idx_document_chunks_content_tsv ON public.document_chunks USING gin (content_tsv);

CREATE INDEX idx_document_chunks_embedding_hnsw ON public.document_chunks USING hnsw (embedding public.vector_cosine_ops) WITH (m='16', ef_construction='64');

CREATE INDEX ix_a2a_messages_next_retry_at ON public.a2a_messages USING btree (next_retry_at) WHERE (next_retry_at IS NOT NULL);

CREATE INDEX ix_admin_action_audit_action ON public.admin_action_audit USING btree (action);

CREATE INDEX ix_admin_action_audit_resource_id ON public.admin_action_audit USING btree (resource_id);

CREATE INDEX ix_admin_action_audit_source ON public.admin_action_audit USING btree (source);

CREATE INDEX ix_admin_action_audit_user_id ON public.admin_action_audit USING btree (user_id);

CREATE INDEX ix_agent_jobs_change_request_id ON public.agent_jobs USING btree (change_request_id);

CREATE INDEX ix_agent_jobs_change_status ON public.agent_jobs USING btree (change_request_id, status);

CREATE INDEX ix_agent_jobs_module ON public.agent_jobs USING btree (module);

CREATE INDEX ix_agent_jobs_started_by_user_id ON public.agent_jobs USING btree (started_by_user_id);

CREATE INDEX ix_agent_jobs_status ON public.agent_jobs USING btree (status);

CREATE INDEX ix_agent_jobs_status_updated ON public.agent_jobs USING btree (status, updated_at);

CREATE INDEX ix_agentic_events_run_id ON public.agentic_events USING btree (run_id);

CREATE INDEX ix_agentic_run_repos_repo_id ON public.agentic_run_repos USING btree (repo_id);

CREATE INDEX ix_agentic_run_repos_run_id ON public.agentic_run_repos USING btree (run_id);

CREATE INDEX ix_agentic_runs_change_request_id ON public.agentic_runs USING btree (change_request_id);

CREATE INDEX ix_api_fields_message_id ON public.api_fields USING btree (message_id);

CREATE UNIQUE INDEX ix_api_fields_message_xpath ON public.api_fields USING btree (message_id, xpath);

CREATE UNIQUE INDEX ix_api_messages_api_name ON public.api_messages USING btree (api_name);

CREATE INDEX ix_approvals_approver_id ON public.approvals USING btree (approver_id);

CREATE INDEX ix_approvals_artifact_id ON public.approvals USING btree (artifact_id);

CREATE INDEX ix_artifact_cold_storage_change_request_id ON public.artifact_cold_storage USING btree (change_request_id);

CREATE INDEX ix_artifact_cold_storage_compressed_at ON public.artifact_cold_storage USING btree (compressed_at);

CREATE INDEX ix_artifact_cold_storage_ready_for_archive ON public.artifact_cold_storage USING btree (ready_for_archive);

CREATE INDEX ix_artifact_cold_storage_source_id ON public.artifact_cold_storage USING btree (source_id);

CREATE INDEX ix_artifact_cold_storage_source_table ON public.artifact_cold_storage USING btree (source_table);

CREATE INDEX ix_assignment_status_history_assignment_id ON public.assignment_status_history USING btree (assignment_id);

CREATE INDEX ix_assignment_status_history_created_at ON public.assignment_status_history USING btree (created_at);

CREATE INDEX ix_auth_audit_created_at ON public.auth_audit USING btree (created_at);

CREATE INDEX ix_auth_audit_event ON public.auth_audit USING btree (event);

CREATE INDEX ix_auth_audit_username ON public.auth_audit USING btree (username);

CREATE INDEX ix_blockers_assignment_id ON public.blockers USING btree (assignment_id);

CREATE INDEX ix_blockers_change_request_id ON public.blockers USING btree (change_request_id);

CREATE INDEX ix_blockers_partner_id ON public.blockers USING btree (partner_id);

CREATE INDEX ix_brd_requirements_change_request_id ON public.brd_requirements USING btree (change_request_id);

CREATE INDEX ix_cert_case_specs_round_case ON public.cert_case_specs USING btree (cflow_id, run_number, case_id);

CREATE INDEX ix_cert_flow_states_change_partner ON public.cert_flow_states USING btree (change_request_id, partner_id);

CREATE INDEX ix_cert_request_variants_round_case ON public.cert_request_variants USING btree (cflow_id, run_number, case_id);

CREATE INDEX ix_cert_simulator_sync_log_change_id ON public.cert_simulator_sync_log USING btree (change_request_id, created_at);

CREATE INDEX ix_cert_simulator_sync_log_created_at ON public.cert_simulator_sync_log USING btree (created_at);

CREATE INDEX ix_change_analyses_change_request_id ON public.change_analyses USING btree (change_request_id);

CREATE INDEX ix_change_impacted_paths_change_request_id ON public.change_impacted_paths USING btree (change_request_id);

CREATE INDEX ix_change_impacted_paths_repo_path ON public.change_impacted_paths USING btree (repo_id, path);

CREATE INDEX ix_change_manifests_run_id ON public.change_manifests USING btree (run_id);

CREATE INDEX ix_change_reports_change_request_id ON public.change_reports USING btree (change_request_id);

CREATE INDEX ix_change_requests_created_by ON public.change_requests USING btree (created_by);

CREATE INDEX ix_change_requests_status ON public.change_requests USING btree (status);

CREATE INDEX ix_clarifications_change_id ON public.clarifications USING btree (change_request_id);

CREATE INDEX ix_code_plans_change_request_id ON public.code_plans USING btree (change_request_id);

CREATE INDEX ix_code_repo_file_state_repo_id ON public.code_repo_file_state USING btree (repo_id);

CREATE INDEX ix_conversations_change_request_id ON public.conversations USING btree (change_request_id);

CREATE INDEX ix_counter_proposals_assignment_id ON public.counter_proposals USING btree (assignment_id);

CREATE INDEX ix_counter_proposals_change_request_id ON public.counter_proposals USING btree (change_request_id);

CREATE INDEX ix_counter_proposals_partner_id ON public.counter_proposals USING btree (partner_id);

CREATE INDEX ix_decision_ledger_change_request_id ON public.decision_ledger_entries USING btree (change_request_id);

CREATE INDEX ix_decision_ledger_question_key ON public.decision_ledger_entries USING btree (question_key);

CREATE INDEX ix_decline_specs_change_request_id ON public.decline_specs USING btree (change_request_id);

CREATE INDEX ix_doc_code_links_confidence ON public.doc_code_links USING btree (confidence);

CREATE INDEX ix_doc_code_links_doc_chunk_id ON public.doc_code_links USING btree (doc_chunk_id);

CREATE INDEX ix_doc_code_links_symbol_chunk_id ON public.doc_code_links USING btree (symbol_chunk_id);

CREATE INDEX ix_document_chunks_embedding ON public.document_chunks USING hnsw (embedding public.vector_cosine_ops);

CREATE INDEX ix_document_chunks_language ON public.document_chunks USING btree (language);

CREATE INDEX ix_document_chunks_parent_chunk_id ON public.document_chunks USING btree (parent_chunk_id);

CREATE INDEX ix_document_chunks_parent_symbol_id ON public.document_chunks USING btree (parent_symbol_id);

CREATE INDEX ix_document_reconciliations_change_kind_status ON public.document_reconciliations USING btree (change_request_id, doc_kind, status);

CREATE INDEX ix_document_reconciliations_change_request_id ON public.document_reconciliations USING btree (change_request_id);

CREATE INDEX ix_emergency_issues_change_request_id ON public.emergency_issues USING btree (change_request_id);

CREATE INDEX ix_emergency_issues_partner_id ON public.emergency_issues USING btree (partner_id);

CREATE INDEX ix_emergency_issues_status ON public.emergency_issues USING btree (status);

CREATE INDEX ix_escalation_tickets_a2a_message_id ON public.escalation_tickets USING btree (a2a_message_id);

CREATE INDEX ix_escalation_tickets_change_request_id ON public.escalation_tickets USING btree (change_request_id);

CREATE INDEX ix_escalation_tickets_cluster_id ON public.escalation_tickets USING btree (cluster_id);

CREATE INDEX ix_escalation_tickets_status ON public.escalation_tickets USING btree (status);

CREATE INDEX ix_escalation_tickets_team ON public.escalation_tickets USING btree (team);

CREATE INDEX ix_eval_policy_audit_actor_user_id ON public.eval_policy_audit USING btree (actor_user_id);

CREATE INDEX ix_eval_policy_audit_checkpoint_created ON public.eval_policy_audit USING btree (checkpoint_id, created_at);

CREATE INDEX ix_eval_policy_audit_checkpoint_id ON public.eval_policy_audit USING btree (checkpoint_id);

CREATE INDEX ix_eval_policy_audit_created_at ON public.eval_policy_audit USING btree (created_at);

CREATE INDEX ix_eval_verdicts_change_checkpoint ON public.eval_verdicts USING btree (change_request_id, checkpoint_id);

CREATE INDEX ix_eval_verdicts_change_request_id ON public.eval_verdicts USING btree (change_request_id);

CREATE INDEX ix_eval_verdicts_checkpoint_id ON public.eval_verdicts USING btree (checkpoint_id);

CREATE INDEX ix_eval_verdicts_verdict ON public.eval_verdicts USING btree (verdict);

CREATE UNIQUE INDEX ix_flow_context_repo_id ON public.flow_context USING btree (repo_id);

CREATE INDEX ix_integration_exchanges_exchange_id ON public.integration_exchanges USING btree (exchange_id);

CREATE INDEX ix_kit_publications_change_request_id ON public.kit_publications USING btree (change_request_id);

CREATE INDEX ix_kit_revision_plans_change_request_id ON public.kit_revision_plans USING btree (change_request_id);

CREATE UNIQUE INDEX ix_kit_revision_plans_change_version ON public.kit_revision_plans USING btree (change_request_id, target_version);

CREATE INDEX ix_llm_usage_records_change_request_id ON public.llm_usage_records USING btree (change_request_id);

CREATE INDEX ix_llm_usage_records_run_id ON public.llm_usage_records USING btree (run_id);

CREATE INDEX ix_llm_usage_records_ts ON public.llm_usage_records USING btree (ts);

CREATE INDEX ix_module_context_repo_id ON public.module_context USING btree (repo_id);

CREATE INDEX ix_negotiation_cluster_members_cluster_id ON public.negotiation_cluster_members USING btree (cluster_id);

CREATE INDEX ix_negotiation_cluster_members_counter_proposal_id ON public.negotiation_cluster_members USING btree (counter_proposal_id);

CREATE INDEX ix_negotiation_clusters_change_request_id ON public.negotiation_clusters USING btree (change_request_id);

CREATE INDEX ix_negotiation_messages_blocker_id ON public.negotiation_messages USING btree (blocker_id);

CREATE INDEX ix_negotiation_messages_correlation_id ON public.negotiation_messages USING btree (correlation_id);

CREATE INDEX ix_negotiation_messages_counter_proposal_id ON public.negotiation_messages USING btree (counter_proposal_id);

CREATE INDEX ix_negotiation_round_states_change_request_id ON public.negotiation_round_states USING btree (change_request_id);

CREATE INDEX ix_negotiation_round_states_partner_id ON public.negotiation_round_states USING btree (partner_id);

CREATE INDEX ix_negotiation_threads_kind ON public.negotiation_threads USING btree (kind);

CREATE INDEX ix_notifications_is_read ON public.notifications USING btree (is_read);

CREATE INDEX ix_notifications_user_id ON public.notifications USING btree (user_id);

CREATE INDEX ix_phase_b_run_repos_run_id ON public.phase_b_run_repos USING btree (run_id);

CREATE INDEX ix_process_execution_audit_command ON public.process_execution_audit USING btree (command);

CREATE INDEX ix_process_execution_audit_created_at ON public.process_execution_audit USING btree (created_at);

CREATE INDEX ix_process_execution_audit_run_id ON public.process_execution_audit USING btree (run_id);

CREATE INDEX ix_repo_path_context_repo_id ON public.repo_path_context USING btree (repo_id);

CREATE INDEX ix_resolver_recommendations_a2a_message_id ON public.resolver_recommendations USING btree (a2a_message_id);

CREATE INDEX ix_resolver_recommendations_change_request_id ON public.resolver_recommendations USING btree (change_request_id);

CREATE INDEX ix_review_findings_run_id ON public.review_findings USING btree (run_id);

CREATE INDEX ix_sim_packs_change_request_id ON public.sim_packs USING btree (change_request_id);

CREATE INDEX ix_sim_packs_pack_id ON public.sim_packs USING btree (pack_id);

CREATE INDEX ix_upi_xml_templates_flow_code ON public.network_xml_templates USING btree (flow_code);

CREATE UNIQUE INDEX ix_upi_xml_templates_pending_llm ON public.network_xml_templates USING btree (api_name) WHERE (((source)::text = 'llm'::text) AND (approved_at IS NULL));

CREATE INDEX ix_verification_runs_run_id ON public.verification_runs USING btree (run_id);

CREATE INDEX ix_xsd_java_links_confidence ON public.xsd_java_links USING btree (confidence);

CREATE INDEX ix_xsd_java_links_node_id ON public.xsd_java_links USING btree (node_id);

CREATE INDEX ix_xsd_java_links_repo_id ON public.xsd_java_links USING btree (repo_id);

CREATE INDEX ix_xsd_schema_edges_from_node_id ON public.xsd_schema_edges USING btree (from_node_id);

CREATE INDEX ix_xsd_schema_edges_to_node_id ON public.xsd_schema_edges USING btree (to_node_id);

CREATE INDEX ix_xsd_schema_nodes_repo_id ON public.xsd_schema_nodes USING btree (repo_id);

CREATE UNIQUE INDEX uq_agentic_events_run_seq ON public.agentic_events USING btree (run_id, seq);

CREATE UNIQUE INDEX uq_agentic_runs_active ON public.agentic_runs USING btree (change_request_id) WHERE ((status)::text = 'active'::text);

CREATE UNIQUE INDEX uq_change_manifests_run_id ON public.change_manifests USING btree (run_id);

CREATE UNIQUE INDEX uq_change_reports_cr_input ON public.change_reports USING btree (change_request_id, input_hash);

CREATE UNIQUE INDEX uq_code_repos_registry_baseline_per_repo ON public.code_repos USING btree (gitlab_repo) WHERE is_registry_baseline;

CREATE UNIQUE INDEX uq_module_context_repo_path ON public.module_context USING btree (repo_id, module_path);

ALTER TABLE ONLY public.a2a_messages
    ADD CONSTRAINT a2a_messages_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.a2a_messages
    ADD CONSTRAINT a2a_messages_partner_id_fkey FOREIGN KEY (partner_id) REFERENCES public.partner_agents(id);

ALTER TABLE ONLY public.a2a_sessions
    ADD CONSTRAINT a2a_sessions_partner_id_fkey FOREIGN KEY (partner_id) REFERENCES public.partner_agents(id);

ALTER TABLE ONLY public.agent_jobs
    ADD CONSTRAINT agent_jobs_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.agent_jobs
    ADD CONSTRAINT agent_jobs_started_by_user_id_fkey FOREIGN KEY (started_by_user_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.agentic_events
    ADD CONSTRAINT agentic_events_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.agentic_runs(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.agentic_run_repos
    ADD CONSTRAINT agentic_run_repos_repo_id_fkey FOREIGN KEY (repo_id) REFERENCES public.code_repos(id);

ALTER TABLE ONLY public.agentic_run_repos
    ADD CONSTRAINT agentic_run_repos_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.agentic_runs(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.agentic_runs
    ADD CONSTRAINT agentic_runs_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.api_fields
    ADD CONSTRAINT api_fields_message_id_fkey FOREIGN KEY (message_id) REFERENCES public.api_messages(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.api_fields
    ADD CONSTRAINT api_fields_parent_field_id_fkey FOREIGN KEY (parent_field_id) REFERENCES public.api_fields(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.approvals
    ADD CONSTRAINT approvals_approver_id_fkey FOREIGN KEY (approver_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.assignment_status_history
    ADD CONSTRAINT assignment_status_history_actor_partner_id_fkey FOREIGN KEY (actor_partner_id) REFERENCES public.partner_agents(id);

ALTER TABLE ONLY public.assignment_status_history
    ADD CONSTRAINT assignment_status_history_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.assignment_status_history
    ADD CONSTRAINT assignment_status_history_assignment_id_fkey FOREIGN KEY (assignment_id) REFERENCES public.change_partner_assignments(id);

ALTER TABLE ONLY public.blockers
    ADD CONSTRAINT blockers_assignment_id_fkey FOREIGN KEY (assignment_id) REFERENCES public.change_partner_assignments(id);

ALTER TABLE ONLY public.blockers
    ADD CONSTRAINT blockers_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.blockers
    ADD CONSTRAINT blockers_partner_id_fkey FOREIGN KEY (partner_id) REFERENCES public.partner_agents(id);

ALTER TABLE ONLY public.blockers
    ADD CONSTRAINT blockers_resolved_by_fkey FOREIGN KEY (resolved_by) REFERENCES public.users(id);

ALTER TABLE ONLY public.brd_requirements
    ADD CONSTRAINT brd_requirements_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.brds
    ADD CONSTRAINT brds_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.brds
    ADD CONSTRAINT brds_uploaded_by_fkey FOREIGN KEY (uploaded_by) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.build_runs
    ADD CONSTRAINT build_runs_phase_b_run_id_fkey FOREIGN KEY (phase_b_run_id) REFERENCES public.phase_b_runs(id);

ALTER TABLE ONLY public.cert_case_specs
    ADD CONSTRAINT cert_case_specs_api_field_id_fkey FOREIGN KEY (api_field_id) REFERENCES public.api_fields(id);

ALTER TABLE ONLY public.cert_case_specs
    ADD CONSTRAINT cert_case_specs_api_message_id_fkey FOREIGN KEY (api_message_id) REFERENCES public.api_messages(id);

ALTER TABLE ONLY public.cert_case_specs
    ADD CONSTRAINT cert_case_specs_variant_id_fkey FOREIGN KEY (variant_id) REFERENCES public.cert_request_variants(id);

ALTER TABLE ONLY public.cert_request_variants
    ADD CONSTRAINT cert_request_variants_api_message_id_fkey FOREIGN KEY (api_message_id) REFERENCES public.api_messages(id);

ALTER TABLE ONLY public.cert_runs
    ADD CONSTRAINT cert_runs_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.cert_runs
    ADD CONSTRAINT cert_runs_partner_id_fkey FOREIGN KEY (partner_id) REFERENCES public.partner_agents(id);

ALTER TABLE ONLY public.cert_simulator_sync_log
    ADD CONSTRAINT cert_simulator_sync_log_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.cert_simulator_sync_log
    ADD CONSTRAINT cert_simulator_sync_log_cert_engine_partner_id_fkey FOREIGN KEY (cert_engine_partner_id) REFERENCES public.partner_agents(id);

ALTER TABLE ONLY public.cert_simulator_sync_log
    ADD CONSTRAINT cert_simulator_sync_log_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.cert_test_results
    ADD CONSTRAINT cert_test_results_cert_run_id_fkey FOREIGN KEY (cert_run_id) REFERENCES public.cert_runs(id);

ALTER TABLE ONLY public.cert_triage
    ADD CONSTRAINT cert_triage_cert_test_result_id_fkey FOREIGN KEY (cert_test_result_id) REFERENCES public.cert_test_results(id);

ALTER TABLE ONLY public.cert_waivers
    ADD CONSTRAINT cert_waivers_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.cert_waivers
    ADD CONSTRAINT cert_waivers_partner_id_fkey FOREIGN KEY (partner_id) REFERENCES public.partner_agents(id);

ALTER TABLE ONLY public.change_analyses
    ADD CONSTRAINT change_analyses_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.change_impacted_paths
    ADD CONSTRAINT change_impacted_paths_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.change_manifests
    ADD CONSTRAINT change_manifests_approved_by_fkey FOREIGN KEY (approved_by) REFERENCES public.users(id);

ALTER TABLE ONLY public.change_manifests
    ADD CONSTRAINT change_manifests_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.agentic_runs(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.change_partner_assignments
    ADD CONSTRAINT change_partner_assignments_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.change_partner_assignments
    ADD CONSTRAINT change_partner_assignments_partner_id_fkey FOREIGN KEY (partner_id) REFERENCES public.partner_agents(id);

ALTER TABLE ONLY public.change_reports
    ADD CONSTRAINT change_reports_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.change_request_contexts
    ADD CONSTRAINT change_request_contexts_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.change_requests
    ADD CONSTRAINT change_requests_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id);

ALTER TABLE ONLY public.clarifications
    ADD CONSTRAINT clarifications_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.code_iterations
    ADD CONSTRAINT code_iterations_phase_b_run_id_fkey FOREIGN KEY (phase_b_run_id) REFERENCES public.phase_b_runs(id);

ALTER TABLE ONLY public.code_plans
    ADD CONSTRAINT code_plans_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.code_plans
    ADD CONSTRAINT code_plans_phase_b_run_id_fkey FOREIGN KEY (phase_b_run_id) REFERENCES public.phase_b_runs(id);

ALTER TABLE ONLY public.code_review_results
    ADD CONSTRAINT code_review_results_code_iteration_id_fkey FOREIGN KEY (code_iteration_id) REFERENCES public.code_iterations(id);

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT conversations_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT conversations_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id);

ALTER TABLE ONLY public.counter_proposals
    ADD CONSTRAINT counter_proposals_assignment_id_fkey FOREIGN KEY (assignment_id) REFERENCES public.change_partner_assignments(id);

ALTER TABLE ONLY public.counter_proposals
    ADD CONSTRAINT counter_proposals_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.counter_proposals
    ADD CONSTRAINT counter_proposals_partner_id_fkey FOREIGN KEY (partner_id) REFERENCES public.partner_agents(id);

ALTER TABLE ONLY public.counter_proposals
    ADD CONSTRAINT counter_proposals_resolved_by_fkey FOREIGN KEY (resolved_by) REFERENCES public.users(id);

ALTER TABLE ONLY public.decision_ledger_entries
    ADD CONSTRAINT decision_ledger_entries_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.decline_specs
    ADD CONSTRAINT decline_specs_approved_by_fkey FOREIGN KEY (approved_by) REFERENCES public.users(id);

ALTER TABLE ONLY public.decline_specs
    ADD CONSTRAINT decline_specs_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.deployment_runs
    ADD CONSTRAINT deployment_runs_build_run_id_fkey FOREIGN KEY (build_run_id) REFERENCES public.build_runs(id);

ALTER TABLE ONLY public.deployment_runs
    ADD CONSTRAINT deployment_runs_phase_b_run_id_fkey FOREIGN KEY (phase_b_run_id) REFERENCES public.phase_b_runs(id);

ALTER TABLE ONLY public.doc_code_links
    ADD CONSTRAINT doc_code_links_doc_chunk_id_fkey FOREIGN KEY (doc_chunk_id) REFERENCES public.document_chunks(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.doc_code_links
    ADD CONSTRAINT doc_code_links_symbol_chunk_id_fkey FOREIGN KEY (symbol_chunk_id) REFERENCES public.document_chunks(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.document_reconciliations
    ADD CONSTRAINT document_reconciliations_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.emergency_issues
    ADD CONSTRAINT emergency_issues_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.emergency_issues
    ADD CONSTRAINT emergency_issues_partner_id_fkey FOREIGN KEY (partner_id) REFERENCES public.partner_agents(id);

ALTER TABLE ONLY public.escalation_tickets
    ADD CONSTRAINT escalation_tickets_a2a_message_id_fkey FOREIGN KEY (a2a_message_id) REFERENCES public.a2a_messages(id);

ALTER TABLE ONLY public.escalation_tickets
    ADD CONSTRAINT escalation_tickets_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.escalation_tickets
    ADD CONSTRAINT escalation_tickets_partner_id_fkey FOREIGN KEY (partner_id) REFERENCES public.partner_agents(id);

ALTER TABLE ONLY public.eval_policy_audit
    ADD CONSTRAINT eval_policy_audit_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.eval_verdicts
    ADD CONSTRAINT eval_verdicts_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.eval_verdicts
    ADD CONSTRAINT eval_verdicts_previous_verdict_id_fkey FOREIGN KEY (previous_verdict_id) REFERENCES public.eval_verdicts(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.feedback
    ADD CONSTRAINT feedback_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.feedback
    ADD CONSTRAINT feedback_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id);

ALTER TABLE ONLY public.flow_context
    ADD CONSTRAINT flow_context_repo_id_fkey FOREIGN KEY (repo_id) REFERENCES public.code_repos(id);

ALTER TABLE ONLY public.git_events
    ADD CONSTRAINT git_events_phase_b_run_id_fkey FOREIGN KEY (phase_b_run_id) REFERENCES public.phase_b_runs(id);

ALTER TABLE ONLY public.is_review_results
    ADD CONSTRAINT is_review_results_code_iteration_id_fkey FOREIGN KEY (code_iteration_id) REFERENCES public.code_iterations(id);

ALTER TABLE ONLY public.kit_publications
    ADD CONSTRAINT kit_publications_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.kit_publications
    ADD CONSTRAINT kit_publications_published_by_fkey FOREIGN KEY (published_by) REFERENCES public.users(id);

ALTER TABLE ONLY public.kit_revision_plans
    ADD CONSTRAINT kit_revision_plans_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.module_context
    ADD CONSTRAINT module_context_repo_id_fkey FOREIGN KEY (repo_id) REFERENCES public.code_repos(id);

ALTER TABLE ONLY public.negotiation_cluster_members
    ADD CONSTRAINT negotiation_cluster_members_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.negotiation_clusters(id);

ALTER TABLE ONLY public.negotiation_cluster_members
    ADD CONSTRAINT negotiation_cluster_members_counter_proposal_id_fkey FOREIGN KEY (counter_proposal_id) REFERENCES public.counter_proposals(id);

ALTER TABLE ONLY public.negotiation_cluster_members
    ADD CONSTRAINT negotiation_cluster_members_partner_id_fkey FOREIGN KEY (partner_id) REFERENCES public.partner_agents(id);

ALTER TABLE ONLY public.negotiation_clusters
    ADD CONSTRAINT negotiation_clusters_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.negotiation_clusters
    ADD CONSTRAINT negotiation_clusters_pm_decided_by_fkey FOREIGN KEY (pm_decided_by) REFERENCES public.users(id);

ALTER TABLE ONLY public.negotiation_messages
    ADD CONSTRAINT negotiation_messages_blocker_id_fkey FOREIGN KEY (blocker_id) REFERENCES public.blockers(id);

ALTER TABLE ONLY public.negotiation_messages
    ADD CONSTRAINT negotiation_messages_counter_proposal_id_fkey FOREIGN KEY (counter_proposal_id) REFERENCES public.counter_proposals(id);

ALTER TABLE ONLY public.negotiation_messages
    ADD CONSTRAINT negotiation_messages_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.negotiation_threads(id);

ALTER TABLE ONLY public.negotiation_round_states
    ADD CONSTRAINT negotiation_round_states_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.negotiation_round_states
    ADD CONSTRAINT negotiation_round_states_partner_id_fkey FOREIGN KEY (partner_id) REFERENCES public.partner_agents(id);

ALTER TABLE ONLY public.negotiation_threads
    ADD CONSTRAINT negotiation_threads_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.negotiation_threads
    ADD CONSTRAINT negotiation_threads_partner_id_fkey FOREIGN KEY (partner_id) REFERENCES public.partner_agents(id);

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.partner_progress
    ADD CONSTRAINT partner_progress_assignment_id_fkey FOREIGN KEY (assignment_id) REFERENCES public.change_partner_assignments(id);

ALTER TABLE ONLY public.phase_b_run_repos
    ADD CONSTRAINT phase_b_run_repos_repo_id_fkey FOREIGN KEY (repo_id) REFERENCES public.code_repos(id);

ALTER TABLE ONLY public.phase_b_run_repos
    ADD CONSTRAINT phase_b_run_repos_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.phase_b_runs(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.phase_b_runs
    ADD CONSTRAINT phase_b_runs_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.phase_b_triage_reports
    ADD CONSTRAINT phase_b_triage_reports_build_run_id_fkey FOREIGN KEY (build_run_id) REFERENCES public.build_runs(id);

ALTER TABLE ONLY public.phase_b_triage_reports
    ADD CONSTRAINT phase_b_triage_reports_phase_b_run_id_fkey FOREIGN KEY (phase_b_run_id) REFERENCES public.phase_b_runs(id);

ALTER TABLE ONLY public.phase_b_triage_reports
    ADD CONSTRAINT phase_b_triage_reports_uat_test_run_id_fkey FOREIGN KEY (uat_test_run_id) REFERENCES public.uat_test_runs(id);

ALTER TABLE ONLY public.product_canvases
    ADD CONSTRAINT product_canvases_approved_by_fkey FOREIGN KEY (approved_by) REFERENCES public.users(id);

ALTER TABLE ONLY public.product_canvases
    ADD CONSTRAINT product_canvases_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.product_canvases
    ADD CONSTRAINT product_canvases_uploaded_by_fkey FOREIGN KEY (uploaded_by) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.product_kit_documents
    ADD CONSTRAINT product_kit_documents_approved_by_fkey FOREIGN KEY (approved_by) REFERENCES public.users(id);

ALTER TABLE ONLY public.product_kit_documents
    ADD CONSTRAINT product_kit_documents_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.product_kit_documents
    ADD CONSTRAINT product_kit_documents_override_uploaded_by_fkey FOREIGN KEY (override_uploaded_by) REFERENCES public.users(id);

ALTER TABLE ONLY public.product_kit_documents
    ADD CONSTRAINT product_kit_documents_uploaded_by_fkey FOREIGN KEY (uploaded_by) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.repo_path_context
    ADD CONSTRAINT repo_path_context_repo_id_fkey FOREIGN KEY (repo_id) REFERENCES public.code_repos(id);

ALTER TABLE ONLY public.research_outputs
    ADD CONSTRAINT research_outputs_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.resolver_recommendations
    ADD CONSTRAINT resolver_recommendations_a2a_message_id_fkey FOREIGN KEY (a2a_message_id) REFERENCES public.a2a_messages(id);

ALTER TABLE ONLY public.resolver_recommendations
    ADD CONSTRAINT resolver_recommendations_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.resolver_recommendations
    ADD CONSTRAINT resolver_recommendations_partner_id_fkey FOREIGN KEY (partner_id) REFERENCES public.partner_agents(id);

ALTER TABLE ONLY public.review_findings
    ADD CONSTRAINT review_findings_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.agentic_runs(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tech_specs
    ADD CONSTRAINT tech_specs_approved_by_fkey FOREIGN KEY (approved_by) REFERENCES public.users(id);

ALTER TABLE ONLY public.tech_specs
    ADD CONSTRAINT tech_specs_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.tech_specs
    ADD CONSTRAINT tech_specs_override_uploaded_by_fkey FOREIGN KEY (override_uploaded_by) REFERENCES public.users(id);

ALTER TABLE ONLY public.tech_specs
    ADD CONSTRAINT tech_specs_uploaded_by_fkey FOREIGN KEY (uploaded_by) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.uat_test_cases
    ADD CONSTRAINT uat_test_cases_phase_b_run_id_fkey FOREIGN KEY (phase_b_run_id) REFERENCES public.phase_b_runs(id);

ALTER TABLE ONLY public.uat_test_results
    ADD CONSTRAINT uat_test_results_test_case_id_fkey FOREIGN KEY (test_case_id) REFERENCES public.uat_test_cases(id);

ALTER TABLE ONLY public.uat_test_results
    ADD CONSTRAINT uat_test_results_test_run_id_fkey FOREIGN KEY (test_run_id) REFERENCES public.uat_test_runs(id);

ALTER TABLE ONLY public.uat_test_runs
    ADD CONSTRAINT uat_test_runs_phase_b_run_id_fkey FOREIGN KEY (phase_b_run_id) REFERENCES public.phase_b_runs(id);

ALTER TABLE ONLY public.uat_triage_results
    ADD CONSTRAINT uat_triage_results_test_result_id_fkey FOREIGN KEY (test_result_id) REFERENCES public.uat_test_results(id);

ALTER TABLE ONLY public.uat_triage_results
    ADD CONSTRAINT uat_triage_results_test_run_id_fkey FOREIGN KEY (test_run_id) REFERENCES public.uat_test_runs(id);

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.verification_runs
    ADD CONSTRAINT verification_runs_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.agentic_runs(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.xsd_java_links
    ADD CONSTRAINT xsd_java_links_node_id_fkey FOREIGN KEY (node_id) REFERENCES public.xsd_schema_nodes(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.xsd_java_links
    ADD CONSTRAINT xsd_java_links_repo_id_fkey FOREIGN KEY (repo_id) REFERENCES public.code_repos(id);

ALTER TABLE ONLY public.xsd_schema_edges
    ADD CONSTRAINT xsd_schema_edges_from_node_id_fkey FOREIGN KEY (from_node_id) REFERENCES public.xsd_schema_nodes(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.xsd_schema_edges
    ADD CONSTRAINT xsd_schema_edges_to_node_id_fkey FOREIGN KEY (to_node_id) REFERENCES public.xsd_schema_nodes(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.xsd_schema_nodes
    ADD CONSTRAINT xsd_schema_nodes_repo_id_fkey FOREIGN KEY (repo_id) REFERENCES public.code_repos(id);

ALTER TABLE ONLY public.xsds
    ADD CONSTRAINT xsds_change_request_id_fkey FOREIGN KEY (change_request_id) REFERENCES public.change_requests(id);

ALTER TABLE ONLY public.xsds
    ADD CONSTRAINT xsds_override_uploaded_by_fkey FOREIGN KEY (override_uploaded_by) REFERENCES public.users(id);

ALTER TABLE ONLY public.xsds
    ADD CONSTRAINT xsds_uploaded_by_fkey FOREIGN KEY (uploaded_by) REFERENCES public.users(id) ON DELETE SET NULL;
