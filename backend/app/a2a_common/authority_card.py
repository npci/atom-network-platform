# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""NPCI platform AgentCard.

Lists every A2A task type the platform can RECEIVE from a partner. The
skills mirror `app.models.phase_c.A2ATaskType` so any wire-side change
to that enum has to flip a flag here too — easier to spot regressions
than a hidden router branch.

The partner platform's card (Slice 4, in its own repository) will list a
different but overlapping set of skills (the receiver's perspective). Cert-agent's
card (`certagent/cert-agent/app/a2a/agent_card.py`) stays as-is — it
covers a third, smaller surface (run-certification + query-run-status).
"""
from __future__ import annotations

from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    MutualTlsSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)

from app.core.config import settings
from app.core.domain.registry import prompt_block


def _card_name() -> str:
    """Wire-visible platform identity, from the active pack's `platform_name`
    block ("the NPCI UPI Change Management Platform" — the leading article is
    prompt phrasing, not part of the NAME, so it is stripped here)."""
    name = prompt_block("platform_name", "Change Management Platform").strip()
    return name[4:] if name.lower().startswith("the ") else name


def _card_description() -> str:
    domain = prompt_block("domain_name", "platform")
    actors = prompt_block("ecosystem_actors", "partner organisations")
    return (
        "Receives change communications, partner clarifications, progress "
        "updates, readiness declarations, certification responses and "
        f"defect notices from registered {domain} partners ({actors}, "
        "and the internal cert-engine). Outbound: dispatches change "
        "communications and cert test requests to partners."
    )


# Slice 4 of A2A security hardening — advertise auth requirements via
# the standard A2A `security_schemes` map. Two schemes are published:
#
#   bearer_jwt — HS256 JWT in `Authorization: Bearer <token>`. Issued
#                by NPCI's /a2a/auth handshake; validated by
#                `app.a2a_common.sdk_auth_middleware.SdkAuthMiddleware`.
#   mtls       — pinned client cert at the nginx ingress, with the
#                SHA-256 fingerprint matching `partner_agents.client_cert_fingerprint`.
#                ADVERTISED for discovery; ENFORCED only for partners
#                with `tls_tier == 'mtls'` (Slice 6).
#
# The top-level `security_requirements` lists the BASELINE that every
# caller must meet — bearer_jwt only. mtls is layered on top per partner
# and is therefore not in the base requirement list.
_SECURITY_SCHEMES = {
    "bearer_jwt": SecurityScheme(
        http_auth_security_scheme=HTTPAuthSecurityScheme(
            description=(
                "HS256 JWT obtained via POST /a2a/auth with the partner's "
                "api_key. Token TTL is short; partners must refresh before "
                "expiry. Cached server-side in `a2a_sessions` so admin can "
                "revoke per-partner."
            ),
            scheme="bearer",
            bearer_format="JWT",
        ),
    ),
    "mtls": SecurityScheme(
        mtls_security_scheme=MutualTlsSecurityScheme(
            description=(
                "Mutual TLS with a pinned client certificate. Required only "
                "for bank-tier partners (`tls_tier='mtls'`). The cert's "
                "SHA-256 fingerprint must match the registered value in "
                "`partner_agents.client_cert_fingerprint`."
            ),
        ),
    ),
}

_SECURITY_REQUIREMENTS = [
    SecurityRequirement(schemes={"bearer_jwt": StringList(list=[])}),
]


# Each skill ≡ one A2ATaskType value. Description is the partner-facing
# contract: what payload to send, what to expect back. Tags are for
# discovery / filtering only.
AUTHORITY_AGENT_CARD = AgentCard(
    name=_card_name(),
    description=_card_description(),
    version="1.0.0",
    capabilities=AgentCapabilities(streaming=False),
    default_input_modes=["application/json"],
    default_output_modes=["application/json"],
    security_schemes=_SECURITY_SCHEMES,
    security_requirements=_SECURITY_REQUIREMENTS,
    supported_interfaces=[
        AgentInterface(
            # Full URL — the A2A SDK posts here verbatim, so a bare
            # path won't work (httpx requires the http:// prefix).
            # Sourced from `settings.authority_public_url` which defaults
            # to the docker service name; production overrides via
            # AUTHORITY_PUBLIC_URL env. Mirror of the partner-card fix
            # in commit d554360.
            url=f"{settings.authority_public_url.rstrip('/')}/a2a-rpc/rpc",
            protocol_binding="JSONRPC",
            protocol_version="1.0",
        ),
    ],
    skills=[
        AgentSkill(
            id="change_communication",
            name="Receive Change Communication",
            description=(
                "Outbound from NPCI to partner — registered here so "
                "partners can verify NPCI's role in their card discovery."
            ),
            tags=["change", "outbound"],
        ),
        AgentSkill(
            id="change_acknowledgement",
            name="Change Acknowledgement",
            description=(
                "Partner formally accepts a previously-communicated "
                "change. Data: {change_id, payload}. Side-effect: "
                "ChangePartnerAssignment.status → ACCEPTED."
            ),
            tags=["change", "lifecycle"],
        ),
        AgentSkill(
            id="query",
            name="Implementation Query",
            description=(
                "Partner asks NPCI a question about a change. Data: "
                "{change_id, payload: {text}}. NPCI auto-drafts a "
                "response asynchronously; PO approves before sending."
            ),
            tags=["change", "negotiation"],
        ),
        AgentSkill(
            id="clarification_response",
            name="Clarification Response",
            description=(
                "Outbound from NPCI to partner. Listed for symmetry."
            ),
            tags=["change", "outbound"],
        ),
        AgentSkill(
            id="milestone_update",
            name="Implementation Milestone Update",
            description=(
                "Partner reports an implementation milestone "
                "(milestone: design | coding | testing, state: completed | "
                "in_progress | …). Data: {change_id, payload: {milestone, state, "
                "version_implementing?, notes?, risks?}}. Side-effect: "
                "ChangePartnerAssignment.status auto-derived once a milestone "
                "completes."
            ),
            tags=["change", "lifecycle", "milestone"],
        ),
        AgentSkill(
            id="cert_readiness_declaration",
            name="Readiness Declaration",
            description=(
                "Partner declares ready for certification. Requires all "
                "three ProgressSteps reported. Data: {change_id}. "
                "Side-effect: status → READY_FOR_CERTIFICATION."
            ),
            tags=["change", "lifecycle", "certification"],
        ),
        AgentSkill(
            id="cert_test_request",
            name="Certification Test Request",
            description=(
                "Outbound from NPCI to a cert_engine partner. Listed "
                "for symmetry."
            ),
            tags=["certification", "outbound"],
        ),
        AgentSkill(
            id="cert_test_response",
            name="Certification Test Response",
            description=(
                "Cert engine returns the per-TC results of a cert run. "
                "Data: {payload: {cert_run_id, total, passed, failed, "
                "skipped, results: [{test_case_id, status, ...}]}}. "
                "Side-effect: triggers AI triage on failures."
            ),
            tags=["certification", "results"],
        ),
        AgentSkill(
            id="cert_acknowledgement",
            name="Certification Acknowledgement",
            description=(
                "Partner acknowledges receipt of cert results. "
                "Data: {payload: {cert_run_id}}."
            ),
            tags=["certification"],
        ),
        AgentSkill(
            id="defect_notice",
            name="Defect Notice",
            description=(
                "Partner reports a defect against a cert TC. Data: "
                "{payload: {cert_run_id, test_case_id, description}}."
            ),
            tags=["certification", "defect"],
        ),
        AgentSkill(
            id="defect_resolution",
            name="Defect Resolution",
            description=(
                "Partner reports a defect resolved. Data: "
                "{payload: {cert_run_id, test_case_id, resolution}}."
            ),
            tags=["certification", "defect"],
        ),
        AgentSkill(
            id="round_opened",
            name="Round Opened Notice",
            description=(
                "Outbound from NPCI to partner. Fires whenever a new "
                "negotiation round opens on NPCI side (initial ack, PM "
                "force-advance, silent advance, new version ship). Data: "
                "{change_id, round_number, max_rounds, deadline_at, "
                "kit_version, opened_reason}. Listed for symmetry — the "
                "partner receives and appends to its round_notices log."
            ),
            tags=["change", "negotiation", "outbound"],
        ),
        AgentSkill(
            id="round_closed",
            name="Round Closed Notice",
            description=(
                "Outbound from NPCI to partner. Fires whenever a "
                "negotiation round closes on NPCI side. Data: {change_id, "
                "round_number, closed_at, close_reason: "
                "'pm_forced'|'silent_acceptance'|'superseded_by_version'|"
                "'frozen'}. Next-round details, when there is one, arrive "
                "in the follow-up round_opened notice. Listed for symmetry."
            ),
            tags=["change", "negotiation", "outbound"],
        ),
    ],
)
