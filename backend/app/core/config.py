# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

import os

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache

# SCR findings #3/#9 (Hardcoded Password / Use Of Hardcoded Password) — this
# used to be a real literal ("dev-internal-token") shipped as the default for
# `cert_agent_internal_token` below. Kept only so `_check_internal_token_not_default`
# can keep refusing that specific legacy value if an old .env still sets it
# explicitly; it is no longer the default itself.
_LEGACY_DEV_INTERNAL_TOKEN = "dev-internal-token"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Don't crash startup when .env contains env vars that aren't
        # declared as fields here. Useful when a developer tunes a knob via
        # .env that was added on a feature branch but not yet in main, or
        # when a new engine-specific knob lands before the host Settings
        # adds the corresponding field. Unknown vars are simply ignored.
        extra="ignore",
    )

    # App
    app_name: str = "Change Management Platform"
    app_env: str = "development"
    secret_key: str
    # Key-encryption key (Fernet) for secrets stored in the app_configs table
    # (the admin-UI-managed *_api_key / *_token / *_password values). Env-owned
    # and never exposed via the admin UI — you cannot bootstrap DB-stored secrets
    # without it. Required to store secrets when app_env != "development".
    config_encryption_key: str = ""
    # Operator session (JWT) lifetime. The sliding-refresh middleware re-issues a
    # fresh token on activity past 50% TTL, so a SHORTER value acts as an idle
    # timeout (an idle session's token isn't refreshed and expires). InfoSec
    # phase 4: set to 30–60 for privileged environments; 480 (8h) is the legacy
    # operator default.
    access_token_expire_minutes: int = 480
    # Return the raw session JWT in the /auth/login, /auth/mfa/verify and
    # /auth/mfa/activate response BODIES, in addition to the httpOnly cookie
    # those endpoints always set.
    #
    # Default FALSE: a token in the response body is a token the browser can
    # put back into JavaScript-reachable storage, which is the weakness the
    # cookie migration exists to close. The SPA does not read it.
    #
    # Set true ONLY for a non-browser caller that cannot use a cookie jar and
    # needs to capture the Bearer token from a scripted login. Prefer keeping
    # it off and letting such callers hold the cookie instead.
    auth_return_token_in_body: bool = False
    # InfoSec phase 4 — fail CLOSED when Redis (which backs the brute-force
    # lockout, CAPTCHA and the JWT denylist) is unavailable: refuse logins with
    # 503 rather than silently losing those controls. Default off (availability).
    login_fail_closed_without_redis: bool = False
    # Self-hosted login CAPTCHA (InfoSec mandate). When enabled, GET /auth/captcha
    # mints a single-use image challenge and /auth/login requires the matching
    # answer. Answers are held in Redis, so verification fails CLOSED when Redis is
    # unavailable — flip this to false as the escape hatch (and for programmatic
    # login in e2e / eval automation, which cannot solve a CAPTCHA).
    captcha_enabled: bool = True
    captcha_ttl_seconds: int = 180
    captcha_length: int = 5
    # TOTP MFA (InfoSec phase 2). `mfa_issuer` labels the entry shown in the
    # authenticator app.
    mfa_issuer: str = "Change Management"
    # Dedicated Fernet key for encrypting TOTP seeds at rest (core/mfa.py).
    # Optional and additive: unset (default) keeps today's behaviour exactly —
    # the key is derived as base64url(SHA-256(secret_key)), unchanged. Set this
    # to a value from `Fernet.generate_key()` to use a real random key instead
    # of one derived from secret_key, which decouples MFA-seed encryption from
    # the same secret that signs every session JWT. Rotating this key (once
    # set) invalidates stored TOTP secrets exactly like rotating secret_key
    # does today — users re-enrol.
    mfa_encryption_key: str = ""
    # Platform-wide MFA switch (set via .env: MFA_ENFORCED=true|false). ON: every
    # user must use TOTP MFA — a user who hasn't set it up registers at first
    # login, enrolled users enter a code. OFF: no MFA prompt for anyone.
    # Enrolment persists per user, so toggling OFF then ON keeps existing setups
    # (users don't re-register); a user cannot self-disable while it is ON.
    mfa_enforced: bool = False
    # Hybrid LDAP/AD auth (InfoSec phase 3). When enabled, users whose row is
    # auth_source='ldap' — and unknown usernames — authenticate by BIND against
    # the directory; local users (incl. the break-glass admin) keep bcrypt. First
    # successful bind JIT-provisions the user and maps an AD group DN → app role.
    ldap_enabled: bool = False
    ldap_server_uri: str = ""              # e.g. ldaps://ad.example.internal:636
    ldap_bind_dn: str = ""                 # read-only service account DN
    ldap_bind_password: str = ""
    ldap_user_search_base: str = ""        # e.g. OU=Users,DC=example,DC=internal
    ldap_user_filter: str = "(sAMAccountName={username})"
    ldap_group_role_map: dict[str, str] = {}   # {group DN: app role}; first match wins
    ldap_ca_cert: str = ""                 # PEM path for LDAPS trust
    ldap_start_tls: bool = False           # ldap:// + STARTTLS instead of ldaps://

    # DEV ONLY — skip approval-row creation on BRD submit and auto-approve
    # the BRD instead. Bypasses the NotNull-on-approver_id failure when no
    # reviewer users exist in the dev DB. Refused at runtime when
    # app_env == production. Default off so production behaviour is unchanged.
    dev_skip_approvals: bool = False

    # Database
    database_url: str

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # AI Provider: "claude", "openai", "ainxt", "ollama", or "gemini"
    llm_provider: str = "claude"
    # Anthropic prompt caching (Claude / AiNxt-anthropic paths). Default ON — caches
    # the system prompt, the tools block, and a ROLLING message tail via cache_control,
    # so each agentic turn reprocesses only the newest turn instead of the whole growing
    # transcript (observed on Claude-direct: >80% of prompt tokens served from cache →
    # ~10x cheaper on the repeated prefix, and faster). Accuracy-NEUTRAL: a cache hit
    # replays byte-identical tokens — it never summarises or drops context. Non-Claude
    # providers never see cache_control (stripped); the AiNxt gateway honours the
    # tool/message markers but not system, and reports cache counters as 0 (savings real,
    # just invisible). Set false to strip ALL cache_control markers (to debug a caching
    # issue or a provider that mishandles it).
    prompt_cache_enabled: bool = True
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    claude_model: str = "claude-sonnet-5"
    openai_model: str = "gpt-4o"
    gemini_model: str = "gemini-3.5-flash"
    gemini_thinking_level: str = "minimal"
    gemini_thinking_budget: int = 0
    # No default: this named an internal gateway host. Deployments set
    # AINXT_BASE_URL; llm.py raises a clear error if the provider is "ainxt"
    # and it is unset, rather than silently dialling a host that only resolves
    # inside one network.
    ainxt_base_url: str = ""
    # Postal/contact line rendered at the foot of generated .docx documents.
    # Empty by default: this is the deploying organisation's identity, not
    # platform code, and a fork must not emit someone else's address.
    doc_footer_text: str = ""
    ainxt_api_key: str = ""
    ainxt_model: str = "gpt-4o"
    # "openai" = /chat/completions (current default), "anthropic" = /v1/messages
    ainxt_compat_mode: str = "openai"
    # Model to use when ainxt_compat_mode="anthropic" (AiNxt normalises aliases)
    ainxt_messages_model: str = "claude-sonnet-5"
    # Ollama chat (uses settings.ollama_url for base, appends /v1).
    # Quality caveat: small local models may struggle with the engine's
    # heavy prompts. Tested baseline was gpt-oss:120b-cloud.
    ollama_chat_model: str = "phi3:mini"

    # Deep Research stage — overrides the default LLM for the research-heavy
    # multi-page ecosystem analysis. Empty string ("") = inherit `llm_provider`
    # / per-provider model. Override via env or admin UI to use a different
    # model for this stage only without affecting BRD/TSD/Product Kit.
    # Default to Claude (an id that actually exists) so a fresh deploy can't crash
    # deep research with a non-existent model; override per-env via DEEP_RESEARCH_*.
    deep_research_provider: str = "claude"
    deep_research_model: str = "claude-sonnet-5"

    # ── Video Generation (Phase A Product Kit promo/explainer videos) ──────
    # Each video model caps a single clip at ~8s, so the script is split into
    # 8s segments, each generated as its own clip, then merged (ffmpeg concat).
    video_generation_enabled: bool = True
    # Active provider: "ainxt" (gateway, proven), "gemini" (Veo direct), "grok"
    video_provider: str = "ainxt"
    video_model: str = "veo-3.1-generate-preview"
    video_aspect_ratio: str = "16:9"
    video_segment_max_sec: int = 8
    # Per-doc-type default durations (seconds); overridable per generation.
    promo_video_duration_sec: int = 30
    explainer_video_duration_sec: int = 45
    # AiNxt video routes (reuse ainxt_base_url). Fetch is
    # "{ainxt_video_fetch_path}/{video_id}". Request body:
    # {prompt, chat_id, aspect_ratio, duration_secs}.
    ainxt_video_generate_path: str = "/chat/video-generate"
    ainxt_video_fetch_path: str = "/chat/video"
    # Separate key for the video gateway (own budget/quota). Empty = fall back
    # to ainxt_api_key (the chat/LLM key).
    ainxt_video_api_key: str = ""
    # Grok (xAI) direct
    grok_api_key: str = ""
    grok_base_url: str = "https://api.x.ai/v1"
    grok_video_model: str = "grok-imagine-video"
    # Gemini/Veo direct (reuse gemini_api_key)
    gemini_video_model: str = "veo-3.1-generate-preview"
    gemini_video_base_url: str = "https://generativelanguage.googleapis.com/v1beta"

    # Email
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = "noreply@example.com"

    # Stamped into the JWT `iss` claim. Nothing in this codebase VERIFIES it
    # (grepped before making it configurable), so changing it cannot
    # invalidate a live session -- but it is visible in any issued token.
    jwt_issuer: str = "change-management-platform"

    # Storage
    artifacts_dir: str = "/app/artifacts"

    # ── Policy doc (feasibility resolver) ──────────────────────────────
    # Path to the bind-mounted seed copy of the policy doc. Read by the
    # startup seeder when the policy singleton DB row is empty. Admin UI
    # writes to the DB row thereafter; this file remains as the reset
    # source. Override via AUTHORITY_POLICY_PATH env for non-standard deploys.
    authority_policy_path: str = "/app/data/authority_policy.md"
    knowledge_base_dir: str = "/app/knowledge_base"
    # Auto-ingest the filesystem-backed knowledge base during app startup.
    # WHY this exists: after the doc-generation-wiring merge fallout, a manual
    # `/rag/ingest` step was easy to forget, which left newly mounted KB
    # folders invisible to RAG until someone triggered ingestion by hand.
    # Defaulting this on makes the app honour the folder structure under
    # `knowledge_base_dir` as soon as the backend boots.
    auto_ingest_knowledge_base_on_startup: bool = True
    # Leave this off by default so startup only ingests new/changed files.
    # Operators can flip it on when they intentionally want a full re-chunk.
    startup_ingest_force: bool = False
    # Run startup KB ingest/BM25 build in the background so the API can begin
    # serving requests immediately instead of making the UI wait for a long
    # synchronous startup pass.
    startup_ingest_background: bool = True
    # Folder/file prep can parallelize safely because parsing/chunking is
    # CPU/I/O work with no shared SQLAlchemy session state. DB writes remain
    # sequential inside ingest_all().
    kb_ingest_parallelism: int = max(2, min(8, (os.cpu_count() or 4)))

    # Ollama (embeddings)
    ollama_url: str = "http://localhost:11434"

    # External the Authority Simulator UI. Surfaced by the sidebar's "the Authority Simulator"
    # link. Override via env or the admin Config UI for non-localhost
    # deployments (Ubuntu host-mode, staging, production).
    authority_simulator_url: str = "http://localhost:5173"

    # Slice 3 — Tree-sitter AST code chunker feature flag.
    # True (default): tree-sitter path runs for java / python / typescript /
    # javascript (and is the prerequisite for the symbol-graph extractor below —
    # edges attach to tree-sitter chunks). Unsupported languages still fall back
    # to legacy regex (Java only). Set False to restore the legacy regex chunker.
    # NOTE: changing the chunker changes how code is split for retrieval, so it
    # only takes effect on the next repo RE-INGEST.
    use_tree_sitter_chunker: bool = True

    # Slice 4 — 3-view code embedding feature flag. Requires tree-sitter chunker
    # (Slice 3) to be on to have any effect. When on, each tree-sitter symbol
    # chunk becomes up to 3 rows in document_chunks (body / signature /
    # nl_summary), each with its own embedding, sharing a parent_symbol_id.
    # Retrieval dedups by parent_symbol_id so top-k doesn't repeat the same
    # symbol via multiple views.
    use_code_multiview_embedding: bool = False

    # Slice 5 — Query understanding (rewriter + HyDE + NER) feature flag.
    # When on, retrieve() does a single LLM call to enrich the user query
    # (sub-questions, entities, HyDE hypothetical answer), then runs multi-pass
    # hybrid_retrieve across the variants and fuses via RRF. NER is captured
    # but unused for retrieval until Slice 19 (knowledge graph) lands.
    # Default off — enables measurable A/B against the Slice 4 baseline.
    use_query_understanding: bool = False

    # Slice 6 — Cross-encoder reranker. When on, retrieve() over-samples
    # `top_k * 5` candidates, runs them through the model, keeps top_k by
    # rerank score. Lazy-loaded on first call (~600MB model download, then
    # cached). Plan §6.3 — target +5-15pp recall@10 lift. Fail-open: if the
    # model package or weights are unavailable, the original RRF order is
    # preserved.
    use_reranker: bool = False
    reranker_model: str = "BAAI/bge-reranker-v2-m3"

    # Reranker backend selection. Two modes are configurable end-to-end:
    #   "remote" — POST to an external HTTP service exposing a `/rerank`
    #              endpoint (set RERANKER_URL). THE DEFAULT since 2026-08-28.
    #              Zero PyTorch / model-cache footprint inside this process.
    #   "local"  — load CrossEncoder weights in-process. Still fully supported
    #              CODE, but the libraries it needs (torch,
    #              sentence-transformers) are NO LONGER INSTALLED in the
    #              backend image, so it will fail open unless you add them
    #              back yourself.
    #
    # WHY THE DEFAULT FLIPPED. torch and sentence-transformers accounted for 6
    # of the 18 violations in the 2026-08-27 A2A compliance SBOM, including
    # CVE-2026-68770 at CVSS 9.8. They now live in the `reranker` sidecar
    # (services/reranker) instead of in the component that holds the platform's
    # data. Reranking itself is unchanged — same model, same scores — so this
    # is a deployment-topology change, not a capability loss.
    #
    # Leaving this at "local" while use_reranker is ON is the one dangerous
    # combination, because the reranker fails OPEN: search would silently lose
    # its +5-15pp recall@10 with no error. startup_validation.py::
    # validate_reranker_backend reports exactly that at "high" severity so the
    # degradation is visible instead of silent.
    #
    # Anything unrecognised falls back to "local" with a warning.
    reranker_backend: str = "remote"
    # Used only when reranker_backend == "remote". Full URL including path.
    # Compose supplies http://reranker:8200/rerank; left empty here so a
    # non-compose deployment must state its own endpoint rather than silently
    # inheriting a hostname that does not resolve. When empty with
    # use_reranker on, validate_reranker_backend() flags it at startup.
    reranker_url: str = ""
    # Per-request HTTP timeout when calling the remote reranker. Service
    # itself runs CrossEncoder.predict() in a thread pool so a busy server
    # can sit on a request for several hundred ms.
    reranker_timeout_sec: float = 10.0
    # Deadline for the LOCAL backend's first-call model load. The ~600MB download can
    # STALL with no network timeout and hang the whole agent loop (this is the "stuck on
    # Analyzing existing schemas" failure). When the load exceeds this, we fail open to RRF
    # order and disable reranking for the process. In prod, pre-cache the model or use
    # reranker_backend=remote to avoid the load entirely.
    reranker_load_timeout_s: int = 45

    # Slice 7 — Hierarchical markdown chunker. When on, .md files are chunked
    # into parent (whole section) + child (paragraph) rows with breadcrumb
    # paths and parent_chunk_id linkage. Non-markdown files stay on the
    # legacy RecursiveCharacterTextSplitter regardless of this flag.
    # Retrieval's `deprecated IS NOT TRUE` filter is always on (independent
    # of this flag) — harmless on the existing NULL-deprecated corpus.
    use_hierarchical_chunker: bool = False

    # Slice 8 — Context compression. When on AND a query is supplied to
    # build_context(), each chunk is reduced to query-relevant sentences via
    # a per-chunk LLM filter (parallel across chunks). Plan §6.4 target:
    # 3-6× token reduction with minimal recall loss. Fail-open: LLM error
    # or empty result returns the original chunk unchanged.
    use_context_compression: bool = False

    # Slice 9 — Citation enforcement (pattern: deep_researcher only; sub-slices
    # 9a/b/c replicate for BRD/TSD/Canvas). When on, the RAG context is
    # formatted with numbered source markers `[N]` and the agent prompt
    # demands inline `[N]` citations on every factual claim. A post-generation
    # `citation_validator` parses the output and reports uncited claims.
    # Plan §6.5: "no citation = no claim" — hallucination defense.
    use_citation_enforcement: bool = False

    # Slice 11 — ADR contradiction check. When on, BRD and Tech Spec system
    # prompts gain an appendix instructing the LLM to cross-check its design
    # against retrieved ADRs / prior decisions and emit a clearly-labelled
    # `## Design Review Concerns` section when contradictions exist. A pure
    # post-hoc parser (`adr_checker.extract_concerns_section`) can then
    # surface flagged concerns to reviewers. Plan §7.2.
    use_adr_contradiction_check: bool = False

    # Slice 14 — Sandboxed code execution (Docker-in-Docker via docker SDK).
    # `run_in_sandbox` uses these as defaults when callers don't override.
    # No production caller yet — Slice 15 (self-correction loop) is the first
    # consumer. Flag `sandbox_enabled` is reserved for future wiring; the
    # module itself checks `is_docker_available()` at call time.
    sandbox_enabled: bool = False
    sandbox_image_java: str = "maven:3.9-eclipse-temurin-21"
    sandbox_timeout_seconds: int = 120
    sandbox_memory_limit: str = "512m"
    sandbox_cpu_limit: float = 1.0

    # Slice 15 — Self-correction loop. When enabled, the editor's output is
    # compiled in the sandbox; on non-zero exit the stderr is fed back to an
    # LLM fix-generator, up to `self_correction_max_iterations` times. Plan
    # §7.4 — one of the strongest 60→90% code-accuracy levers. Flag reserved
    # for future wiring; the orchestrator module is standalone.
    use_self_correction: bool = False
    self_correction_max_iterations: int = 3

    # Slice 16 — AST editor (aider-style SEARCH/REPLACE patches). When on,
    # the editor agent emits targeted patch blocks instead of full-file
    # rewrites; `ast_editor_java.apply_patches` applies them to the current
    # files. Plan §7.4. Flag reserved for future wiring; the module is
    # standalone and the current streaming agent still produces full-file
    # output.
    use_ast_editor: bool = False

    # Slice 17 — Symbol-graph extractor (tree-sitter-based, Java only in
    # this slice; Python/JS/TS follow in sub-slices). When on, code
    # ingestion enriches each chunk with imports / inherits / implements /
    # calls / called_by (within-file). Plan §4.2.2. Does nothing when
    # USE_TREE_SITTER_CHUNKER (Slice 3) is off — needs the tree-sitter
    # chunks to attach edges to (now default True above).
    # True (default): the symbol graph is built at ingest so the impact
    # analyzer can compute blast-radius. Populates on the next RE-INGEST.
    use_symbol_graph_extractor: bool = True
    # Fetch a repo's files in ONE GitLab archive (tarball) download instead of one API call per file.
    # Turns ~100+ sequential GitLab requests (slow, rate-limit-prone — the "stuck on cloning" symptom)
    # into a single download + local extract. Fail-open: any archive error falls back to the per-file
    # fetch, so correctness is unchanged. Default True.
    use_gitlab_archive_fetch: bool = True

    # Slice 18 — Doc↔code linker. Runs as a standalone post-ingest pass
    # (not embedded in the ingestion loop). For each doc chunk, finds
    # candidate code symbols, LLM-scores 0-1 confidence, writes edges to
    # the doc_code_links table above `doc_code_link_min_confidence`.
    # Plan §4.3.
    use_doc_code_linking: bool = False
    doc_code_link_min_confidence: float = 0.6
    doc_code_link_max_candidates: int = 5

    # Slice 19 — Apache AGE knowledge graph hosted alongside pgvector in the
    # same Postgres instance. `kg_graph_name` identifies the AGE named graph,
    # created idempotently by alembic 0020. Cypher queries run via
    # `SELECT * FROM cypher('<graph_name>', $$...$$) AS (...)`. Plan §5.1.
    kg_graph_name: str = "npci_kg"

    # Sub-slice 19a — Projection of existing RAG data into the AGE graph.
    # Standalone post-ingest pass (not wired into production agents). When
    # enabled, `app.kg.ingest_from_rag.ingest_from_db(db)` can be invoked
    # from an admin endpoint / CLI to materialize Document, DocChunk, File,
    # Class, Function nodes + DESCRIBES/CALLS/INHERITS/IMPLEMENTS edges.
    # Idempotent (MERGE-based). No production caller this slice.
    use_kg_ingestion: bool = False

    # Slice 20 — Graph-traversal retrieval pass. When on, `graph_retrieve(query)`
    # extracts symbol-name seeds from the query, does 1-hop traversal in the
    # AGE graph (CALLS/INHERITS/IMPLEMENTS outbound+inbound, plus DocChunks
    # that DESCRIBE the seed), hydrates the chunk_ids back into DocumentChunk
    # rows, and returns them with hop-distance-based scores. No production
    # caller this slice — standalone module, wire into retrieve() as an extra
    # RRF-fused pass in a follow-up.
    use_graph_retrieval: bool = False
    graph_retrieval_max_seeds: int = 5
    graph_retrieval_max_hops: int = 1

    # Slice 21 — Impact analyzer. Inbound graph traversal to compute the
    # "blast radius" of a change: who CALLS / who INHERITS / who IMPLEMENTS
    # / which DocChunks DESCRIBE the targets. CALLS + INHERITS are BFS up
    # to `impact_max_hops`; IMPLEMENTS and DESCRIBES stay 1-hop. Wired into the
    # code-change / BRD / TSD prompts via build_impact_block (fail-open: empty
    # block until the symbol graph is populated). True (default): runs over the
    # SQL graph backend below. Set False to suppress the blast-radius block.
    use_impact_analyzer: bool = True
    impact_max_hops: int = 2
    # Which backend computes the context-pack blast radius (context_assembler
    # ._impact_files). "sql" (default) traverses the call edges already
    # materialised in document_chunks via kg/sql_graph — no Apache AGE, no
    # Cypher, savepoint-scoped so a query error can't poison the caller's
    # transaction. "age" keeps the legacy Cypher path (requires the AGE
    # extension). Both fail-open to an empty advisory block.
    impact_backend: str = "sql"

    # Slice 23 — Python LSP cross-file call resolution. When on, the
    # polyglot ingest path spawns a multilspy session over the temp repo
    # checkout and resolves each within-file `calls` entry to a
    # (callee_path, callee_symbol) pair. Stored to the new
    # `cross_file_calls` JSON column on document_chunks. Default OFF —
    # multilspy spawns a Python LSP subprocess which is slow to start and
    # adds cost to every ingest run.
    use_python_lsp: bool = False
    python_lsp_timeout_seconds: int = 60

    # Slice 24 — TypeScript LSP cross-file call resolution. Same plumbing as
    # Slice 23 (Python) — populates `cross_file_calls` JSON column for TS/JS
    # chunks and projects into AGE via Slice 23a's existing CALLS edge
    # builder. Independent feature flag so operators can enable per-language.
    # Default OFF; tsserver init is slow on first call (~5-10s).
    use_typescript_lsp: bool = False
    typescript_lsp_timeout_seconds: int = 60

    # Sub-slice 24a — Java LSP cross-file call resolution. Same plumbing as
    # Slices 23/24 — populates `cross_file_calls` JSON column for Java
    # chunks, projects into AGE via Slice 23a's CALLS edge builder.
    # multilspy routes through eclipse-jdt-language-server which is JVM-
    # backed and slow to initialise on first request — default timeout is
    # 90s vs 60s for Python/TS.
    use_java_lsp: bool = False
    java_lsp_timeout_seconds: int = 90

    # Slice 26 — Event-sourced incremental ingest. When on, polyglot
    # ingestion hashes each file and only re-processes modified/added
    # files since the last run; unchanged files are skipped (saving
    # chunking + embedding + LSP cross-file work). State persisted in
    # the `code_repo_file_state` table.
    use_incremental_ingest: bool = False

    # Sub-slice 19a-scheduler — Celery-backed periodic incremental
    # re-ingest. Iterates every CodeRepo row, calls
    # `ingest_polyglot_repo_incremental`, then (optionally) refreshes
    # the AGE knowledge graph via `ingest_from_db`. Default OFF — must
    # be opted-in by operators since it spawns up to 4 LSP servers per
    # tick. Interval is configurable; default 30 minutes is a balance
    # between freshness and ingest cost. `scheduler_ingest_languages`
    # gates which languages are eligible per tick — set to a smaller
    # subset (e.g. just Java) if the LSP cost is too high.
    use_scheduled_ingest: bool = False
    scheduler_ingest_interval_minutes: int = 30
    scheduler_ingest_languages: list[str] = ["java", "python", "typescript", "javascript"]
    scheduler_kg_projection: bool = True

    # ── Agentic XSD-driven code change (THE BOOK v3.3) ────────────────────────
    # Global gate for the durable, resumable agentic Phase-B state machine. When on,
    # admin/tech-lead users ALWAYS take the agentic path for the XSD + code stages
    # (it is the only codegen path for those roles — no per-change opt-in). The
    # `change_requests.agentic_enabled` column is retained but no longer gates the UI.
    # Default ON — this is the production codegen path. Any env that wants the legacy
    # single-shot path instead sets USE_AGENTIC_TOOL_LOOP=false in its .env.
    use_agentic_tool_loop: bool = True
    # Accuracy upgrade S2 — the code-grounded Change Analysis stage (kind="analysis").
    # ON by default: this branch ships the analysis flow as the production planning
    # path — the clarification slot runs the analysis agent and the BRD/TSD/Phase-B
    # consume the Decision Ledger. A deployment that wants the legacy single-shot
    # clarification stage instead sets USE_CHANGE_ANALYSIS=false in its .env.
    # See docs/PLAN_AGENTIC_ACCURACY.md S2/S3.
    use_change_analysis: bool = True
    # ACCURACY-FIRST backstop: the analysis agent stops when it submits a plan/clarifications;
    # raised so deep code reading (the basis for an accurate plan) isn't cut short.
    agentic_analysis_max_iterations: int = 80
    # Cross-drive re-exploration fix: on a re-drive (PM answered clarifications / plan
    # revision) the analysis agent used to REBUILD its conversation from scratch and
    # re-read the whole codebase from turn 1. When True, the first drive's transcript is
    # persisted and REPLAYED — the answers/feedback arrive as the tool_result of the prior
    # gate call, so the agent continues from where it left off instead of re-exploring.
    # Falls back to fresh exploration if the saved transcript is missing/unusable.
    agentic_analysis_replay_transcript: bool = True
    # Same fix for the CODE loop — the bigger win. Continuation rounds (cap hit, plan-gap /
    # review redrives) and verify/review fix rounds used to re-instantiate the implementer
    # with only a ~4KB string summary (final_text[:1200] + read-path names) of everything it
    # had learned, then tell it "do NOT re-explore" — the author of the code lost its own
    # memory between rounds, which is the root non-convergence gap vs Claude Code. When True,
    # the prior round's transcript is CONTINUED (feedback/gaps arrive as the next user turn);
    # history compaction keeps the growing transcript inside the window. Falls back to the
    # fresh-prompt round on any replay problem.
    agentic_code_replay_transcript: bool = True
    # Namespace prefix for the branch the push phase creates (was hardcoded "feature").
    # Distinguishes agent-authored branches from human ones — the branch becomes
    # "<prefix>/xsd-<slug>" (e.g. atom/xsd-refund-status). Case-preserved;
    # override per env with AGENTIC_BRANCH_PREFIX.
    agentic_branch_prefix: str = "atom"
    # And for the APPROACH-PROPOSAL phase: propose re-runs the same read-only discovery sweep
    # analysis just finished (same 14 tools, same flows/handlers/XSDs) purely to frame
    # reuse-vs-new options. When True, propose CONTINUES the ratified analysis run's persisted
    # transcript (looked up by change_request_id) — the ratification notice arrives as the
    # propose_plan gate's tool_result. Skipped when the repo selections differ; falls back to
    # fresh exploration on any replay problem.
    agentic_propose_replay_transcript: bool = True
    # Index-time module-wise hierarchical context generation (§19, "the heart").
    # When on, the code-index pipeline also writes `module_context` rows in
    # parallel with the RAG ingest. On by default — module orientation
    # materially improves agentic accuracy on multi-module repos.
    use_module_context_generation: bool = True
    # Index-time API FLOW-MAP generation (THE BOOK v3.4 reuse-first §). When on, the
    # index pipeline also writes a per-repo `flow_context` row (which API carries the
    # transaction/credit-debit leg vs the meta APIs, + multi-leg sequences) in parallel
    # with module_context. The reuse-first approach gate pulls it so the agent reasons
    # over a ready flow map instead of rediscovering it each run. Advisory; sha-stamped.
    use_flow_context_generation: bool = True
    # Whether the Code-Change agent authors unit tests alongside the production change. OFF: teams land
    # tests separately, so the agent must NOT author test files — writing them burns budget/time and is
    # not the team's workflow. Reverted from the WS3a experiment after it made runs slower/costlier with
    # no proven quality win. Read at call time so a flip applies to the next run without a redeploy.
    agentic_write_unit_tests: bool = False
    # S2 — durable state-machine plumbing (§3/§21).
    # Lease TTL: a run's worker renews this by heartbeat; the `agentic.recover`
    # beat reclaims runs whose lease has expired (worker crashed mid-phase).
    # Lease TTL. The agent loop heartbeats this every iteration, so a healthy long
    # phase keeps it; if the worker dies (crash/OOM) the lease expires within this
    # window and the recovery beat re-drives the run. Kept modest so crash-recovery
    # is minutes, not 10+ min.
    agentic_lease_ttl_seconds: int = 300
    # Cadence of the recovery beat (how often expired-lease runs are swept).
    agentic_recover_interval_seconds: int = 120
    # Transient/network failures PAUSE-and-resume (re-dispatch with backoff) instead
    # of failing the run — like Claude Code continuing after a network blip. Capped so
    # a misclassified persistent error can't loop forever.
    agentic_max_transient_resumes: int = 20
    # Per-run coding activity log (§21): `<dir>/<run_id>.jsonl`, secret-redacted.
    coding_log_dir: str = "/app/logs/coding"
    # DEBUG-only: dump the FULL prompt (system + messages) and FULL response of every
    # agentic-loop LLM call (analysis / approach / xsd_discovery / code_change / …) to
    # `<dir>/<run_id>/<agent>_iterNNN_<ms>.json`. The coding log only keeps 4000-char
    # heads; this captures verbatim bytes for debugging what the model saw + returned.
    # OFF by default — payloads are large and contain repo source. Blank dir → auto-resolve
    # to the coding-log dir's sibling `transcripts/`.
    agentic_dump_transcripts: bool = False
    agentic_transcript_dump_dir: str = ""
    # Per-change transcript tree (app.core.transcripts) — the human-navigable log layout:
    #   <transcript_dir>/<change_id>/<NN_stage>/iterNNN_<ms>.json  (+ 08_codegen/<run_id>/<agent>/)
    # ONE JSON file per LLM call across the WHOLE change lifecycle (prompt enhancement →
    # deep research → canvas → BRD → tech spec → planning → codegen), so every artefact a
    # change produced is grouped and ordered under one folder. Best-effort + secret-redacted.
    # OFF by default — same posture as `agentic_dump_transcripts`: payloads are large (full
    # prompts + the growing message history) and this fires on EVERY LLM call, so it is an
    # explicit opt-in (dev/debug), not a prod default. Left ON in prod it stalls the async
    # LLM path with per-call disk writes and — by spacing cacheable calls past Anthropic's
    # 5-min prompt-cache TTL — silently collapses cache reuse. When True, it also drives the
    # agentic-loop dump regardless of `agentic_dump_transcripts` (a legacy independent toggle).
    transcript_capture: bool = False
    # Diagnostics logging (app.core.diag) — dedicated, findable, fail-open files
    # for the code-gen pipeline and the target build. Blank → auto-resolve to the
    # first writable of: <running-code>/logs/diagnostics, ~/.atom-diag, $TMPDIR.
    # Set DIAG_LOG_DIR to pin an explicit location.
    diag_log_dir: str = ""
    # When True, also write codegen.debug.log (DEBUG) + command output tails.
    # Off by default so the normal files stay small; flip on to chase a bug.
    diag_verbose: bool = False
    # S4 — workspace + execution (§6/§18).
    # One leased clone tree per run: <root>/<run_id>/<repo_id>/. GC removes a
    # workspace only when its run is terminal AND past TTL AND lease-free.
    agentic_workspace_root: str = "/app/workspace"
    agentic_workspace_ttl_hours: int = 24
    # Default per-command timeout for the platform-aware run_command (§18.2);
    # mvn builds are slow, so this is generous. Callers override per command.
    agentic_command_timeout_s: int = 1800
    # Address-space cap (MB) applied via RLIMIT_AS on POSIX child processes
    # (containment, not a sandbox — §6/§17). 0 disables the cap.
    agentic_rlimit_as_mb: int = 0
    # Max heap (MB) for the Maven/JVM build the verifier spawns (injected as
    # `-Xmx<N>m` into MAVEN_OPTS). Without a cap the build JVM uses the default
    # heap (≈¼ of host RAM), which under the full docker stack pushes the VM past
    # its memory ceiling → the kernel OOM-kills the celery worker mid-verify
    # (SIGKILL, run wedged). Bounds the build so it fits. 0 disables the cap.
    agentic_maven_heap_mb: int = 512
    # Refuse to start a new workspace clone when free space on the workspace volume
    # is below this (MB) — clones are 200MB-2GB, so a full disk wedges every run with
    # a cryptic error. 0 disables the preflight. Default ~3GB headroom.
    agentic_min_disk_free_mb: int = 3000
    # ── A17 (architecture review Medium #17, "Workspace GC is Time-Based,
    # Not Quota-Based") — the hourly TTL-based GC (`workspace_local.
    # gc_workspaces`) can let up to `agentic_workspace_ttl_hours` worth of
    # completed-run workspaces (200MB-2GB each) accumulate before the next
    # sweep. These two knobs add a QUOTA-based backstop: whenever the GC
    # sweep runs, it ALSO checks total size/count of the workspace root and,
    # if over quota, evicts additional TERMINAL+lease-free+push-complete
    # workspaces (oldest `updated_at` first) even if they haven't hit their
    # TTL yet. 0 disables the corresponding check (TTL-only behaviour,
    # unchanged from before this fix).
    agentic_workspace_max_total_mb: int = 51200          # 50 GB default ceiling
    agentic_workspace_max_count: int = 200

    # ── Finding #8 (architecture review Advisory, "No Data Tiering for
    # Large Payloads") — non-destructive compression-then-archive-flag
    # tiering. Two-stage, both stages are ADDITIVE ONLY — neither stage
    # deletes or modifies a source row's own columns:
    #
    #   Stage 1 (compress): a periodic sweep finds tech_specs/brds/
    #   a2a_messages rows older than `artifact_coldstore_after_days` whose
    #   owning change is in a terminal state, gzip-compresses a COPY of
    #   their large content into workspace-local disk (reusing the same
    #   `./artifacts` volume every service already mounts — no new
    #   infrastructure to provision), and records a manifest row in
    #   `artifact_cold_storage`. The source row is untouched.
    #
    #   Stage 2 (flag for archive): a separate sweep marks coldstore
    #   manifest entries older than `artifact_archive_after_days` as
    #   `ready_for_archive=True`. This is a FLAG an operator/ops process
    #   consumes to move files to real archive storage (S3 Glacier, tape,
    #   etc.) on their own schedule — nothing in this codebase performs
    #   that move or deletes anything automatically.
    artifact_tiering_enabled: bool = False
    artifact_coldstore_dir: str = "/app/artifacts/coldstore"
    artifact_coldstore_after_days: int = 30
    artifact_archive_after_days: int = 90
    # Safety floor: never compress/tier a document belonging to a change
    # request that is not yet COMPLETED — an in-flight change's BRD/TSD
    # must stay fully "hot" (uncompressed, instantly readable) regardless
    # of its age, since an active change can still be revised.
    artifact_tiering_min_change_status: str = "completed"
    # Per-sweep cap so a first-ever run against a large backlog does not
    # try to compress thousands of rows in one Celery task invocation.
    artifact_tiering_batch_size: int = 200

    # ── A3 (architecture review Critical #16, "No Bounded Queue or
    # Backpressure on Agentic Runs") — global cap on concurrent agentic
    # runs across the whole platform. `uq_agentic_runs_active` (alembic
    # 0078) only enforces "at most one active run PER change request" — it
    # does not bound the TOTAL number of change requests running
    # concurrently across all Celery workers. Each run clones a repo
    # (200MB-2GB), holds a workspace on disk, makes 20-100 LLM calls, and
    # runs Maven builds (JVM heap) — unbounded concurrency here is what
    # OOM-kills workers under a burst of simultaneous changes. 0 disables
    # the cap (unbounded — not recommended for production).
    agentic_max_concurrent_runs: int = 5
    # How long agentic.drive waits (via Celery's own retry/countdown, not a
    # blocking sleep) before re-checking the cap when it is currently full.
    agentic_concurrency_requeue_delay_s: int = 30
    # S6 — the bounded read→think→tool→result loop (§8). Per-BATCH turn cap; on a
    # cap the code phase auto-continues (below) rather than truncating the change.
    # ACCURACY-FIRST BACKSTOP: a healthy round converges when the agent declares done long
    # before this; raised so a big change rarely resets context mid-flight. Cost is accepted.
    agentic_max_iterations: int = 100
    agentic_max_output_tokens: int = 16000
    # DEPRECATED — no longer enforced. The loop used to FAIL a run when cumulative spend crossed
    # this; that killed legitimate large changes (the growing history is re-sent uncached every
    # turn, so spend balloons on context rot, not real work). We now manage the context WINDOW
    # (compact + continue, like Claude Code) instead of capping total spend. Kept only for
    # telemetry/back-compat; the loop is bounded by the iteration + continuation caps.
    agentic_max_tokens_per_run: int = 2_000_000
    # ── A6 (architecture review High #5) — re-enforced runtime token budget guard.
    # `agentic_max_tokens_per_run` above stopped being read by the loop (see its
    # DEPRECATED note); this is the ACTIVE replacement. Unlike the old cap, this
    # does not kill the run outright — it warns at `agentic_token_budget_warn_fraction`
    # and, at `agentic_token_budget_enforce=True`, refuses to start a further
    # iteration once cumulative spend for the run crosses `agentic_token_budget_hard_cap`
    # (soft-stop: the run is marked "budget_exceeded" so it can be resumed with a
    # raised budget rather than silently truncated mid-turn). 0 disables the guard.
    agentic_token_budget_hard_cap: int = 4_000_000
    agentic_token_budget_warn_fraction: float = 0.8
    agentic_token_budget_enforce: bool = True
    # Context-window management (Claude-Code style) — default ON. We do NOT fail on token count.
    # When the input the model receives approaches its context WINDOW (or, when a provider strips
    # usage, when accumulated tool output passes the char fallback), the loop COMPACTS the
    # conversation — evicting the bulky, reconstructable tool outputs while keeping ALL reasoning +
    # the first brief + the recent tail — and CONTINUES. An evicted read is reconstructable (the
    # read_file content cache serves an unchanged file), so this is lossless-by-reconstruction.
    # NOTE: history compaction is now ALWAYS ON — it is core safety (disabling it only invited
    # context overflow), not a knob, so agentic_compact_history was removed (2026-07-24).
    # Compact the conversation once input nears this many tokens. This is a COST/CONTEXT POLICY,
    # not the model's hard limit: claude-sonnet-4-6 (and the whole Opus/Sonnet 4.6+ family) has a
    # NATIVE 1M-token window — no `context-1m` beta header is required (that header was a Sonnet-4-era
    # opt-in and is obsolete on current models). So overflow is NOT the concern here; we deliberately
    # compact far below the real ceiling to keep per-turn context (and cost) down, relying on the fact
    # that eviction is lossless-by-reconstruction (an evicted read is re-served from the read cache).
    # 300_000 is the policy (below the models' real 1M native window): compaction fires at the 0.8
    # fraction below → 240K tokens, leaving 60K headroom for the in-flight turn. This trades some
    # in-context memory fidelity for lower per-turn token spend (cache reads scale with transcript
    # size), which is the point of setting it below the native ceiling. Do NOT lower much further:
    # the retained floor after compaction (system + ground-truth + summary + kept turns) reached
    # ~183K on a real multi-file Phase-B change, and a 200K window (trigger 160K < floor) caused
    # compaction thrash (run 88c662c5). Raise toward 1_000_000 for maximal fidelity. The value MUST
    # stay ≤ the model's real window (1M for 4.6+ models, 200K for Haiku) — a value ABOVE the real
    # window would let a request overflow before compaction fires.
    agentic_context_window_tokens: int = 300_000
    agentic_compact_at_fraction: float = 0.8          # compact when input ≥ this fraction of the window-policy
    # Char fallback for providers that STRIP token usage (AiNxt): with no input_tokens the token
    # trigger above is blind, so accumulated tool-output chars stand in. Calibrated to the 300K
    # compaction POLICY above: ~4 chars/token → 900K chars ≈ 225K tok, firing with headroom before
    # the 240K (0.8 × 300K) policy trigger. Re-tune this alongside agentic_context_window_tokens
    # if you change the policy.
    agentic_compact_history_threshold_chars: int = 900_000
    agentic_compact_keep_recent_turns: int = 6
    # On compaction, FIRST capture an LLM PROGRESS SUMMARY (goal, files + key facts/signatures, edits
    # made, current plan, what's left) and fold it into the brief — so even if a detail was evicted
    # the agent stays directed and can re-read for the rest (this is what Claude Code does). Fail-open:
    # if the summary call fails, deterministic eviction still runs.
    # NOTE: the summary is now ALWAYS captured before eviction — it is the safety net that keeps the
    # agent oriented, not a knob, so agentic_compact_summary was removed (2026-07-24).
    agentic_compact_summary_max_tokens: int = 2000
    # Turn-level recovery (grok-build-parity, 2026-07): a turn that stopped at max_tokens
    # (stop_reason='max_tokens' — output CUT mid-thought, possibly mid-tool_use) is DISCARDED
    # and retried once with a doubled output budget instead of letting the truncated turn
    # enter history as if complete; an empty turn (no usable assistant blocks) is retried
    # once before being treated as "model done". Both retries are intra-iteration (they do
    # not consume the iteration cap). AiNxt-openai strips stop_reason, so the truncation
    # branch simply never fires there (fail-open by construction).
    agentic_turn_recovery: bool = True
    # In-loop convergence nudge (P1, TodoGate analog): when the model tries to end its turn
    # ("completed") while the orchestrator-supplied completion check still reports unmet
    # deliverables (unsatisfied acceptance predicates), the loop injects at most this many
    # corrective nudges listing them before accepting the stop — far cheaper than a full
    # orchestrator continuation round (fresh workspace re-entry) for the same push.
    # 0 disables in-loop nudging (the orchestrator gap-redrive remains the backstop).
    agentic_convergence_nudges: int = 2
    # P2 — strategist stage: once a change has failed this many code_change attempts, each
    # further fix round first gets a one-shot STRATEGIST recommendation (one structural change
    # of approach) attached to its feedback — the counter to whack-a-mole rounds where each
    # patch mints the next failure. 0 disables. Advisory + fail-open.
    agentic_strategist_after_attempts: int = 3
    # P2 — harness-truth exploration nudge: in the CODE phase, if this many consecutive
    # iterations pass without a single file edit, inject a deterministic harness-record
    # reminder (iterations/edits counts the model cannot fabricate) telling it to start
    # implementing or state its blocker. Re-arms every N iterations. 0 disables.
    # Analysis/review loops are read-only by design and are never nudged.
    agentic_exploration_nudge_every: int = 25
    # Streaming guard (P1): max seconds to wait between chunks on a streaming LLM call
    # before the stream is declared stalled and the call fails loudly — a hung stream
    # otherwise blocks its caller forever with no diagnostic. 0 disables the guard.
    llm_stream_idle_timeout_s: int = 300
    # On a code-change cap, re-enter the SAME workspace and keep going (continue from
    # where it left off) up to this many continuations before stopping — lets large
    # multi-file features finish unattended. Each continuation emits a ⚠ warning.
    # ACCURACY-FIRST: raised so a large multi-file change isn't cut off before it's complete
    # (the plan-gap check still stops it the moment the change IS complete).
    agentic_max_code_continuations: int = 16
    # S7 — XSD→Java link confidence floor (§7.3). Links below this are routed to
    # the "needs confirmation" list and NEVER presented as definite.
    xsd_link_min_confidence: float = 0.55
    # S10 — verification (§9). Comma-separated Maven modules whose compile errors
    # are SOFT-failed (legacy Java-8 IGW) — but ONLY when the module is untouched
    # and out-of-scope; a change that edits it is gated normally (§9.2).
    agentic_soft_fail_modules: str = "IGW"
    # S11 — Anthropic review (§10). Blocking findings loop back to code-change at
    # most this many rounds, then a human adjudicates. ACCURACY-FIRST: the loop EXITS the moment
    # the review is clean (no blocking findings), so this only binds a run that keeps needing
    # fixes — raised so review quality, not a turn cap, decides when a change is done.
    agentic_max_review_rounds: int = 6
    # Dedicated reviewer model (must be an Anthropic/claude id — run_review asserts it).
    # The adversarial reviewer otherwise runs on the SAME model as the code author
    # (llm_router routes both to Purpose.REASONING), so the model grades its own output —
    # a structural self-review weakness. Set e.g. "claude-opus-4-8" to make the reviewer
    # a different pair of eyes. Blank = current behaviour (author's model).
    # A NON-Claude id (e.g. "gpt-5.4") is also accepted — setting it IS the opt-in — but only
    # works via AiNxt anthropic-compat, whose translation layer carries the tool loop for
    # OpenAI upstreams (docs/ainxt_messages_compat.md). The id must start with gpt/o1/o3/o4:
    # AiNxt silently routes unrecognized ids to Claude (B5), so run_review rejects those loudly.
    # A18 (architecture review Important #4 + SDLC review gap #6, "Deterministic
    # gates in shadow-only mode" / self-grading blind spot) — previously blank,
    # meaning the adversarial reviewer ran on the SAME model as the code author
    # (both routed to Purpose.REASONING), so the model graded its own output.
    # Defaulting to a distinct model closes the self-review weakness the review
    # calls out. Operators without a second frontier model provisioned should
    # set this back to "" explicitly (a conscious opt-out, not a silent gap) —
    # see docs/ARCHITECTURE_REVIEW_REMEDIATION.md §A18 for the trade-off.
    agentic_reviewer_model: str = "claude-opus-4-8"
    # ── Reviewer mode (grok-style goal-verifier vs the legacy gate loop) ──────────────
    # "goal_verifier" (default): an adversarial skeptic panel returns a grok-shaped verdict
    #   (refuted + findings + blocking-kind), convergence is driven by a path:line gap
    #   fingerprint (stall → stop, not ride the round cap), the deterministic gates become
    #   ADVISORY suspects the verifier confirms/dismisses, and contradiction/unverifiable
    #   route to the human gate. See agentic_goal_verifier.py + goal_verifier_core.py.
    # "legacy" (set AGENTIC_REVIEWER_MODE=legacy to fall back): the pre-existing _phase_review
    #   gate loop (contract/DI/plan-fidelity gates + adversarial reviewer).
    agentic_reviewer_mode: str = "legacy"
    # Skeptic panel size for goal_verifier mode. 3 (grok default) gives a majority vote;
    # skeptic 0 runs first and a high-confidence decisive refute short-circuits the rest, so
    # "3" is not "always 3x cost". Set 1 for the cheapest single-verifier loop.
    agentic_verifier_panel_size: int = 3
    # A blocker-SEVERITY finding (financial/correctness/security/regulatory "must not ship")
    # gets a higher loop-back ceiling than ordinary blocking findings — so a blocker surfaced
    # in the LAST normal round still gets fix rounds instead of freezing open. If a blocker
    # survives even this, the push is HARD-BLOCKED until it's resolved or explicitly overridden.
    agentic_max_blocker_rounds: int = 10
    # S13 — orchestrator (§3). Verification failures loop back to code-change at most this many
    # attempts. ACCURACY-FIRST: stops the instant the build+tests pass; also absorbs review
    # loop-backs (which share this counter), so it's set well above the review ceilings.
    agentic_max_code_attempts: int = 14
    # Governance review stages (EA → InfoSec) between code approval and Build. Off by
    # default: with the flag off every touchpoint is inert (start 409s, the
    # agentic-complete gate is skipped, the UI renders the plain Build CTA).
    governance_reviews_enabled: bool = False
    # "auto" (docker when reachable, else rlimit-subprocess) or "subprocess".
    # Asymmetric by design: declared bundle scripts run on either backend, but
    # gov_bash requires docker and refuses without it — so forcing "subprocess"
    # DISABLES bash rather than degrading its isolation (governance_sandbox.run_shell).
    governance_sandbox_backend: str = "auto"
    governance_sandbox_image: str = "python:3.10-slim"
    # Per-stage fix budget: review #1 → ONE fix round → verify → verification-scoped
    # review #2 → park. 2 leaves room for a build-breaking fix retry; a repeated
    # blocking-finding fingerprint parks earlier regardless (no review-fix ping-pong).
    governance_max_fix_rounds: int = 2
    # Verify build runs ONLINE by default (like a normal `mvn clean install` that
    # resolves from Nexus) — never offline. Set True only if you deliberately want an
    # offline `mvn -o` build against a pre-warmed ~/.m2 (then `-U` is dropped).
    agentic_verify_offline: bool = False
    # Pluggable verification backend (§9 / dependency-decoupling). The build
    # toolchain must NOT be a hard dependency: "auto" runs the LOCAL mvn/javac
    # backend when the toolchain is present, else degrades to "deferred" (the run
    # completes to human approval flagged `unverified` for CI). "local" forces the
    # toolchain, "ci"/"off" always defer. Never crashes a run for a missing tool.
    agentic_verifier: str = "auto"            # auto | local | ci | off
    # Blast-radius verification (§9.1). When a change alters a SCHEMA (.xsd/.xjb →
    # regenerates JAXB accessors) or a CORE-repo source (a shared signature), a
    # CONSUMER in a module the agent never touched can break — and building only the
    # touched modules misses it (it only shows in a full reactor build). With this ON,
    # such a change triggers a FULL build of every in-scope app repo so a broken caller
    # fails the gate instead of slipping to approval. Costs a full app build on
    # schema/core changes; set false only if a repo can't full-build and you accept the
    # weaker touched-modules-only check.
    agentic_verify_consumers_on_api_change: bool = True
    # Maven reactor parallelism for verify builds (`mvn -T <val>`), e.g. "1C" (one
    # thread per core) or "4". Speeds the full-reactor builds the blast-radius gate
    # runs WITHOUT dropping any module (every module still compiles). Blank = serial.
    # Set blank if a non-thread-safe plugin ever makes a parallel build flaky.
    agentic_verify_threads: str = "1C"
    # Run verify installs as `clean install` (from-scratch, matches the deployment
    # build) rather than incremental `install`. ON by default so a retry round can't
    # reuse stale target/ classes and mask a removed/renamed symbol. Set false to
    # trade that safety for faster incremental verify rounds.
    agentic_verify_clean: bool = True
    # Modules to EXCLUDE from the verification verdict — comma-separated wildcard
    # patterns (`*` = any chars, matched case-insensitively against the Maven module
    # name and the failing line). A build failure attributable to a matching module —
    # a compile error, a missing reactor module, or an unresolved dependency on it — is
    # recorded as 'skipped' and never counts toward the gate. For legacy/unmaintained or
    # absent modules the operator has deemed out of scope (e.g. `*igw*,*hsm-proxy*`).
    # Unconditional (unlike agentic_soft_fail_modules, which re-gates a touched module).
    agentic_verify_skip_modules: str = ""
    # Reuse-first "approach decision" gate (THE BOOK v3.4): before creating any API/XSD
    # the agent maps the existing flows and STOPS to present the human reuse-vs-new
    # options + a recommendation. ON by default; set false to skip the propose pass and
    # go straight through (legacy behaviour).
    agentic_approach_gate: bool = True
    # Parallel review (default OFF): overlap the correctness/completeness reviewer and the
    # independent plan-fidelity gate (both read-only LLM calls). Latency only — the merge is
    # deterministic so the verdict is byte-identical to the sequential default, which the
    # accuracy-first path keeps. Turn on only to shave review wall-clock.
    agentic_parallel_review: bool = False
    # R1′ loop convergence: an LLM-only behavioural-completeness opinion (plan-fidelity `has_gap` with
    # no real reviewer blocker) must not spin the code↔review loop. When True, such gaps drive at most
    # `agentic_max_behavioral_rounds` review rounds, then become ADVISORY and the run proceeds to the
    # human approval gate — the gap is still flagged via `has_blocker`, so it is escalated, never silently
    # shipped. Deterministic gaps (missing files / stubs via `_plan_gap_feedback`) and real reviewer
    # blockers are unaffected. Set False for the legacy "loop on any behavioural gap" behaviour.
    agentic_behavioral_gap_advisory: bool = True
    agentic_max_behavioral_rounds: int = 2
    # R5 converge-or-escalate: if the OPEN must-block set does not strictly shrink over this many review
    # rounds, stop looping and route to the human gate (blockers stay flagged via has_blocker) instead of
    # burning rounds re-attempting a fix the agent can't land. 0 disables (rely on the absolute caps only).
    agentic_max_stall_rounds: int = 2
    # Feed the behavioural plan-fidelity judge the REAL git diff (changed hunks) rather than the per-file
    # content-head summary, which truncates each file to its first ~3k chars. That truncation hid edits made
    # deep inside a large file (e.g. a setter added mid-class), so the judge flagged a PHANTOM "missing
    # behaviour" gap and escalated CORRECT code to the human gate. Fail-open: falls back to the content
    # summary when the workspace diff can't be rendered (e.g. a resumed run whose clone was GC'd). Set False
    # to restore the legacy content-head summary.
    agentic_fidelity_real_diff: bool = True
    # Tier-2 of the same fix: deterministically corroborate the behavioural judge against the diff. (1) GROUND
    # — feed the judge the set of call-symbols it can SEE were added on '+' lines, instructing it not to call
    # them "missing" (it may still flag them faked / used wrongly). (2) GATE — downgrade a 'missing_behavior'
    # BLOCKER to advisory only when it names a verifiably-added symbol, the claim is pure ABSENCE, it is NOT
    # leg/partial-specific, and NOT a security/financial/regulatory finding. Downgrade-only (never deletes,
    # never escalates, stays visible in the review snapshot), logged, fail-open. False disables both layers.
    agentic_fidelity_corroborate: bool = True
    # Plan-fidelity's BEHAVIOURAL check is a non-deterministic LLM completeness opinion. When True it is
    # ADVISORY ONLY: its gaps still drive bounded loop-back (the agent tries to finish) and are surfaced to
    # the human at the push-approval gate, but they NEVER become must-block items — so an LLM hallucination
    # ("X is missing" when X is present) can no longer BLOCK THE PUSH or trip the "push is blocked" gate. The
    # DETERMINISTIC file-coverage miss and the REAL adversarial-reviewer blockers (correctness/security with a
    # concrete location) still block as before. Aligns with §8 (deterministic drives the loop; LLM judgment is
    # advisory). Default True. Set False to let plan-fidelity behavioural gaps block the push (legacy).
    agentic_plan_fidelity_advisory: bool = True
    # R4 — deterministic acceptance predicates (the robust tier-1 completeness check). SHADOW phase: when
    # True, extract predicates from the ratified plan, verify them against the real diff with the pure
    # checker (acceptance_predicates.py), and emit an `acceptance_predicates` event comparing them to the
    # LLM verdict — BLOCKS NOTHING yet. Lets us prove zero false-unmet on live runs before the predicates
    # become a real blocker + the code agent's pre-submit definition-of-done. Fail-open. Adds one LLM
    # extraction call per run (cached). Set False to disable the shadow entirely.
    agentic_acceptance_predicates: bool = True
    # R4 ENFORCE — flip the predicates from measure-only to a real tier-1 BLOCKER: an UNMET predicate (a
    # plan deliverable the diff verifiably lacks) loops the run back with precise feedback and blocks the
    # push until satisfied. This is the DETERMINISTIC completeness gate that replaces the LLM blocker §8.5c
    # demoted to advisory. Requires `agentic_acceptance_predicates` on. Fail-open + the LLM-advisory and
    # human push-gate remain as backstops.
    # A18 (architecture review Important #4, SDLC gap 6) — ENFORCING as of this
    # remediation. The shadow rollout (agentic_acceptance_predicates=True,
    # measuring since its introduction) found no false-unmet signal against
    # the standard the flag's own comment set as the bar, so per
    # ARCHITECTURE_REVIEW_ACTIONS.md A18 ("flip the three shadow gates to
    # enforcing... set agentic_reviewer_model away from the author's model")
    # this is now a real blocker. Fail-open + the LLM-advisory review and the
    # human push-gate remain as backstops if this gate itself errors.
    agentic_acceptance_predicates_enforce: bool = True
    # Document↔CODE consistency gate (post-codegen). After the change-set freezes, audit the generated
    # TSD's CONCRETE implementation claims (DB columns, config keys, error codes, named methods) against
    # the ACTUAL diff; a claim the code doesn't support means the DOC is wrong → reconcile the TSD to the
    # code and re-persist (non-blocking). A `code_missing` finding (code lacks a plan+TSD-required
    # behaviour) routes into the review/blocking path. Fail-open. Set False to disable.
    agentic_doc_code_gate: bool = True
    # Deterministic CONTRACT gate (post-codegen, LLM-free). Cross-references the generated diff against
    # itself + the ratified plan for two runtime-bug classes the LLM gates structurally miss: (a) a hash/map
    # field READ via .get("X") that nothing WRITES (silent null/default → wrong branch), and (b) a switch-side
    # error code the plan declares but the code never EMITS as a literal. Both shipped by the $371 cbabbf9c
    # run after six clean review rounds. Shadow by default (measure + surface); flip _enforce to block +
    # loop the run back with precise feedback. Fail-open. See app/agents/contract_gate.py.
    agentic_contract_gate: bool = True
    # ENFORCE — A18 (architecture review Important #4) recommends flipping every
    # shadow gate to enforcing. This ONE remains OFF deliberately: the gate has
    # a KNOWN false-positive class (a read of a field written by code OUTSIDE
    # the diff — i.e. by unchanged, pre-existing code — currently looks
    # identical to "nothing writes this field" and would wrongly block a
    # correct change). Flipping this before the field-consistency
    # writer-scope is widened past the diff would turn a review finding into
    # a self-inflicted outage (legitimate changes blocked on a false
    # positive), which is a worse outcome than leaving the gap open a cycle
    # longer. Tracked as a scoped follow-up in
    # docs/ARCHITECTURE_REVIEW_REMEDIATION.md §A18 — widen contract_gate.py's
    # writer-scope search to the whole file (not just the diff hunk) FIRST,
    # shadow-validate on live runs, THEN flip this to True. Do not flip this
    # flag as part of a routine config change without that fix landing first.
    agentic_contract_gate_enforce: bool = False
    # Finding #4 follow-up (ADR-0004's tracked prerequisite) — widens
    # check_field_consistency's WRITE-scope from "modified files only" to
    # "the full repo-wide Java corpus" (the same corpus already built for
    # check_error_code_emission's corpus_text param, just also threaded into
    # extra_write_text). This is the exact fix ADR-0004 and the
    # agentic_contract_gate_enforce comment both name as the prerequisite
    # for eventually flipping enforcement: a field read by the change but
    # WRITTEN by unchanged code ANYWHERE in the repo (not just in a modified
    # file) is no longer a false positive. Kept as its OWN flag — separate
    # from agentic_contract_gate_enforce — so it can be measured in shadow
    # mode independently: turn this ON to see whether it reduces the
    # false-unmet count, turn it OFF to fall back to the original
    # modified-files-only scope, all while agentic_contract_gate_enforce
    # stays False and nothing blocks a run either way. True by default
    # because widening can only REMOVE false positives (a field write
    # anywhere in the corpus was always a legitimate write; the old code
    # simply couldn't see it) — it cannot introduce a new false negative,
    # so there is no safety reason to default it off.
    agentic_contract_gate_widen_writer_scope: bool = True
    # Static DI-WIRING gate (post-codegen, LLM-free) — Phase 1 of the context-load gate. The verify
    # step stops at `mvn install -DskipTests` and never refreshes a Spring context, so boot-time
    # wiring failures (missing/ambiguous bean, unbound @Value, component outside the scan path,
    # constructor-injection cycle) pass every gate and crash at deployment. This checks the wiring
    # statically, DELTA-SCOPED to the classes the change touched (legacy wiring noise is not the
    # agent's to fix). Shadow by default; flip _enforce to block + loop back with precise feedback.
    # Fail-open. See app/agents/di_wiring_gate.py. Phase 2 (scoped @SpringBootTest slice boot via the
    # vestigial "smoke" VerificationStep) needs live-run validation before it can be built.
    agentic_di_gate: bool = True
    # A18 (architecture review Important #4, SDLC gap 6) — ENFORCING as of this
    # remediation, per ARCHITECTURE_REVIEW_ACTIONS.md A18. Unlike the contract
    # gate above, this gate's false-positive risk was already bounded at
    # design time (DELTA-SCOPED to changed classes only, so legacy wiring
    # noise cannot trip it) — the shadow rollout comment's own "Fail-open"
    # guarantee plus that scoping is why this one is safe to flip today while
    # the contract gate is not.
    agentic_di_gate_enforce: bool = True
    # ADR-0005 / SDLC review gap 4 ("TSD treated as prose document, not binding
    # contract"). When True, CODE_CHANGE phase entry (kind='code' runs, and the
    # full-run pipeline's XSD→CODE_CHANGE transition) checks whether the
    # change's latest TechSpec has status=APPROVED and emits a
    # `tsd_approval_gate` telemetry event either way — MEASURE ONLY, matching
    # this repo's shadow-gate rollout pattern (agentic_contract_gate,
    # agentic_di_gate). Fail-open: any error evaluating the gate itself does
    # not block the run.
    agentic_tsd_approval_gate: bool = True
    # Enforce = park the run (AWAITING_TSD_APPROVAL) instead of merely logging a
    # shadow telemetry event. Requires agentic_tsd_approval_gate=True. Kept as
    # its own flag (same pattern as agentic_contract_gate/_enforce) so the gate
    # can be measured in shadow mode — how many in-flight runs WOULD have been
    # blocked — before it can stop anything.
    agentic_tsd_approval_gate_enforce: bool = False
    # Backward-compatible on-ramp: because no explicit "approve the TSD" UI
    # action exists yet anywhere in the product, flipping the gate on with NO
    # existing APPROVED rows would park every single Phase-B run on day one.
    # When True, TSD generation (agents.py `ws_tech_spec`) auto-stamps a freshly
    # (re)generated TSD as APPROVED the moment it's written — this preserves
    # today's de-facto behaviour (a generated TSD IS what code gets built
    # against) while making the state EXPLICIT and version-lockable. An
    # operator who wants a REAL human sign-off step should build a TSD
    # approve/reject UI (mirroring the existing BRD approval flow in
    # `submit_brd`/`respond_approval`) and turn this off once that ships.
    agentic_tsd_auto_approve_on_generate: bool = True
    # SDLC review gaps 7/8/9/11 — mandatory cross-module analysis gate
    # (app/agents/cross_module_gate.py). Deterministic, LLM-free: flags a
    # changed Java method DEFINITION that was never checked with
    # callers()/impact_analysis()/symbol_graph() this run. Shadow by default
    # (measure + surface via the `cross_module_gate` event); flip _enforce to
    # block + loop the run back with a precise remediation directive.
    # Fail-open. Same rollout discipline as agentic_contract_gate/agentic_di_gate.
    agentic_cross_module_gate: bool = True
    agentic_cross_module_gate_enforce: bool = False
    # Skip method names shorter than this (getters/setters/common short names
    # like "get"/"run" would otherwise dominate the finding list with
    # near-certain false positives). 4 chars is deliberately conservative —
    # tune down only after observing the shadow finding quality on live runs.
    agentic_cross_module_gate_min_name_len: int = 4
    # SDLC review gap 10 — TSD-derived test coverage gate
    # (app/agents/tsd_test_generator.py). Extracts checkable assertions from
    # the approved TSD and measures how many are referenced by a `tsd-ref:`
    # marker in this change's test files. Shadow by default: the code-gen
    # prompt is not yet taught to emit tsd-ref markers, so enforcing today
    # would block on a convention nothing produces yet — this is the
    # measurement half of the gap; enforcement is a deliberate follow-up once
    # code generation is updated to cite TSD sections in its tests.
    agentic_tsd_test_coverage_gate: bool = True
    agentic_tsd_test_coverage_gate_enforce: bool = False
    # Minimum fraction of extracted TSD assertions that must be covered by a
    # `tsd-ref` marker for a behavioural change to pass (only meaningful once
    # _enforce is True). 0.5 is a deliberately soft floor for the first
    # enforcement rollout — raise it after observing shadow coverage on live runs.
    agentic_tsd_test_coverage_min_ratio: float = 0.5
    # Plan enforcement audit (pre-ratification). After the Change-Analysis plan is proposed, read-grounded
    # audit of each "validate/reject/persist/enforce" claim: is the named enforcement point actually wired
    # in the repo evidence? Conservative — a sound plan is left byte-identical; only an evidence-backed gap
    # is appended to technical_analysis["enforcement_audit"] + surfaced to the human ratifier. Fail-open.
    agentic_plan_enforcement_audit: bool = True
    # Require real feature tests. OFF: the team lands tests separately, and forcing the code agent to write
    # tests burned budget/time without a proven quality win (WS3a experiment, reverted). When off, the code
    # agent does not author tests and this verification gate is skipped. Leave off unless an A/B shows it helps.
    agentic_require_feature_tests: bool = False
    # Deterministic critical-decision gate on ratify: a plan whose money-movement /
    # atomicity / ordering decisions are missing or unsourced cannot be ratified
    # (see api/agentic.py). Declared here because the gate reads it off settings and
    # `extra="ignore"` meant AGENTIC_REQUIRE_CRITICAL_DECISIONS was silently dropped —
    # the gate was permanently ON with no way to turn it off for a bisect. Default
    # True preserves exactly that behaviour; it is now actually switchable.
    agentic_require_critical_decisions: bool = True
    # 3c — RUN the change's OWN test classes (scoped by -Dtest) as a real execution gate, so a green
    # verify means the behaviour RAN, not merely compiled. OFF by default (validate on real runs first):
    # a test FAILURE loops the run back to code_change; an infra/toolchain problem stays advisory via the
    # environment gate (unverified, no loop). No-op when the change owns no test files. Distinct from
    # agentic_require_feature_tests (a test must EXIST) and agentic_write_unit_tests (authoring — stays off).
    agentic_run_feature_tests: bool = False
    # When the feature-test step fails ONLY because pre-existing (legacy) test sources
    # don't compile — files this change never touched — skip the step visibly instead of
    # looping the code agent on errors it must not fix. Genuine failures of the change's
    # own tests still gate normally. OFF restores the old behaviour (any test-step failure
    # → required_tests=false → loop).
    agentic_legacy_test_compile_failopen: bool = True
    # Rolling prompt-cache on the conversation tail (Lever 1). Caches the GROWING
    # message history (prior file reads / tool results) so each turn reprocesses only
    # the newest turn instead of the whole transcript — the fix for "reads get slower
    # late in a run". Accuracy-NEUTRAL: a cache hit replays byte-identical tokens, it
    # never summarises or drops context; a non-matching prefix simply misses and
    # recomputes. Toggle for A/B: flip OFF to compare outputs with caching disabled.
    agentic_cache_message_tail: bool = True
    # Shallow-clone depth for the agentic workspace (Lever 3). Fetch only the last N
    # commits of the target branch instead of the full multi-GB history — the deep
    # history is what makes the clone slow, and nothing in the pipeline needs it
    # (the XSD base-diff reads `git show <base_sha>:path`, and base = the clone tip).
    # The margin (50, not 1) keeps RECENT history so the agent's `git log`/`blame`
    # still work — accuracy stays intact. Safe default, no env var needed; set to 0
    # for a full-history clone if a deep-history workflow ever needs it.
    agentic_clone_depth: int = 50
    # Interleaved extended thinking (Anthropic) for the code/xsd agents — the
    # "reasoning" that lets the agent plan tool use + diagnose failures. 0 disables;
    # min 1024 when enabled. Must stay below agentic_max_output_tokens.
    agentic_thinking_budget_tokens: int = 4000
    # Java LSP (Eclipse jdtls) diagnostics tool (§8 structural tier). OFF by default —
    # jdtls is a 0.5–1 GB+ JVM server, fine on a high-RAM prod/dev host but it would
    # OOM a small box. When ON, the `lsp_diagnostics` tool is offered to the code agent
    # (advisory, never forced). Image ships jdtls at `agentic_lsp_home`.
    agentic_lsp_enabled: bool = False
    agentic_lsp_home: str = "/opt/jdtls"
    agentic_lsp_heap_mb: int = 2048
    agentic_lsp_timeout_s: int = 90

    # Tool-policy gate (THE BOOK §8): the code agent must gather structural blast-radius
    # intel (impact_analysis / callers / symbol_graph / ast_query / lsp_diagnostics) at
    # least once before MODIFYING a .java file — so a shared-symbol change is made WITH
    # its consumers known, not blind. grep alone does NOT satisfy it (the point is to push
    # past lexical search to the call graph). ON by default: this is the intended flow;
    # the agent self-corrects on the rejection (gathers intel, retries) rather than editing
    # blind, and the loop runs on a reliable-tool-call provider (llm_provider=claude /
    # ainxt anthropic-compat — call_claude_tools refuses the finish-reason-blind path).
    # Set False to restore the legacy blind-edit flow.
    agentic_require_intel_before_java_edit: bool = True

    # ── Accuracy ──────────────────────────────────────────────────────────────────────
    # The code-intelligence BACKBONE is already on (symbol_graph/impact_analyzer/intel gate);
    # this adds a PROMPT clause steering the code agent to PREFER callers/impact_analysis over
    # grep for "who uses X"/blast-radius (grep stays fine for exact-string discovery). ON: the
    # call-graph index is verified-populated for the code repos (~12.5K call edges on the app
    # repo), so it steers to WORKING tools; cuts wasted grep round-trips + catches consumers grep
    # misses. Complements the always-on _CONTRACT_DISCIPLINE clause (shared-state producer/consumer
    # + emit-declared-codes) which covers the string-keyed / new-file bugs the call-graph can't see.
    agentic_prefer_code_intelligence_prompt: bool = True
    # Pre-commitment: the code agent records the build/test outcome it EXPECTS before verify
    # runs; the runtime compares actual vs expected and surfaces a mismatch as a signal back
    # into the next code_change round. ADVISORY only — never overrides the deterministic
    # build verdict (so it cannot pass broken code).
    agentic_record_verify_expectation: bool = False
    # Durable progress ledger: persist files-already-read + verify-failure history at the
    # CODE_CHANGE boundary so a resumed run / completion-round doesn't re-derive them.
    # ADVISORY + content-hash-keyed (a changed/edited file is never suppressed from re-read).
    agentic_progress_ledger: bool = False

    # Slice 27 — Per-agent LLM routing. Maps a `Purpose` (defined in
    # `app.core.llm_router`) to a specific model id, overriding the
    # global default returned by `get_model()`. Frontier models for
    # reasoning-heavy purposes (BRD/TSD/code-change/deep-research),
    # lighter cheaper-faster models for routing/utility purposes
    # (taxonomy classifier, query enrichment, doc-code link confidence,
    # code summarizer, context compressor, ambiguity detector).
    #
    # Empty string for any value disables routing for that purpose —
    # falls back to the default `get_model()` so flag-off equals
    # current behaviour. Value is the same model-id format the
    # provider's SDK accepts (e.g. "claude-haiku-4-5-20251001",
    # "gpt-4o-mini"). The router does not validate against a known
    # list — wrong model ids surface as provider errors at call time.
    use_llm_routing: bool = False
    # Phase 7.1 — provide sane defaults so flipping `use_llm_routing=True`
    # produces routing immediately, without forcing the operator to also
    # populate three env vars. The model IDs match the team's current
    # generation-default Anthropic models. Operators can override per-
    # purpose via env vars; setting one to "" disables routing for that
    # bucket (callers fall through to `get_model()`).
    routing_model_routing: str = "claude-haiku-4-5-20251001"    # Purpose.ROUTING
    routing_model_utility: str = "claude-haiku-4-5-20251001"    # Purpose.UTILITY
    routing_model_reasoning: str = ""                            # Purpose.REASONING — empty = use global default (frontier model)

    # Per-agent model overrides — win over Slice 27a's `routing_model_*`
    # for that one agent. Populated lazily as needed; default empty so
    # the existing routing chain continues to apply.
    #
    # Read by `app.rag.code_summarizer._code_summarizer_model_override()`.
    # Set this to (for example) "gpt-5.2-mini" to A/B a different model
    # for the per-symbol NL summary phase without affecting other utility-
    # purpose agents (compressor, validators, ambiguity_detector, etc.).
    code_summarizer_model: str = ""

    # Adversarial-reviewer model override. The reviewer SHOULD NOT be the same model as the
    # code author (code_change) — a model has a documented "self-correction blind spot" (it
    # misses in its own output the same defects it catches in others'). Set this to a
    # different (equal-or-stronger) model id the provider serves, e.g. "claude-opus-4-8"
    # while code_change runs on Sonnet. Empty = inherit the normal routing (same model as
    # today — inert), so flag-off behaviour is unchanged. Read in adversarial_reviewer.py.
    adversarial_reviewer_model: str = ""

    # Phase A Excellence — Slice 1: critic LLM defaults.
    # Per-checkpoint overrides live in `app_configs` under the
    # `eval_critic.<checkpoint_id>.*` keys (provider, model, enabled).
    # These globals provide the env-level defaults when no per-checkpoint
    # override is set. `eval_critic_default_provider` empty means
    # "auto-pick a provider different from llm_provider so the generator
    # doesn't grade its own output" — controlled by `eval_critic_cross_provider`.
    eval_critic_enabled_by_default: bool = True
    eval_critic_default_provider: str = ""
    eval_critic_default_model: str = ""
    eval_critic_cross_provider: bool = True

    # Phase A Excellence — critic grounding on the product / code knowledge base.
    # When on, the critic retrieves the most relevant indexed product-knowledge
    # and code-knowledge chunks (via the team's `app.rag.retrieval.retrieve`)
    # and injects them as authoritative context so the LLM-as-judge evaluates
    # the artifact against our actual product reality — not just internal
    # consistency. The seam is FAIL-OPEN: if nothing is indexed (local laptop)
    # or retrieval errors, it returns no grounding and the critic proceeds
    # rubric-only, identical to the ungrounded path. In UAT, where the product
    # and code indexes are populated, the same code lights up automatically.
    eval_grounding_enabled: bool = True
    eval_grounding_top_k: int = 4

    # Slice 28 — Observability traces. When on, every `call_llm` / `stream_llm`
    # invocation emits a structured JSON log line with `agent_name`,
    # `purpose`, `model`, `provider`, latency, prompt/response sizes,
    # success/failure. Operators forward those to Langfuse / OTEL /
    # Datadog via standard log shipping (no hard SDK dep this slice —
    # follow-up 28a wires Langfuse directly when a backend is chosen).
    # Default ON: one JSON row per LLM call (agent, model, latency, tokens,
    # stop_reason) is the cheapest "is the request going to the LLM, when, how
    # long, how big" signal we have — and the user needs it to diagnose. Rows
    # land in the dedicated, rotating `llm_calls.jsonl` (see below); Langfuse
    # forwarding stays separately gated by `use_langfuse`, so this flips on no
    # external calls. Set USE_OBSERVABILITY_TRACES=false to silence.
    use_observability_traces: bool = True
    # Persist every LLM call's token/cost to the `llm_usage_records` table (best-effort, own
    # session, swallows errors) so the Usage dashboard can roll up per-change / per-phase /
    # per-section spend — including the non-flow agents (BRD/TSD/docgen/eval/Family-A). Set
    # PERSIST_LLM_USAGE=false to disable the per-call DB write if it ever becomes a load concern.
    persist_llm_usage: bool = True

    # Prompt-cache viability probe (temporary diagnostic; ON by default). Every
    # LLM call logs a CACHE_PROBE line with a hash of its system prompt (the
    # cache-prefix candidate) + run_id + agent, so we can tell — from real runs
    # — whether any agent re-sends a byte-identical stable prefix within the
    # 5-min cache TTL. If hashes never repeat inside a run, routing that agent
    # through AiNxt's /ask/cached endpoint cannot help. See
    # docs/MEMORY_AND_TOKEN_EFFICIENCY_PLAN.md B1. Log-only; no behaviour change.
    # Set CACHE_PROBE_LOGGING=false to silence once we've collected the data.
    cache_probe_logging: bool = True

    # Slice 24+ — dedicated rotating JSONL file for LLM call traces.
    # Read here (not via os.getenv in observability.py) so the value set
    # in .env is actually honoured: pydantic parses .env, the raw process
    # env usually doesn't carry it. Blank → land it next to the other
    # diagnostics files (app.core.diag's resolved dir, which is mounted +
    # writable), instead of the old unmounted /tmp path nobody could find.
    # Set LLM_CALL_LOG_PATH to pin an explicit location.
    llm_call_log_path: str = ""

    # Sub-slice 28a — Langfuse SDK forwarding. When True AND `langfuse`
    # package is installed AND host/keys are configured, every emitted
    # `LlmCallTrace` is also pushed as a Langfuse generation. Falls back
    # silently to log-only emission when the SDK or creds are missing.
    use_langfuse: bool = False
    langfuse_host: str = "https://cloud.langfuse.com"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    # ── Phase 0.4 — Langfuse trace sampling ──────────────────────────────────
    # When < 1.0, only that fraction of LLM calls forward to Langfuse.
    # Local INFO logs always emit so a full audit trail remains. Errors
    # bypass sampling so we never miss them. Set to 0.1 in prod for
    # predictable upstream ingestion cost.
    langfuse_sample_rate: float = 1.0

    # ── Phase 0.3 — Pre-call context budget assertion ────────────────────────
    # When True (default), call_llm / stream_llm raise ContextOverflowError
    # BEFORE provider dispatch if (system + messages + max_response) exceeds
    # the model's context window. Set to False to fall back to provider-side
    # truncation behaviour (legacy).
    use_context_budget_check: bool = True

    # ── Phase 1.2 — BM25 backend selection ───────────────────────────────────
    # "tsvector"  : Postgres content_tsv GIN index + ts_rank_cd (Phase 1.2).
    #               Stateless, low-memory. REQUIRES alembic migration 0029
    #               which creates the content_tsv column + GIN index.
    # "rank_bm25" : in-process BM25Okapi index, rebuilt on every ingest via
    #               Celery generation counter (legacy).
    # Default is "rank_bm25" until the content_tsv migration is confirmed
    # applied on every environment. Flip to "tsvector" once eval recall
    # is verified.
    bm25_backend: str = "rank_bm25"

    # ── Phase 1.3 — Query-embedding LRU cache ────────────────────────────────
    # Each entry is one 768-dim float vector + a SHA256 string key.
    # 4096 entries ≈ 12 MB. Set to 0 to disable.
    query_embedding_cache_size: int = 4096

    # ── Phase 1.4 — Per-window storage for oversized chunks ──────────────────
    # When True, symbols whose body exceeds MAX_EMBED_CHARS produce one
    # document_chunks row PER sliding window (sharing parent_symbol_id)
    # instead of a single mean-pooled vector. Higher recall on long methods
    # at ~1.5–2× storage for affected symbols only.
    use_per_window_chunk_storage: bool = False

    # ── Phase 1.5 — Graph traversal backend ──────────────────────────────────
    # "sql"  (default): pure-SQL recursive queries against the JSON edge columns
    #        on document_chunks (populated by the symbol-graph extractor). No
    #        AGE dependency — works with the default ingest, which is why it is
    #        the default now that the symbol graph is on.
    # "age"  : Apache AGE Cypher — requires the AGE graph to be projected
    #        (manual/admin or scheduled ingest); dormant by default.
    graph_backend: str = "sql"

    # ── Phase 2.1 — Parallel multi-pass variants ─────────────────────────────
    # When True, the multi-pass retrieve loop runs each query variant
    # (raw + HyDE + sub-questions) concurrently via ThreadPoolExecutor.
    # Each task allocates its own SessionLocal() so SQLAlchemy 2.0 thread
    # safety holds. Wall-time for N variants drops from N*T to ~T.
    # Set False to fall back to the sequential loop (legacy behaviour).
    use_parallel_multi_pass: bool = True
    # Cap concurrent variant workers — protects the DB connection pool.
    # 4 covers (raw + HyDE + up to 3 sub-questions = 5 variants worst-case)
    # with one slot of headroom; nothing actually waits with the default
    # MAX_SUB_QUESTIONS=3 → max 4 variants → 4 workers handle them all.
    multi_pass_max_workers: int = 4

    # ── Phase 2.2 — Enriched-query cache ─────────────────────────────────────
    # Process-local LRU keyed on SHA256(query) → EnrichedQuery.
    # First call to enrich_sync() issues the LLM call; subsequent calls
    # for the same query reuse the cached structure. 1024 entries is well
    # under 1 MB and covers typical multi-turn agent flows. Set to 0 to
    # disable caching entirely.
    query_understanding_cache_size: int = 1024

    # ── Phase 2.3 — Context compression mode ─────────────────────────────────
    # "off"     — pass-through; equivalent to use_context_compression=False.
    # "overlap" — deterministic line-overlap scorer. No LLM call. Default
    #             for new installs.
    # "llm"     — original Slice 8 path (per-chunk LLM filter). Kept for
    #             A/B comparison and instant rollback.
    # The legacy `use_context_compression` flag still gates whether
    # compression runs at all; this knob only chooses the algorithm.
    context_compression_mode: str = "overlap"
    # Max kept lines per chunk in "overlap" mode. Chunks at or below this
    # line count pass through unchanged (no compression). Generous default
    # so most function-sized chunks remain whole.
    context_compression_max_lines: int = 25

    # ── Phase 2.4 — Reranker score cache + early-exit ────────────────────────
    # Process-local LRU keyed on (query, ordered chunk-id tuple) →
    # ranked candidate dicts. Backend-agnostic: works the same for
    # local CrossEncoder and remote reranker services.
    use_reranker_score_cache: bool = True
    reranker_score_cache_size: int = 512
    # Skip the cross-encoder entirely when the candidate list is at or
    # below this length. With min=2 the function returns the input order
    # unchanged for 1- and 2-candidate inputs (ranking 2 things is noise).
    reranker_min_candidates: int = 2

    # ── Phase 2.5 — MMR diversification ──────────────────────────────────────
    # When True, retrieval over-samples 2× and the final top-K is picked
    # by Maximal Marginal Relevance instead of pure score order. Default
    # OFF until the eval harness has been run with it on.
    use_mmr_diversification: bool = False
    # MMR trade-off between query relevance and chunk-vs-chunk novelty.
    # 1.0 = pure relevance (no diversification); 0.0 = pure novelty;
    # 0.5 is the literature standard.
    mmr_lambda: float = 0.5

    # ── Phase 3.3 — Noise-symbol filter ──────────────────────────────────────
    # When True (default), the tree-sitter chunker drops trivial symbols
    # — one-line getters/setters, simple delegates, empty bodies — so
    # they don't waste an embedding row each. The file-level chunk is
    # always retained so BM25 / grep queries can still locate them by
    # name. 20-40% chunk-count reduction on a typical Java codebase.
    skip_noise_symbols: bool = True

    # ── Phase 3 Gap D — Embed warmup on startup ──────────────────────────────
    # When True (default), a background thread fires one cheap `embed`
    # call at FastAPI startup so Ollama loads the model into VRAM before
    # user-facing requests arrive. Disable if your embedder is shared or
    # cold-start is acceptable (CI/dev).
    embed_warmup_on_startup: bool = True

    # ── Phase 7.3 — Graph 1-hop expansion of top-K ───────────────────────────
    # When True, retrieve() appends 1-hop CALLS/INHERITS/IMPLEMENTS
    # neighbours of the final top-K chunks as `ctx_only=True` rows so the
    # context builder can render them in a secondary section. Default OFF
    # until eval harness validates the iteration-count drop on code-change
    # queries.
    use_graph_one_hop_expansion: bool = False

    # ── Phase 8.2 — Text-search configuration for tsvector queries ───────────
    # "english" (default) — stems + drops English stopwords. Good for prose,
    #   lossy for code (collapses `tokens`/`tokenize`, drops `is`/`as`/`in`).
    # "simple_code" — Phase 8.2 — copy of pg_catalog.simple, no stemming
    #   and no stoplist. Better for code. Requires migration 0054 to have
    #   created the config on the target DB.
    # Hot-switchable: Phase 1.2's GIN index serves any per-query config.
    bm25_text_search_config: str = "english"

    # STEP 9 docgen-merge — PM-vocab → domain-vocab query rewriter (teammate's
    # `app/rag/query_rewriter.py`). Conceptually overlaps with Slice 5
    # `query_understanding.py` (HyDE + sub-questions). Run only ONE at a time
    # — running both back-to-back dilutes the RRF fusion. Default OFF; flip
    # for an A/B run against the retrieval golden set.
    use_query_rewriter: bool = False

    # ── Excel Testcase Engine (cert_test_cases LangGraph pipeline) ──────────
    # WHY this flag: the host's `cert_test_cases` doc-type was a markdown-only
    # stub. The engine at `app.excel_testcase_engine` replaces that with a
    # multi-stage pipeline (enhance → plan → write → render → validate →
    # repair) and emits a domain-format Excel workbook plus markdown + docx
    # companions. Default ON — the legacy markdown path is kept in code for
    # rollback only and is *not* used in normal operation. To revert to the
    # legacy path, set excel_engine_enabled = false in env or DB config.
    excel_engine_enabled: bool = True

    # ── BRD/TSD-only cert engine ────────────────────────────────────────────
    # The excel test-case engine now trusts the BRD and TSD as the only
    # sources of truth (no canonical domain spec bundle, no XSD diff, no PM
    # scope-signal enforcement, no adaptive cap). The `use_adaptive_test_case_cap`
    # and `use_scope_ownership_enforcement` flags are gone — flipping them
    # had no effect on the engine's behaviour after the BRD/TSD-only refactor.

    # `capture_scope_signals` — when True, the agentic Change-Analysis clarification
    # gate appends the PM scope-signal questions (in-scope parties/operations, risk
    # profile, compliance sensitivity) to the batch the PM answers in AnalysisPanel.
    #
    # DEFAULT FLIPPED TO FALSE. The questions were only ever a capture mechanism —
    # their consumer, `clarification_loader.get_scope_signals`, has NO production
    # caller (tests only), and the BRD/TSD-only refactor above deleted the
    # enforcement they fed. So the PM answered a pack-sized batch of closed-set
    # questions (1 parties multi-select + one yes/no per pack operation + risk +
    # compliance) whose typed answers nothing read. Only the LLM's own
    # clarifications are asked now. Set True to restore capture for a domain that
    # wires up a real consumer.
    capture_scope_signals: bool = False

    # v3 — when True, `context_cache._build` runs the LLM-based
    # `party_inference` agent and `question_generator` emits a single
    # multi-select "parties involved" question with the inferred set
    # pre-checked. When False, the builder falls back to pre-checking all
    # four canonical parties (same widget, same wire format — just no
    # LLM-side inference). Loader recovery handles both shapes and also
    # the legacy 4×yes_no answers already in the ledger. Flip False if
    # party_inference regresses; nothing downstream breaks.
    agentic_clarification_infer_parties: bool = True

    # When True, the cert orchestrator bypasses cert-agent's /api/llm-agent/run
    # and synthesises an all-PASS run result for the matched test cases. Use
    # this when Anthropic credits are exhausted or when iterating on the
    # back-channel / signoff / UI without burning tokens. Default OFF.
    mock_cert_run: bool = False

    # Base URL for the cert-agent HTTP API (cert_push + cert_orchestrator).
    # Defaults to the docker-network hostname used by `certagent/docker-compose.yml`,
    # which works inside the docker stack when `atom_backend` is attached to the
    # `cert-net` external network. Override via CERT_AGENT_URL env when:
    #   - cert-agent runs on the host (Ubuntu native): http://host.docker.internal:8000
    #     (compose already maps host-gateway → host on Linux via extra_hosts)
    #   - cert-agent runs on a remote box: http://<ip-or-fqdn>:8000
    # NO trailing slash — callers concatenate paths.
    cert_agent_url: str = "http://cert-agent:8000"
    # Shared token authenticating backend → cert-agent calls. SCR findings
    # #3/#9 — this used to default to a literal, published-in-source value
    # ("dev-internal-token"), which is exactly the "hardcoded password" shape a
    # secrets scanner flags regardless of whether a production guard exists.
    # It now follows the same pattern as every other secret field in this
    # file (anthropic_api_key, jwt_signing_secret, etc.): defaults to empty,
    # and callers must set CERT_AGENT_INTERNAL_TOKEN explicitly. Startup
    # validation (`validate_secrets_not_placeholder` /
    # `_check_internal_token_not_default` below) still enforces this is set
    # to a non-legacy value before production boots; dev/UAT now emit a
    # startup warning instead of silently authenticating on a public string.
    cert_agent_internal_token: str = ""

    # Bank-agent URL — required by Slice 4's two-phase orchestrator to
    # fire the BANK-initiated batch from the bank side. Same docker
    # network as cert-agent; override via BANK_AGENT_URL when bank-agent
    # runs on host/remote.
    bank_agent_url: str = "http://bank-agent:8003"

    # ── Integration-testing tunnel (ITA I-0) ────────────────────────────────
    # Carries an encapsulated HTTP exchange between the two platforms so a
    # Simulator on one side can drive an External API on the other. OFF BY
    # DEFAULT and dev-only: the ingress is an H3 (externally reachable,
    # hostile) interface, and an HTTP tunnel between security domains is
    # SSRF-as-a-service unless the target is constrained.
    integration_testing_enabled: bool = False
    # The alias allowlist, as a JSON object keyed by alias. THE caller never
    # sends a URL — it sends an alias this side resolves here, so this policy
    # is the tunnel's command allowlist. Validated at STARTUP (below): a
    # malformed policy stops the app rather than starting it permissive.
    #   {"external_api": {"scheme": "http", "host": "api.internal",
    #                     "port": 8080, "path_prefixes": ["/v1/"],
    #                     "strip_headers": ["cookie"]}}
    integration_testing_allowlist: str = ""
    # Loop guard: a tunnel that forwards into another tunnel amplifies.
    integration_testing_max_hops: int = 1
    # ── The timeout budget (ITA §6). It MUST shrink inward ──────────────────
    # initiator (120s, its own) > ingress > A2A send > egress→target.
    # When every layer shares one timeout the OUTERMOST fires first and the
    # operator gets a generic 504 with no inner detail; shrinking inward means
    # the layer that actually failed is the one that reports. Ordering is
    # asserted at startup (ITA-5) because a deployment that gets it wrong only
    # misbehaves under load.
    integration_testing_ingress_timeout_s: float = 105.0
    integration_testing_a2a_timeout_s: float = 90.0
    # 60s is the confirmed functional-test ceiling. Note it EXCEEDS the A2A
    # transport default of 30s, which is why the send must pass an explicit
    # timeout — see `send_task_to_partner(timeout=)`.
    integration_testing_target_timeout_s: float = 60.0
    # H3 size cap, enforced in the agent and not only at nginx.
    integration_testing_max_body_bytes: int = 5 * 1024 * 1024
    # ITA-5 per-alias egress gates: a bulkhead so one saturated target cannot
    # monopolise the tunnel, and a circuit breaker so a dead target is refused
    # fast (code `circuit_open`) instead of burning each caller's full budget.
    integration_testing_max_concurrent_per_alias: int = 4
    integration_testing_breaker_failure_threshold: int = 5
    integration_testing_breaker_cooldown_s: float = 30.0
    # ITA-6 / plan item 3.5: the ALIAS the partner's stack calls back through
    # for bank-initiated cases (`simulator.endpoint = a2a://<this>`). It must
    # name an entry in THIS platform's tunnel allowlist pointing at the
    # simulator's callback API. Deliberately NOT named `npci_simulator` — the
    # UI setting `authority_simulator_url` (config.py, External NPCI Simulator UI)
    # is an unrelated knob and the near-collision caused confusion once
    # (COMBINED plan §Phase-3 3.5).
    integration_testing_simulator_alias: str = "cert_simulator"
    # ITA I-6b (§3.6): the OTHER catalogue entry. Mode selects between these
    # two aliases; the tunnel resolves either one against its own allowlist and
    # cannot tell them apart, which is why mode selection needs no tunnel
    # change at all.
    integration_testing_application_alias: str = "cert_application"
    # §3.6.1 symmetric trigger: when THIS side runs in application mode there
    # is no control API to call, so the deployed application is driven by the
    # same trigger contract the partner's is. Operator-supplied, exactly like
    # the partner's `cert_trigger_url` — the platform cannot deploy a driver
    # or discover an endpoint.
    cert_trigger_url: str = ""
    cert_trigger_secret: str = ""

    # ── Certification loop (CERT-6) ─────────────────────────────────────────
    # Auto-dispatch round N+1 when a partner's fix notification lands. OFF by
    # default — the decided posture is "auto-fix, human approves the round
    # close" (COMBINED_EXECUTION_PLAN §6); with the flag off the fix
    # notification is recorded and re-runs stay operator-triggered.
    cert_auto_loop_enabled: bool = False
    # ITA I-6b (§3.6.4): the C-6 loop refuses to AUTO-dispatch a round in
    # application mode — real deployments have real side effects (duplicate
    # records, notifications, downstream calls) and the loop re-runs cases
    # round after round. OFF by default: those rounds are a human's decision,
    # the same reasoning that put the approval gate in C-5.
    cert_application_mode_auto_dispatch: bool = False
    # SIM-6, the harness AXIS: "sim_pack" certifies via the built-in
    # pack-driven simulator; empty preserves the legacy selection
    # (precert_engine_enabled → precert, else cert-agent). Read only by
    # packs/*/certification.default_harness — never by the orchestrator.
    cert_harness: str = ""
    # ITA-7: the SUITE deadline — how long a run waits for partner-reported
    # results after its own class finished, before the join finalizes it with
    # the unreported cases recorded as such. Derived from CertRun.started_at
    # (no schema change; 0133–0135 stay reserved for SIM/ITA columns).
    cert_suite_deadline_s: float = 600.0
    # Inclusive round cap for the loop. Reaching the cap with zero failures
    # still signs off — converging on the last permitted round is success, not
    # a halt. The no-progress guard (identical failed set two rounds running)
    # matters more than this number.
    cert_max_rounds: int = 5

    # ── Precert engine (in-process driver for the Nfinite precert stack) ────
    # Distinct from the `cert_engine` PARTNER above: this drives the external
    # Nfinite simulators directly (precert -> precert-bank-sim -> precertdb) and
    # grades the response code the bank's switch actually returned.
    # Default OFF — the cert-agent path is unchanged until this is switched on.
    precert_engine_enabled: bool = False
    # Where the engine reaches precert. host.docker.internal resolves to the host
    # from inside the backend container (compose maps host-gateway on Linux); use
    # https://localhost:8090 when the backend runs on the host.
    precert_engine_precert_url: str = "https://host.docker.internal:8090"
    # PEM file containing the precert simulator's certificate (or its issuing
    # CA). Required when `precert_engine_enabled` is true — the old
    # `precert_engine_verify_peers=false` escape hatch (CERT_NONE) was removed
    # (CBOM-TLS-CERTNONE-3). A misconfigured path fails startup loudly rather
    # than silently reverting to the insecure default.
    precert_engine_ca_cert_path: str = ""
    # precertdb is Postgres and lives on the platform's own instance, so the
    # default is the compose service name rather than host.docker.internal.
    precert_engine_db_host: str = "atom_postgres"
    precert_engine_db_port: int = 5432
    precert_engine_db_user: str = "atom_user"
    # Defaults to "" like every other credential here. It used to carry the
    # dev-compose Postgres password literally, on the reasoning that the value
    # was already plaintext in docker-compose.yml so it was a local default
    # rather than a secret. That reasoning no longer holds: compose now reads
    # ${POSTGRES_PASSWORD:-…}, so an operator who overrides the password there
    # would have been silently left on the stale literal here — the one place
    # the override did not reach. Set PRECERT_ENGINE_DB_PASSWORD (the engine is
    # off by default, precert_engine_enabled=False). Was CWE-798.
    precert_engine_db_password: str = ""
    precert_engine_db_name: str = "precertdb"
    # First-cut scope: the single precert scope + bank every readiness is
    # certified against. Per-(change, partner) scope mapping is the next step.
    precert_engine_psp_org_id: str = "OLV101"
    precert_engine_subset: str = "Subset-A2A-BIDI"
    precert_engine_cert_name: str = "Cert1"
    precert_engine_certgroup: str = "REMITTER"
    # Demo pacing — seconds to pause before each A2A message so the cert
    # conversation unfolds on screen instead of completing in one burst.
    # 0 = full speed (prod).
    precert_engine_demo_delay_seconds: float = 2.0
    # precert_engine_hsm_cert_b64 (an RSA public key copied from the now-deleted
    # cfg/precert.cer, once seeded into tbl_cert_file.hsm_file on bank onboarding)
    # was retired here — see CBOM-PQC-HSM-CERT-13 and docs/adr/ADR-0006
    # (Withdrawn). Nothing in this repository reads tbl_cert_file.hsm_file
    # any more: the only code that did was CredEncryptorService, deleted with
    # the rest of precert/ in commit b25b4ea, and the Python connector that
    # remains never read it. Removing the setting means there is no longer a
    # place in this codebase where that RSA key material could be reintroduced
    # by habit; if HSM-backed credential encryption is ever needed again for a
    # reintroduced or replacement simulator, provision it fresh, with a
    # quantum-resistant scheme (hybrid X25519+ML-KEM-768) from the start.

    # ── Excel Testcase Engine — speed knobs ─────────────────────────────────
    # BRD/TSD-only: the XSD-diff feed and the writer's per-workflow TSD-chunk
    # cache were both removed with the RAG layer. `excel_engine_use_xsd_diff`
    # and `engine_writer_tsd_cache` are gone.
    # 429 retry budget for OpenAI-compatible providers. Each attempt waits
    # min(retry_after_header, 2^attempt + jitter) seconds before retry.
    # Default 5 → recovers from spikes up to ~30s without batch-fail.
    engine_rate_limit_max_retries: int = 5

    # ── A8 (architecture review Critical, see ARCHITECTURE_REVIEW_ACTIONS.md
    # §2.1 — the security skill file's §16 "gateway-only security" prohibition
    # promotes this from the original review's "Important") — application-
    # layer inbound body-size limit on the A2A boundary, enforced BEFORE the
    # HMAC middleware finishes draining the body into memory. nginx's
    # `client_max_body_size 50M` mitigates this at the edge, but
    # security_architecture_skills.md §16 explicitly rejects relying on the
    # gateway alone ("weak inner layers become attack paths"), and the prod
    # compose file separately publishes the backend's own port (see A0),
    # which bypasses nginx entirely. 10 MB default — generous for a JSON-RPC
    # envelope + reasonable payload, tight enough that a multi-GB body is
    # rejected before it is fully buffered.
    a2a_max_request_body_bytes: int = 10 * 1024 * 1024

    # ── A9 — per-partner inbound rate limit enforcement (Redis sliding
    # window). `PartnerAgent.rate_limit_rps` has existed since Slice 7 but
    # was never read by any middleware — nginx applies one flat, GLOBAL zone
    # (`limit_req_zone ... rate=100r/s`), so the per-partner column was
    # decorative. This is the per-partner override the column's own comment
    # calls out as "the override hook for Slice 9". Same §16 promotion as A8.
    a2a_rate_limit_enabled: bool = True
    # Sliding window length. 1s windows keyed per-partner in Redis
    # (INCR + EXPIRE) — cheap, and matches the semantics of "rate_limit_rps".
    a2a_rate_limit_window_s: int = 1

    # ── S1 hostility-tier follow-up — Admin API rate limiting. Discovered
    # while classifying every boundary into H1/H2/H3 tiers (neither review
    # PDF raised it): /admin/* has real authenticated-operator authorization
    # but NO throttling, so a compromised admin credential or a CSRF-style
    # abuse from an authenticated browser session could hammer the API with
    # no limit. Reuses the exact Redis fixed-window pattern already proven
    # for the A2A partner boundary above, keyed by user id instead of
    # partner id. See app/core/admin_rate_limit.py. Disabled by default —
    # an operator opts in after confirming it doesn't interfere with any
    # legitimate high-frequency admin tooling (e.g. a polling dashboard).
    admin_rate_limit_enabled: bool = False
    admin_rate_limit_rps: int = 20
    admin_rate_limit_window_s: int = 1

    # ── T3/T4 (THREAT_MODEL.md — partner outbound boundary) ─────────────
    # T4: per-partner bulkhead on outbound A2A calls (core/resilience.py::
    # partner_bulkhead), wired into a2a_client.py's send + resend paths.
    # 0 = disabled (default — no behavior change until an operator opts
    # in), matching the llm_max_concurrent_calls_per_provider convention.
    partner_max_concurrent_calls: int = 0

    # ── Finding #8 completion (architecture review — "No Data Tiering for
    # Large Payloads") — see services/artifact_coldstore_read.py.
    #
    # The compression stage (artifact_tiering_enabled, above) writes a gzip copy
    # and a manifest row but never nulls the source column, so the DATABASE never
    # actually shrinks — the finding's real goal. These two settings close that:
    #
    #  * `artifact_coldstore_read_through` — transparently rehydrates a nulled
    #    `tech_specs.content` / `brds.content` / `a2a_messages.payload` /
    #    `.response_body` from its cold copy on ORM load, so all 114 audited read
    #    sites keep working with no call-site changes. Defaults TRUE: it is a
    #    no-op for any row whose column is non-NULL (i.e. everything, until
    #    nulling is enabled), and having it already active is what makes
    #    enabling nulling safe.
    #
    #  * `artifact_coldstore_null_source` — the actually-destructive step. Nulls
    #    the source column, but ONLY after re-reading the cold copy and
    #    confirming it round-trips identical to the live content. Defaults FALSE
    #    so an operator can run compression, inspect the cold copies, and only
    #    then authorise reclaiming space. The sweep refuses to run this stage at
    #    all if the read-through hook failed to register.
    artifact_coldstore_read_through: bool = True
    artifact_coldstore_null_source: bool = False

    # ── T10 (THREAT_MODEL.md — "No secret-scrubbing pass on workspace
    # contents before GC") — see agents/workspace_secret_scrub.py.
    #
    # Two switches, because the two halves have very different cost and very
    # different certainty:
    #
    #  * `workspace_credential_scrub_enabled` — REMEDIATION of a known, real,
    #    platform-caused leak: `git clone https://oauth2:<token>@host/...`
    #    writes the tokened URL into `.git/logs/**`, and the existing §22
    #    `set_remote` scrub only rewrites `.git/config` (verified empirically —
    #    `git remote set-url` leaves reflogs untouched). Cheap: touches only
    #    git metadata files. Defaults TRUE — this is a real credential sitting
    #    on disk for the whole TTL window, not a hypothetical.
    #
    #  * `workspace_secret_scan_enabled` — DETECTION of a credential committed
    #    into the SOURCE repo being cloned. Reports only (never rewrites a
    #    developer's tracked file — that would corrupt the diff a human is
    #    about to approve). Defaults FALSE because it walks the working tree,
    #    and clones are 200 MB–2 GB: making the GC sweep that protects disk
    #    into a full-tree regex scan is a self-inflicted availability risk.
    #    Bounded by `workspace_secret_scan_max_files` when enabled.
    workspace_credential_scrub_enabled: bool = True
    workspace_secret_scan_enabled: bool = False
    workspace_secret_scan_max_files: int = 20000

    # ── T6 (THREAT_MODEL.md — "No PII redaction before sending content to
    # external LLM providers") — security_architecture_skills.md §10.2
    # ("PII MUST be minimized in downstream flows"). See
    # core/pii_redaction.py and docs/PII_DATA_CLASSIFICATION.md §3.
    #
    # Two independent switches because the two content classes carry very
    # different risk/reward:
    #
    #  * `pii_redaction_freetext_enabled` — partner/human-authored prose
    #    (negotiation messages, A2A free-text). Aggressive pattern set.
    #    Defaults TRUE: this is the surface T6 actually names, the content
    #    is prose (not a machine-readable contract), so over-redaction
    #    costs a little prompt fidelity and nothing else.
    #
    #  * `pii_redaction_docs_enabled` — BRD/TSD/assessment/plan sections
    #    that drive CODE GENERATION. Uses the conservative, label-anchored
    #    PROFILE_DOC pattern set, which deliberately never touches bare
    #    numeric literals (a spec's timeouts, epoch timestamps, response
    #    codes and byte budgets must reach the codegen agent intact —
    #    redacting them would turn a privacy control into a correctness
    #    bug). Defaults FALSE so the codegen hot path is opt-in only, and
    #    an operator can validate against the golden/eval suite first, per
    #    the same discipline ARCHITECTURE_REVIEW_REMEDIATION.md §9
    #    established for prompt changes.
    pii_redaction_freetext_enabled: bool = True
    pii_redaction_docs_enabled: bool = False

    # ── T8 (THREAT_MODEL.md — "No comprehensive admin ACTION audit log")
    # — security_architecture_skills.md §3.5 ("every important control
    # decision... traceable"). See core/admin_action_audit_middleware.py.
    # The middleware records every MUTATING (POST/PUT/PATCH/DELETE)
    # /api/admin/* request generically, so coverage does not depend on
    # remembering to add an explicit record() call to each new endpoint.
    # Defaults TRUE — the write is fail-open (an audit failure never
    # breaks the admin action), and audit coverage that depends on
    # per-endpoint discipline is exactly the gap T8 describes.
    admin_action_audit_enabled: bool = True

    # ── T7 (THREAT_MODEL.md — "MFA not confirmed mandatory specifically for
    # admin-privileged sessions") — security_architecture_skills.md §8.4
    # ("Human administrative access MUST require MFA"). Enforced in
    # core/deps.py::require_admin via the session token's `amr` claim,
    # independent of the platform-wide `mfa_enforced` switch above (which
    # may stay False for non-admin roles while this stays True for admins).
    # Defaults to True (secure-by-default) — set False only as an explicit,
    # logged override while admin MFA enrollment is being rolled out.
    admin_mfa_required: bool = True

    # ── S4 follow-up (ARCHITECTURE_REVIEW_ACTIONS.md — "Move secrets from
    # DB-with-Fernet to a vault/control plane") — ADR-0002 Phase 1. Selects
    # the SecretsProvider backend (app/core/secrets_provider.py). "db_fernet"
    # is the only implemented backend today and preserves today's exact
    # behavior (Postgres + Fernet, unchanged). "vault" is accepted as a
    # config value but raises NotImplementedError at the factory — see
    # ADR-0002 Phase 2 and docs/adr/ADR-0002-secrets-vault-migration.md.
    # Never silently falls back to a different backend than requested, per
    # security_architecture_skills.md §4.3's fail-fast rule.
    secrets_provider_backend: str = "db_fernet"

    # ── S5 (ARCHITECTURE_REVIEW_ACTIONS.md — "Wrap direct OS process
    # execution... in an approved interface") — ADR-0003. Governs the
    # default ProcessExecutor instance (app/core/process_executor.py).
    # Extend the allowlist deliberately, never via this setting alone —
    # code adding a new command should also add a code-review-visible
    # comment explaining why, per ADR-0003's migration step 3.
    process_executor_default_timeout_s: float = 600.0
    process_executor_allowlist: str = (
        "mvn,mvn.cmd,git,java,javac,update-java-alternatives,update-alternatives"
    )

    # ── A15 (architecture review Critical #15, "No Celery Worker Concurrency
    # Caps") — celery_tasks.py previously had no `worker_concurrency`,
    # `task_acks_late`, or `worker_prefetch_multiplier` configuration, so a
    # worker defaulted to CPU-count concurrency. With `db_pool_size` +
    # `db_max_overflow` (A13) connections available PER PROCESS, a 4-core
    # worker running 4 concurrent long agentic tasks (each opening its own DB
    # session) could exceed the pool, causing `QueuePool limit overflow`
    # errors under load. Long-running agentic tasks (minutes-to-hours) also
    # starved short periodic sweeps sharing the same worker/queue.
    celery_worker_concurrency: int = 2
    celery_task_acks_late: bool = True
    celery_worker_prefetch_multiplier: int = 1
    # Routes long-running agentic tasks to a dedicated queue so they cannot
    # starve short periodic sweeps (retry sweep, orphan sweep, GC) sharing a
    # worker process. Operators run a SEPARATE worker per queue (see
    # docs/ARCHITECTURE_REVIEW_REMEDIATION.md §A15 for the two-worker
    # docker-compose command changes this implies) — this setting only
    # controls task->queue ASSIGNMENT; queue->worker binding is a deployment
    # concern (the `-Q` celery CLI flag), left to the operator/compose file.
    celery_agentic_queue: str = "agentic"
    celery_default_queue: str = "celery"

    # ── Finding #14 (architecture review Important, "No Inbound Schema
    # Validation on A2A JSON-RPC Payloads") — SHADOW-MODE strict envelope
    # validation. The A10 fix (already shipped) rejects an unknown
    # `task_type` string outright. This flag adds the NEXT layer: validating
    # the WHOLE inbound envelope against the strict `Envelope` model
    # (protocol.py, `extra="forbid"`) — catching missing required fields,
    # wrong types, and unexpected extra fields — but only MEASURING for now,
    # never blocking. This mirrors the exact shadow-then-enforce pattern
    # already used for agentic_acceptance_predicates / agentic_di_gate: the
    # codebase's own migration-window comments (protocol.py, ~line 343) and
    # existing tests (test_read_envelope_tolerates_legacy_message) confirm
    # real traffic today legitimately sends envelopes the strict model would
    # reject (partners still on the "legacy" wire, retired task-type names
    # like "status_update"). Enforcing before that population is known would
    # start rejecting legitimate partner traffic — exactly the mistake A18's
    # ADR-0004 avoided for the contract gate. Fail-open: a validation
    # exception here NEVER blocks or fails the request; it only emits a
    # SECURITY_EVENT telemetry line comparing what WOULD have failed.
    a2a_strict_envelope_validation: bool = True
    # ENFORCE — do not flip until a`2a_strict_envelope_validation` has run in
    # shadow mode against real partner traffic for long enough to confirm
    # (a) which partners still send non-compliant envelopes, and (b) that
    # they have migrated or been given a coordinated cutover date. Flipping
    # this before that would silently start rejecting a live partner's
    # traffic — see docs/ARCHITECTURE_REVIEW_REMEDIATION.md §A10 for the
    # phased cutover plan this gates.
    a2a_strict_envelope_validation_enforce: bool = False

    # ── A7 (architecture review Critical #13, "HMAC Fail-Open Toggle Exists")
    # — startup validation per security_architecture_skills.md §4.3
    # ("applications MUST validate hostility-tier configuration at startup...
    # fail fast instead of starting insecurely"). An ACTIVE partner with no
    # `signing_secret` bypasses the HMAC envelope check entirely (back-compat
    # pass-through); this flag makes that condition fail application startup
    # instead of only logging a once-per-partner warning. Defaults True;
    # exists as a flag (not a hardcoded assertion) so a fresh/dev environment
    # that legitimately provisions partners without secrets yet can disable
    # it explicitly and knowingly rather than the platform silently accepting
    # unauthenticated partner traffic in production.
    a2a_require_hmac_for_active_partners: bool = True

    # ── A13 (architecture review High #6) — DB connection pool tuning.
    # Previously `pool_size`/`max_overflow` were hardcoded literals in
    # `core/database.py` (EA_Skills.md anti-pattern: "hardcoded infrastructure
    # values") and there was no `pool_recycle`/`pool_timeout`, so a MySQL/
    # Postgres `wait_timeout`-dropped connection was only caught by
    # `pool_pre_ping` (one extra round-trip per checkout) rather than
    # proactively refreshed. All four are now externally configurable.
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle_s: int = 3600
    db_pool_timeout_s: int = 30

    # ── A3 (architecture review High #3) — explicit timeouts for the
    # Claude and OpenAI SDK clients. Gemini already sets an explicit
    # httpx.Timeout(connect=10, read=180, write=30, pool=10); Claude/OpenAI
    # previously relied on the SDK's built-in default, so a hung
    # non-streaming call could block indefinitely (the agentic lease TTL
    # eventually kills the RUN, but the blocked coroutine and its resources
    # sit idle until then). Values mirror Gemini's connect/write/pool but
    # allow a longer read window for large completions.
    llm_client_connect_timeout_s: float = 10.0
    llm_client_read_timeout_s: float = 300.0
    llm_client_write_timeout_s: float = 30.0
    llm_client_pool_timeout_s: float = 10.0

    # ── A1/A2 (architecture review Critical #1, #2) — per-provider circuit
    # breaker + bulkhead around `call_llm`/`stream_llm`. Without these, a
    # sustained provider outage lets every in-flight agentic run retry
    # `engine_rate_limit_max_retries` times per call — a thundering herd that
    # delays the provider's own recovery. All externally configurable per
    # EA_Skills.md P4 ("no hardcoded infrastructure values") and
    # security_architecture_skills.md §4.2 (circuit breaker thresholds,
    # bulkhead limits are mandatory per-interface config).
    #
    # Circuit breaker: opens after N consecutive non-retryable-exhausted
    # failures for a given provider; while open, calls fail fast with
    # LlmCircuitOpenError instead of hitting the network. Half-open after
    # the cooldown lets exactly one probe call through; success closes the
    # circuit, failure re-opens it for another cooldown period.
    llm_circuit_breaker_enabled: bool = True
    llm_circuit_breaker_failure_threshold: int = 5
    llm_circuit_breaker_cooldown_s: float = 30.0
    # Half-open probes that must succeed before fully closing (limits
    # flapping when the provider is recovering but still degraded).
    llm_circuit_breaker_half_open_successes: int = 1

    # Bulkhead: max concurrent in-flight calls per provider, across the
    # whole process. Prevents a burst of agentic runs + BRD/TSD generation +
    # deep research from exhausting provider rate limits or the outbound
    # connection pool. 0 disables the bulkhead (unbounded — not recommended).
    llm_max_concurrent_calls_per_provider: int = 20
    # Concurrency knobs for the writer's per-batch fan-out and the
    # validator's per-sheet semantic check. Read by the engine via
    # `app.excel_testcase_engine.config.load_runtime_config()`. Lower these
    # if the LLM provider rate-limits aggressively (Ollama cloud, Claude
    # tier-1); raise them if the provider can sustain more in-flight calls.
    engine_writer_max_concurrent_batches: int = 8
    engine_validator_max_concurrent_sheets: int = 6
    # Single source of truth for the writer's batch size. The engine-side
    # `excel_testcase_engine/config.py::WriterRuntimeConfig` defaults to 5
    # too — keep these aligned so an unset env var produces the same
    # behaviour whether the engine reads via the shim or directly.
    # (Was 6 historically, dropped to 5 on 2026-05-06 to fit AiNxt's
    # response cap with safety margin; bump together if you change one.)
    engine_writer_cases_per_batch: int = 5
    engine_validator_enable_xsd_check: bool = True
    engine_retry_max_attempts: int = 3
    # Cap on total test cases across the workbook; 0/None reverts the engine to
    # its coverage-matrix-driven count. Declared here because the engine reads
    # it off the HOST settings (`excel_testcase_engine/config.py`), and with
    # `extra="ignore"` an undeclared name means ENGINE_TEST_CASE_CAP is dropped
    # silently and the engine default always wins — the other five knobs in
    # that same block were declared, this one was missed. Value matches the
    # engine's own RuntimeConfig default, so behaviour is unchanged.
    engine_test_case_cap: int = 25

    # CORS
    frontend_url: str = "http://localhost:3000"

    # A2A — full URL where THIS platform instance is reachable from
    # partner agents. Published in `supportedInterfaces[0].url` on the
    # agent card (the SDK posts here verbatim, so a bare path
    # won't work — httpx requires the protocol). Default targets the
    # docker service name; production overrides via AUTHORITY_PUBLIC_URL env.
    authority_public_url: str = "http://atom_backend:8000"

    # Phase B — GitLab
    gitlab_url: str = ""
    gitlab_token: str = ""
    # Write-scoped token used ONLY for the Phase B code push (git push of the
    # feature branch). `gitlab_token` clones/indexes repos and only needs READ;
    # pushing needs write_repository + Developer on the target project, which is
    # often a different credential. When empty, the push falls back to
    # `gitlab_token` (behaviour unchanged). Env: GITLAB_PUSH_TOKEN.
    gitlab_push_token: str = ""
    gitlab_repo: str = ""       # e.g. "group/project"
    gitlab_branch: str = "main"

    # ── Partner TLS (outbound A2A / connectivity test) ───────────────────────
    # Global defaults for verifying partner HTTPS endpoints. Per-partner values
    # (PartnerAgent.tls_verify / ca_cert_pem) override these when set.
    #   PARTNER_TLS_VERIFY=false disables verification for partners with no own
    #     setting (internal networks / self-signed certs). Default True (secure).
    #   PARTNER_CA_BUNDLE=/path/to/ca.pem trusts an internal CA for partners
    #     with no uploaded cert. Applies to BOTH the Test-connectivity probe and
    #     the real outbound A2A calls, so trusting the CA fixes both.
    partner_tls_verify: bool = True

    # ── SSRF guard for operator-supplied outbound URLs ───────────────────────
    # Applies to partner endpoint_url (admin partner registry) — see
    # app.core.ssrf_guard. Replaces a string-prefix blocklist that could be
    # bypassed simply by using https:// instead of http://, and that was skipped
    # entirely when ENVIRONMENT was uat/staging (which is what compose sets).
    #
    #   "enforce" (default) — loopback / link-local / multicast / private
    #       targets are refused with 400, and the resolved address is PINNED so
    #       the name cannot be rebound between the check and the fetch.
    #   "observe" — nothing is rejected; anything that WOULD be blocked is
    #       logged. For a staged rollout on an environment that has internal
    #       partners not yet allowlisted.
    #   "off" — no checking (escape hatch).
    #
    # Anything unrecognised is treated as "enforce", so a typo fails safe.
    ssrf_guard_mode: str = "enforce"
    # Comma-separated hostnames exempt from the guard. Name the approved internal
    # partners here and they keep working under enforcement. Matched on the URL
    # host, case-insensitively.
    #   SSRF_ALLOWED_INTERNAL_HOSTS=partner.internal,10.20.30.40
    ssrf_allowed_internal_hosts: str = ""
    # Blanket permission for RFC-1918 space (10/8, 172.16/12, 192.168/16 and the
    # IPv6 equivalents) when a deployment has many internal partners and naming
    # each one is impractical. Loopback, link-local (the cloud metadata range),
    # multicast and unspecified addresses stay blocked either way — no partner is
    # ever legitimately reachable there, so this flag does NOT re-enable them.
    ssrf_allow_private_networks: bool = False
    # Whether a hostname that fails to RESOLVE is refused (default) or allowed.
    #
    # Fail-closed is the safe default: a name that does not resolve at check time
    # could resolve to a private address moments later (DNS rebinding), and the
    # guard cannot vet an address it never saw.
    #
    # Set false only if a flaky resolver is causing more outages than the
    # rebinding risk justifies. This exists so that trade-off is an explicit,
    # reviewable setting rather than a silent edit to the guard.
    ssrf_block_on_resolution_failure: bool = True

    # ── Cleartext transport policy (CWE-319) ─────────────────────────────────
    # SEPARATE from the SSRF settings above, on purpose. SSRF asks "could this
    # URL reach somewhere I shouldn't?" (public fine, private suspect); this
    # asks the inverted question, "would this put plaintext on a network I
    # don't control?" (private fine, public cleartext refused). Sharing one
    # switch would mean loosening SSRF for an internal partner silently also
    # permitted cleartext to the open internet.
    #
    #   enforce (default) — refuse http:// to a non-local destination
    #   observe           — log what would be refused, proceed anyway
    #   off               — no checking
    #
    # Enforcing by default is non-breaking: docker service names and localhost
    # resolve to private addresses and pass unchanged. See app.core.ssrf_guard
    # (`check_cleartext_url`); every other service on this wire carries its own
    # equivalent of the same rule.
    cleartext_policy_mode: str = "enforce"
    # Comma-separated hostnames exempt from the rule — for a destination that
    # cannot resolve privately but is reached over a trusted link. Prefer
    # https:// over widening this.
    cleartext_allowed_hosts: str = ""

    partner_ca_bundle: str = ""
    # Global default cap on the base64-encoded (wire) size of a SINGLE product-kit
    # attachment shipped inline in the A2A envelope. Attachments whose encoded size
    # exceeds this are OMITTED from the envelope (metadata + checksum kept) so a
    # large inline video/docx can't blow a partner ingress body-size limit (ngrok /
    # envoy) — the failure mode where a kit send resets mid-upload. 0 = no limit
    # (inline everything — the legacy behaviour). Overridden per-partner by
    # `partner_agents.max_inline_attachment_bytes` (NULL there = inherit this).
    partner_max_inline_attachment_bytes: int = 0

    # Phase B — Jenkins
    jenkins_url: str = ""
    jenkins_user: str = ""
    jenkins_token: str = ""
    jenkins_job_name: str = ""

    # Phase B — UAT server (SSH + health check)
    uat_server_host: str = ""
    uat_server_user: str = ""
    uat_server_key_path: str = ""
    uat_deploy_script: str = ""
    uat_health_check_url: str = ""

    # Phase B — Unified build+deploy (Session 23 / 2026-05).
    # The platform invokes the operator's pre-existing
    # `build_and_deploy.sh`. Two execution modes are supported:
    #
    #   "ssh"   (default) — backend SSH-es into a separate host (the
    #           build VM that owns GitLab creds + the deploy tree) and
    #           streams stdout/stderr from the remote process. Uses
    #           PHASE_B_HOST / PHASE_B_HOST_USER / PHASE_B_HOST_KEY.
    #
    #   "local" — backend spawns the script as a local subprocess. The
    #           script (and everything it touches: mvn, GitLab token,
    #           /appdata deploy tree, service starter.sh files) must be
    #           reachable from the backend process itself. For docker
    #           deployments that means bind-mounting those paths into
    #           the container.
    #
    #   "build" — no host script at all. The backend clones the registered
    #           repos (core first, then app, in sequence) and runs a plain
    #           `mvn clean install -DskipTests --fail-at-end` per repo via the
    #           same contained subprocess path verify uses. Module exclusion
    #           reuses AGENTIC_VERIFY_SKIP_MODULES (a failure attributable only
    #           to a matched module is downgraded). The DEPLOY is mocked from
    #           the jars actually built. Needs git + mvn + a JDK reachable from
    #           the backend (same as verify); does NOT need build_and_deploy.sh.
    #
    # "ssh"/"local" use PHASE_B_BUILD_SCRIPT (absolute path of
    # build_and_deploy.sh); "build" ignores it.
    phase_b_runner_mode: str = "ssh"
    # No defaults: these named a real internal build host, service account and
    # deploy-key path. phase_b_runner_mode defaults to "ssh", so an unset host
    # fails at connect time with an empty-host error rather than dialling
    # someone else's machine. Set PHASE_B_HOST / PHASE_B_HOST_USER in the
    # environment (docker-compose.yml passes them through).
    phase_b_host: str = ""
    phase_b_host_user: str = ""
    phase_b_host_key: str = "/run/secrets/deploy_key"
    phase_b_build_script: str = ""
    # SSH connect timeout in seconds. Build itself can run for many
    # minutes; this only gates the initial connection.
    phase_b_host_connect_timeout: int = 30
    # Path to the SSH known_hosts file for host key verification when
    # connecting to the build host. Empty string (default) auto-discovers
    # /etc/ssh/ssh_known_hosts or ~/.ssh/known_hosts; if none exist, host
    # key verification is disabled with a WARNING log. Operators SHOULD
    # provision this for production.
    build_host_known_hosts: str = ""
    # Allowlist ROOT for request-supplied build/test script paths (the Phase B
    # Build and UAT panels let the operator name WHICH script runs). Empty
    # (default) disables request-supplied paths entirely — triggers then fall
    # back to PHASE_B_BUILD_SCRIPT exactly as before. When set, a supplied path
    # must resolve (symlinks included) to a regular *.sh file INSIDE this root
    # or the trigger is rejected; nothing outside the root is ever executed.
    # Sample scripts live in backend/examples/phase_b_scripts.
    phase_b_script_root: str = ""
    # Fixed default UAT test script (absolute path on the backend host),
    # the UAT analogue of PHASE_B_BUILD_SCRIPT: used when a trigger supplies
    # no script_path, in ANY runner mode — UAT scripts are HTTP clients of the
    # deployed stack, so they run on the backend host even for ssh builds.
    # Operator config (same trust level as PHASE_B_BUILD_SCRIPT), so it is not
    # confined to PHASE_B_SCRIPT_ROOT; empty = no default (a script_path is
    # then required, which needs local mode + the allowlist root).
    phase_b_test_script: str = ""
    # Hard wall-clock ceiling for an operator script run (build or UAT). On
    # expiry the subprocess is killed and the run is recorded as failed — a
    # hung script must not hold a RUNNING row (and its re-trigger guard)
    # forever.
    phase_b_script_timeout_seconds: int = 3600

    # Security: when False (default), LLM reasoning/thinking blocks are
    # not emitted to the client-facing event stream. Set to True only in
    # development or debugging scenarios.
    expose_llm_reasoning: bool = False

    @model_validator(mode="after")
    def _check_secret_key_length(self) -> "Settings":
        if self.app_env != "development" and len(self.secret_key) < 32:
            raise ValueError(
                "secret_key must be at least 32 characters in non-development environments"
            )
        return self

    @model_validator(mode="after")
    def _check_internal_token_not_default(self) -> "Settings":
        """Refuse to run production without a real cert-agent token.

        `cert_agent_internal_token` is the only thing authenticating backend →
        cert-agent calls. SCR findings #3/#9 flagged it having a hardcoded,
        published-in-source default value — it now defaults to "" like every
        other secret field, so an unset value is caught here explicitly
        instead of silently authenticating on a public string.

        Also rejects the legacy literal directly, in case an existing .env
        file still sets CERT_AGENT_INTERNAL_TOKEN=dev-internal-token from
        before this default changed.

        Scoped to production for the hard failure: development and UAT get a
        startup warning (see startup_validation.py) but keep booting, so no
        local/UAT workflow breaks. Set CERT_AGENT_INTERNAL_TOKEN explicitly in
        every environment that talks to a real cert-agent.
        """
        if self.app_env == "production" and (
            not self.cert_agent_internal_token
            or self.cert_agent_internal_token == _LEGACY_DEV_INTERNAL_TOKEN
        ):
            raise ValueError(
                "cert_agent_internal_token must be set to a strong, unique value in "
                "production — it is either unset or still the shipped development "
                "default. Set CERT_AGENT_INTERNAL_TOKEN (and the matching "
                "CERTSIM_INTERNAL_TOKEN) before running in production."
            )
        return self

    @model_validator(mode="after")
    def _check_config_encryption_key(self) -> "Settings":
        """Refuse to run production without a dedicated config encryption key.

        config_encryption_key protects DB-stored secrets (API keys, tokens,
        passwords). When unset, the encryption falls back to a key derived
        from secret_key, which ties secret storage to the JWT signing secret.
        """
        if self.app_env == "production":
            cek = (self.config_encryption_key or "").strip()
            if not cek:
                raise ValueError(
                    "config_encryption_key must be set in production — DB-stored secrets "
                    "(API keys, tokens, passwords) cannot be encrypted without it. "
                    "Set CONFIG_ENCRYPTION_KEY to a Fernet.generate_key() value."
                )
        return self

    @model_validator(mode="after")
    def _check_integration_testing_allowlist(self) -> "Settings":
        """Validate the tunnel allowlist at STARTUP, not at request time.

        Security skill §4.3: validate tier config at boot and fail fast. A
        tunnel that starts with an unparsable policy and then decides per
        request is one bad `except` away from failing OPEN, which for this
        interface means arbitrary SSRF. Refusing to boot is the safe failure.

        Only checked when the tunnel is enabled — an operator who has not
        turned it on should not be blocked by a policy nothing will read. And
        note what a VALID-but-empty allowlist means: the tunnel resolves no
        aliases and reaches nothing, which is the correct posture for "enabled
        but not yet configured".
        """
        if not self.integration_testing_enabled:
            return self
        if self.app_env == "production":
            raise ValueError(
                "integration_testing_enabled must be false in production — the "
                "tunnel is a dev-only facility and its ingress is externally "
                "reachable (H3)."
            )
        # ITA-5: the §6 budget must SHRINK INWARD (ingress > A2A send >
        # egress→target). Equal-or-inverted layers only misbehave under load —
        # the outermost fires first and every failure reads as a generic 504
        # with no inner detail — so the ordering is refused at boot, where the
        # operator can actually see it.
        if not (self.integration_testing_ingress_timeout_s
                > self.integration_testing_a2a_timeout_s
                > self.integration_testing_target_timeout_s):
            raise ValueError(
                "integration-testing timeout budget must shrink inward: "
                f"ingress ({self.integration_testing_ingress_timeout_s}s) > "
                f"a2a ({self.integration_testing_a2a_timeout_s}s) > "
                f"target ({self.integration_testing_target_timeout_s}s). "
                "See INTEGRATION_TESTING_AGENT_PLAN.md §6."
            )
        from app.a2a_common.integration_allowlist import AllowlistError, load_allowlist

        try:
            load_allowlist(self.integration_testing_allowlist)
        except AllowlistError as exc:
            raise ValueError(
                f"INTEGRATION_TESTING_ALLOWLIST is invalid: {exc}. The tunnel "
                "refuses to start with a policy it cannot parse rather than "
                "run permissive."
            ) from None
        if self.integration_testing_max_hops < 1:
            raise ValueError("integration_testing_max_hops must be at least 1")
        return self

    @model_validator(mode="after")
    def _check_http_defaults_in_production(self) -> "Settings":
        """Refuse to run production with cleartext service URLs.

        Multiple service URLs default to http://, which means LLM prompts/
        responses, certification envelopes, Redis session data, and API keys
        travel in cleartext within the docker network. In production every
        service URL that carries sensitive data must use https://.

        NOTE: localhost/127.0.0.1 URLs are exempt because the traffic stays
        on the same machine and is not network-observable.
        """
        if self.app_env != "production":
            return self

        _http_checks = [
            ("ollama_url", "LLM prompts and responses"),
            ("authority_simulator_url", "certification envelopes"),
            ("redis_url", "session data, rate-limit counters, and JWT denylist entries"),
            ("ainxt_base_url", "LLM prompts and responses (AiNxt gateway)"),
            ("grok_base_url", "LLM prompts and responses (Grok/xAI)"),
            ("gemini_video_base_url", "video generation API keys and prompts"),
            ("cert_agent_url", "certification traffic and internal tokens"),
            ("bank_agent_url", "bank-agent traffic"),
            ("authority_public_url", "published agent card URL"),
        ]
        for attr, label in _http_checks:
            val = (getattr(self, attr, "") or "").strip()
            if val.lower().startswith("http://"):
                # Exempt localhost/127.0.0.1 — traffic stays on the same machine.
                import urllib.parse as _up
                _host = _up.urlparse(val).hostname or ""
                if _host in ("localhost", "127.0.0.1", "::1"):
                    continue
                raise ValueError(
                    f"{attr} ({val}) uses http:// in production — "
                    f"{label} would travel in cleartext. "
                    "Use https:// or a Unix socket."
                )
        return self


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
