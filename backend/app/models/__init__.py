# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

# Import all models so Alembic can detect them for migrations
from app.models.user import User, UserRole
from app.models.change_request import ChangeRequest, ChangeStatus
from app.models.conversation import Conversation, ConversationModule, MessageRole
from app.models.research import ResearchOutput, ArtifactStatus
from app.models.canvas import ProductCanvas
from app.models.brd import BRD, BRDStatus
from app.models.approval import Approval, ApprovalArtifactType, ApprovalStatus
from app.models.tech_spec import TechSpec
from app.models.decline_spec import DeclineSpec
from app.models.xsd import XSD, XSDStatus
from app.models.product_kit import ProductKitDocument, ProductKitDocType
from app.models.kit_publication import KitPublication
from app.models.notification import Notification, NotificationType
from app.models.document_chunk import DocumentChunk, DocCategory
from app.models.feedback import Feedback
from app.models.change_request_context import ChangeRequestContext
from app.models.clarification import Clarification
from app.models.agent_job import AgentJob, AgentJobStatus
from app.models.eval_verdict import EvalVerdict
from app.models.eval_policy_audit import EvalPolicyAudit
from app.models.agentic import (
    AgenticRun, AgenticRunRepo, AgenticEvent, ChangeManifest,
    VerificationRun, ReviewFinding, ChangeReport,
    AgenticPhase, AgenticStatus, VerifyDecision, ReviewSeverity, ReviewCategory,
)
from app.models.module_context import ModuleContext, RepoPathContext
from app.models.flow_context import FlowContext
from app.models.xsd_graph import (
    XsdSchemaNode, XsdSchemaEdge, XsdJavaLink, XsdEdgeType, XsdLinkSource,
)
from app.models.escalation_ticket import EscalationTicket
from app.models.emergency_issue import EmergencyIssue
from app.models.kit_revision_plan import KitRevisionPlan
from app.models.change_analysis import (
    ChangeAnalysis,
    DecisionLedgerEntry,
    ChangeImpactedPath,
)
from app.models.document_reconciliation import DocumentReconciliation
from app.models.api_registry import ApiMessage, ApiField
from app.models.artifact_cold_storage import ArtifactColdStorage
from app.models.integration_exchange import IntegrationExchange
from app.models.sim_pack import SimPackPublication, SimPackRecord

__all__ = [
    "ApiMessage", "ApiField",
    "SimPackRecord", "SimPackPublication",
    "ChangeAnalysis", "DecisionLedgerEntry", "ChangeImpactedPath",
    "DocumentReconciliation",
    "User", "UserRole",
    "ChangeRequest", "ChangeStatus",
    "Conversation", "ConversationModule", "MessageRole",
    "ResearchOutput", "ArtifactStatus",
    "ProductCanvas",
    "BRD", "BRDStatus",
    "Approval", "ApprovalArtifactType", "ApprovalStatus",
    "TechSpec",
    "DeclineSpec",
    "XSD", "XSDStatus",
    "ProductKitDocument", "ProductKitDocType",
    "KitPublication",
    "Notification", "NotificationType",
    "DocumentChunk", "DocCategory",
    "Feedback",
    "ChangeRequestContext",
    "Clarification",
    "AgentJob", "AgentJobStatus",
    "EvalVerdict",
    "EvalPolicyAudit",
    "AgenticRun", "AgenticRunRepo", "AgenticEvent", "ChangeManifest",
    "VerificationRun", "ReviewFinding", "ChangeReport",
    "AgenticPhase", "AgenticStatus", "VerifyDecision", "ReviewSeverity", "ReviewCategory",
    "ModuleContext", "RepoPathContext",
    "FlowContext",
    "XsdSchemaNode", "XsdSchemaEdge", "XsdJavaLink", "XsdEdgeType", "XsdLinkSource",
    "EscalationTicket",
    "EmergencyIssue",
    "KitRevisionPlan",
    "ArtifactColdStorage",
]
