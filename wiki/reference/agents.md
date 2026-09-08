<!--
Hand-derived from the source tree. There is NO generator and no CI job that
compares this file against one. An earlier header claimed both, which told
whoever noticed the page had gone stale that they were not allowed to fix it.

Re-derive the figures below when the source changes, and update the stamp:
    ls backend/app/agents/*.py | grep -v __init__ | wc -l   # module count
    grep -n 'PURPOSE' backend/app/core/llm_router.py       # the purpose map
-->
# Agent catalogue

> **Derived** from `app/core/llm_router.py + app/agents/`, against alembic head `0141_rename_remaining_columns`.
> Hand-derived from the source tree — re-derive and re-stamp when it changes
> (see the comment at the top of this file).

106 agent modules; 36 carry an explicit model-routing purpose. The purpose map holds 50 entries in all — the other 14 name stages, or agents that live elsewhere in the tree.

Purpose drives model selection through `llm_router.pick_model_for()`. An agent with no entry falls back to `REASONING` -- see `app/core/llm_router.py`. Never pin a model string inside an agent.

| Agent | Purpose | Module | Summary |
|---|---|---|---|
| `acceptance_predicates` | _(default)_ | `app/agents/acceptance_predicates.py` | Deterministic acceptance predicates — the tier-1 completeness check (R4). |
| `adr_checker` | `utility` | `app/agents/adr_checker.py` | ADR contradiction check. |
| `adversarial_reviewer` | _(default)_ | `app/agents/adversarial_reviewer.py` | Adversarial code reviewer. |
| `agentic_edit` | _(default)_ | `app/agents/agentic_edit.py` | The 4-level string-match edit ladder. |
| `agentic_events` | _(default)_ | `app/agents/agentic_events.py` | Event persistence + coding activity log for the agentic state machine. |
| `agentic_goal_verifier` | _(default)_ | `app/agents/agentic_goal_verifier.py` | Goal-verifier reviewer — panel runner, prompt, evidence packet. |
| `agentic_orchestrator` | _(default)_ | `app/agents/agentic_orchestrator.py` | The orchestrator — drives the durable state machine through every phase. |
| `agentic_push` | _(default)_ | `app/agents/agentic_push.py` | Shared short branch + multi-repo push actions. |
| `agentic_review` | _(default)_ | `app/agents/agentic_review.py` | Read-only review subagent. |
| `agentic_runtime` | _(default)_ | `app/agents/agentic_runtime.py` | The bounded agentic loop. |
| `agentic_state` | _(default)_ | `app/agents/agentic_state.py` | The durable, resumable, leased state machine. |
| `agentic_subagents` | _(default)_ | `app/agents/agentic_subagents.py` | XSD-Discovery + Code-Change subagents. |
| `agentic_tools` | _(default)_ | `app/agents/agentic_tools.py` | Agentic tool set + registry. |
| `ambiguity_detector` | `utility` | `app/agents/ambiguity_detector.py` | Gap / ambiguity detection for feature specs. |
| `assumption_handler` | `utility` | `app/agents/assumption_handler.py` | Split detected gaps into PM-blocking vs safely assumable. |
| `ast_editor_java` | _(default)_ | `app/agents/ast_editor_java.py` | Diff-based SEARCH/REPLACE patch editor. |
| `blueprints` | _(default)_ | `app/agents/blueprints.py` | Per-document-type section blueprints — the domain-facing CONTENT. |
| `brd_corrector` | _(default)_ | `app/agents/brd_corrector.py` | Correct an uploaded BRD to match the plan for 'plan-wins' resolutions (Path B). |
| `brd_extractor` | _(default)_ | `app/agents/brd_extractor.py` | BRD requirement extractor + classifier. |
| `build_triager` | `routing` | `app/agents/build_triager.py` | Build-failure triager (verify tier). |
| `canvas` | `reasoning` | `app/agents/canvas.py` | Product Canvas Generator agent. |
| `cert_testing` | `reasoning` | `app/agents/cert_testing.py` | Certification Testing Agent — executes bidirectional tests between the Authority and partners. |
| `cert_triage` | `reasoning` | `app/agents/cert_triage.py` | Cert Triage Agent — analyzes failed certification tests and produces verdicts. |
| `change_walkthrough` | `reasoning` | `app/agents/change_walkthrough.py` | Change Walkthrough agent — turns an implemented change into a plain-language flow |
| `citation_validator` | `utility` | `app/agents/citation_validator.py` | Citation validator. |
| `citations` | _(default)_ | `app/agents/citations.py` | Shared citation-enforcement helpers. |
| `cluster_analyzer` | _(default)_ | `app/agents/cluster_analyzer.py` | Cross-partner cluster analyzer. |
| `cluster_router` | _(default)_ | `app/agents/cluster_router.py` | LLM-based cluster router. |
| `code_change` | `reasoning` | `app/agents/code_change.py` | Code Change Agent. |
| `code_plan_schema` | _(default)_ | `app/agents/code_plan_schema.py` | CodePlan schema + validator. |
| `code_planner` | `reasoning` | `app/agents/code_planner.py` | Code planner — structured-output generator. |
| `code_review` | `reasoning` | `app/agents/code_review.py` | Code Review Agent. |
| `codegen_preflight` | _(default)_ | `app/agents/codegen_preflight.py` | Human-language dependency preflight for agentic code generation. |
| `context_assembler` | _(default)_ | `app/agents/context_assembler.py` | Context-Assembler — builds the scoped, stale-aware ContextPack. |
| `contract_gate` | _(default)_ | `app/agents/contract_gate.py` | Deterministic, LLM-free contract checks for a generated code change-set. |
| `cross_doc_consistency` | _(default)_ | `app/agents/cross_doc_consistency.py` | Cross-document BRD↔TSD consistency (pending #3). |
| `cross_module_gate` | _(default)_ | `app/agents/cross_module_gate.py` | Mandatory cross-module analysis gate. |
| `decline_designer` | `reasoning` | `app/agents/decline_designer.py` | Decline Designer agent — authors the per-feature Decline & Timeout design. |
| `deep_researcher` | `reasoning` | `app/agents/deep_researcher.py` | Deep Researcher agent. |
| `delta_grounding` | `reasoning` | `app/agents/delta_grounding.py` | Delta grounding — code-back the plan amendments a reconciliation folds in. |
| `di_wiring_gate` | _(default)_ | `app/agents/di_wiring_gate.py` | Deterministic static Spring DI-wiring checks — the context-load gate. |
| `diff_stats_gate` | `routing` | `app/agents/diff_stats_gate.py` | Diff-stats gate. |
| `doc_alignment` | _(default)_ | `app/agents/doc_alignment.py` | BRD→plan alignment (extension + divergence) detection for uploaded-doc reconciliation. |
| `doc_code_consistency` | _(default)_ | `app/agents/doc_code_consistency.py` | Document↔code consistency gate (post-codegen). |
| `doc_consistency` | `reasoning` | `app/agents/doc_consistency.py` | Document↔plan consistency gate. |
| `doc_impact` | `routing` | `app/agents/doc_impact.py` | Doc-impact agent — does the Authority's reply imply a Product Kit document change? |
| `document_validator` | `utility` | `app/agents/document_validator.py` | Post-generation document validator. |
| `enrichment` | `reasoning` | `app/agents/enrichment.py` | One-shot enrichment generator. |
| `enrichment_schema` | _(default)_ | `app/agents/enrichment_schema.py` | EnrichedStory schema + validator. |
| `escalation_advisor` | `reasoning` | `app/agents/escalation_advisor.py` | Escalation advisor — drafts the *review team's* own assessment. |
| `feasibility_resolver` | _(default)_ | `app/agents/feasibility_resolver.py` | Feasibility resolver — authority-side recommendation engine. |
| `flow_context_generator` | _(default)_ | `app/agents/flow_context_generator.py` | Index-time API FLOW-MAP generator (reuse-first). |
| `git_guard` | _(default)_ | `app/agents/git_guard.py` | Git-guard hook — the remote-write security boundary. |
| `goal_verifier_core` | _(default)_ | `app/agents/goal_verifier_core.py` | Goal-verifier reviewer — pure core (schema, parse, gap-fingerprint, quorum). |
| `governance_bundle` | _(default)_ | `app/agents/governance_bundle.py` | Governance skill BUNDLES — safe archive handling, classification, safety gate. |
| `governance_orchestrator` | _(default)_ | `app/agents/governance_orchestrator.py` | Governance review stages (EA → InfoSec) — the pre-build compliance loop. |
| `governance_sandbox` | _(default)_ | `app/agents/governance_sandbox.py` | Sandboxed execution of governance-skill scripts + contract-true result parsing. |
| `governance_skills` | _(default)_ | `app/agents/governance_skills.py` | Governance skill files (EA / InfoSec rulebooks) — pure parsing/injection helpers. |
| `impact_block` | _(default)_ | `app/agents/impact_block.py` | Shared `build_impact_block` helper. |
| `infra_errors` | _(default)_ | `app/agents/infra_errors.py` | Classify infra/config errors that the USER must fix (not the agent). |
| `is_review` | `reasoning` | `app/agents/is_review.py` | IS (Information Security) Review Agent. |
| `jaxb_mapper` | _(default)_ | `app/agents/jaxb_mapper.py` | JAXB binding model — the element→Java link. |
| `jdk_discovery` | _(default)_ | `app/agents/jdk_discovery.py` | Installed-JDK discovery + selection (Java-version awareness, §18.1). |
| `lsp_client` | _(default)_ | `app/agents/lsp_client.py` | Minimal Eclipse JDT Language Server (jdtls) client for on-demand diagnostics. |
| `manifest` | _(default)_ | `app/agents/manifest.py` | Immutable approved ChangeManifest + human approval + push preflight (§11). |
| `maven_parser` | _(default)_ | `app/agents/maven_parser.py` | Parse Maven / javac output into structured diagnostics. |
| `module_context_generator` | _(default)_ | `app/agents/module_context_generator.py` | Index-time module-wise context generation — "the heart". |
| `negotiation` | `reasoning` | `app/agents/negotiation.py` | Negotiation Agent — drafts responses to partner queries using RAG context. |
| `negotiation_classifier` | _(default)_ | `app/agents/negotiation_classifier.py` | BRD-based counter-proposal classifier. |
| `party_inference` | _(default)_ | `app/agents/party_inference.py` | Infer which canonical parties are in-scope for a change from its |
| `plan_audit` | _(default)_ | `app/agents/plan_audit.py` | Plan enforcement audit — challenge the PLAN itself, conservatively. |
| `plan_contract` | _(default)_ | `app/agents/plan_contract.py` | The ratified solution-design contract — the BINDING technical surface that BRD/TSD generation |
| `plan_coverage` | _(default)_ | `app/agents/plan_coverage.py` | Plan-coverage (omission) detection for uploaded-doc reconciliation. |
| `plan_fidelity` | `reasoning` | `app/agents/plan_fidelity.py` | Plan-fidelity gate — after code-gen, verify the change actually DELIVERED the ratified plan, |
| `plan_files` | _(default)_ | `app/agents/plan_files.py` | The ratified plan's per-file change list — one reader for every consumer. |
| `plan_versioning` | _(default)_ | `app/agents/plan_versioning.py` | Plan versioning at the approach gate. |
| `platform_adapter` | _(default)_ | `app/agents/platform_adapter.py` | Platform-aware command execution. |
| `product_kit_agent` | _(default)_ | `app/agents/product_kit_agent.py` | Product Kit Generator agent. |
| `prompt_enhancer` | `reasoning` | `app/agents/prompt_enhancer.py` | Prompt Enhancer agent. |
| `proposals_extractor` | `routing` | `app/agents/proposals_extractor.py` | Structured JSON proposals extractor. |
| `question_generator` | `routing` | `app/agents/question_generator.py` | Convert blocking gap keys into PM-friendly questions. |
| `rag_explorer` | _(default)_ | `app/agents/rag_explorer.py` | RAG Explorer agent — temporary testing utility. |
| `repo_scope` | _(default)_ | `app/agents/repo_scope.py` | Server-enforced repository selection, scoping, and index provenance (§5). |
| `revision_planner` | `reasoning` | `app/agents/revision_planner.py` | Revision planner — turn a round's resolved outcomes into a per-doc kit plan. |
| `schema_guardian` | _(default)_ | `app/agents/schema_guardian.py` | Deterministic XSD reuse-vs-create guardian. |
| `self_correction` | `reasoning` | `app/agents/self_correction.py` | Self-correction loop — orchestrator. |
| `strategist` | `reasoning` | `app/agents/strategist.py` | One-shot structural strategist. |
| `stuck_helper` | `routing` | `app/agents/stuck_helper.py` | Stuck-run helper — when a run errors and retry/resume just re-hits the same wall, the |
| `taxonomy` | `routing` | `app/agents/taxonomy.py` | Feature taxonomy classifier — buckets come from the active domain pack. |
| `toolchain_report` | _(default)_ | `app/agents/toolchain_report.py` | System-dependency preflight. |
| `tsd_test_generator` | _(default)_ | `app/agents/tsd_test_generator.py` | TSD-derived test generation. |
| `uat_triage` | `reasoning` | `app/agents/uat_triage.py` | UAT triage agent — AI triage of the Phase B Build + UAT logs (Triage step). |
| `upload_reconciler` | _(default)_ | `app/agents/upload_reconciler.py` | Reconcile an uploaded document against the ratified Change-Analysis plan. |
| `verification_plan` | _(default)_ | `app/agents/verification_plan.py` | Runtime-owned VerificationPlan + deterministic hard gates. |
| `verifier` | _(default)_ | `app/agents/verifier.py` | Pluggable verification backends (dependency-decoupling). |
| `version_change_summary` | `reasoning` | `app/agents/version_change_summary.py` | Version change-summary agent — partner-facing "what changed" note. |
| `video_script_schema` | _(default)_ | `app/agents/video_script_schema.py` | Pydantic models for the segmented video script (Phase A Product Kit). |
| `workspace_local` | _(default)_ | `app/agents/workspace_local.py` | Per-run workspace: clone layout, leasing, reset, and GC. |
| `workspace_secret_scrub` | _(default)_ | `app/agents/workspace_secret_scrub.py` | Workspace credential scrubbing — closes threat T10. |
| `xml_template_generator` | `reasoning` | `app/agents/xml_template_generator.py` | xml_template_generator — drafts a Mustache XML request template for a new |
| `xsd` | `reasoning` | `app/agents/xsd.py` | XSD Generator agent. |
| `xsd_graph_builder` | _(default)_ | `app/agents/xsd_graph_builder.py` | XSD schema graph + deterministic element-index diff. |
| `xsd_namespace` | _(default)_ | `app/agents/xsd_namespace.py` | Deterministic XSD namespace canonicalization. |

## Routed names with no module in `app/agents/`

These appear in the purpose map but are not modules there -- they are stage names or agents that live elsewhere in the tree.

- `adversarial` -- `reasoning`
- `ast_editor` -- `routing`
- `brd` -- `reasoning`
- `code_summarizer` -- `utility`
- `context_compressor` -- `utility`
- `doc_code_linker` -- `routing`
- `docgen_patch_planner` -- `reasoning`
- `gov_ea_review` -- `reasoning`
- `gov_fix` -- `reasoning`
- `gov_is_review` -- `reasoning`
- `product_kit` -- `reasoning`
- `query_understanding` -- `routing`
- `stuck_helper_validator` -- `routing`
- `tech_spec` -- `reasoning`
