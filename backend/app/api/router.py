# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

from fastapi import APIRouter
from app.api.auth import router as auth_router
from app.api.users import router as users_router
from app.api.change_requests import router as changes_router
from app.api.product_kit_video import router as product_kit_video_router
from app.api.rag import router as rag_router
from app.api.logs import router as logs_router
from app.api.agents import router as agents_router
from app.api.phase_b import router as phase_b_router
from app.api.code_indexing import router as code_indexing_router
from app.api.admin_build_smoke import router as admin_build_smoke_router
from app.api.app_config import router as app_config_router
from app.api.partners import router as partners_router
from app.api.a2a import router as a2a_router
from app.api.phase_c import router as phase_c_router
from app.api.clarifications import router as clarifications_router
from app.api.kg_admin import router as kg_admin_router
# R-1 — durable agent-job registry (resume-progress feature foundation)
from app.api.jobs import router as jobs_router
# Cert-agent slice (Session 24) — partner certification surface.
from app.api.cert_report import router as cert_report_router
# Pack-driven simulator (SIM-2…SIM-4) — pack store, resolution, execution.
from app.api.sim_packs import router as sim_packs_router
from app.api.sim_execute import router as sim_execute_router
from app.api.cert_timeline import router as cert_timeline_router
from app.api.cert_simulator_sync import router as cert_simulator_sync_router
# Push test cases for a change to the cert-agent environment.
from app.api.cert_push import router as cert_push_router
# Admin surface for hand-firing Part B cert messages (internal testing).
from app.api.cert_a2a_trigger import router as cert_a2a_trigger_router
from app.api.assignment_actions import router as assignment_actions_router
# Slice 25 — Admin A2A communications log UI backend.
from app.api.a2a_logs import router as a2a_logs_router
from app.api.notifications import router as notifications_router
from app.api.negotiation_mgmt import router as negotiation_mgmt_router
# Feasibility Resolver — admin-edited the Authority policy doc (R1).
from app.api.authority_policy import router as authority_policy_router
# Feasibility Resolver — recommendation endpoints (R2).
from app.api.resolver import router as resolver_router
# Product Kit publication snapshots (immutable per-version shipped envelopes).
from app.api.kit_publications import router as kit_publications_router
# Eval harness Phase 1 — verdict read APIs.
from app.api.eval import router as eval_router
# Escalation routing — Risk / InfoSec / Tech review-team inboxes.
from app.api.escalations import router as escalations_router
# Post-freeze emergency issues (break-glass partner channel).
from app.api.emergency_issues import router as emergency_issues_router
# Agentic XSD-driven codegen — start / approve / events / WS (THE BOOK §13).
from app.api.agentic import router as agentic_router
# API Registry — canonical network wire-API field constraints (deterministic TSD specs).
from app.api.api_registry import router as api_registry_router
# Governance reviews — EA/InfoSec skill management + pre-build review stages.
from app.api.governance import router as governance_router
# Integration-testing tunnel ingress (ITA I-1) — an H3 interface, off by
# default and dev-only. Registered unconditionally; the route itself refuses
# with 503 unless `integration_testing_enabled`, so the surface is one
# well-tested rejection rather than a conditional import.
from app.api.integration_testing import router as integration_testing_router

api_router = APIRouter()

api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(changes_router)
api_router.include_router(product_kit_video_router)
api_router.include_router(rag_router)
api_router.include_router(logs_router)
api_router.include_router(agents_router)
api_router.include_router(phase_b_router)
api_router.include_router(code_indexing_router)
api_router.include_router(app_config_router)
api_router.include_router(admin_build_smoke_router)
api_router.include_router(partners_router)
api_router.include_router(a2a_router)
api_router.include_router(phase_c_router)
api_router.include_router(clarifications_router)
api_router.include_router(kg_admin_router)
api_router.include_router(jobs_router)
api_router.include_router(cert_timeline_router)
api_router.include_router(cert_report_router)
api_router.include_router(sim_packs_router)
api_router.include_router(sim_execute_router)
api_router.include_router(cert_simulator_sync_router)
api_router.include_router(cert_push_router)
api_router.include_router(cert_a2a_trigger_router)
api_router.include_router(assignment_actions_router)
api_router.include_router(a2a_logs_router)
api_router.include_router(notifications_router)
api_router.include_router(negotiation_mgmt_router)
api_router.include_router(authority_policy_router)
api_router.include_router(resolver_router)
api_router.include_router(kit_publications_router)
api_router.include_router(eval_router)
api_router.include_router(escalations_router)
api_router.include_router(emergency_issues_router)
api_router.include_router(agentic_router)
api_router.include_router(api_registry_router)
api_router.include_router(governance_router)
api_router.include_router(integration_testing_router)
