# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

from __future__ import annotations

from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class DocType(str, Enum):
    CIRCULAR = "Circular"
    BRD = "BRD"
    PRD = "PRD"
    TSD = "TSD"
    PRODUCT_NOTE = "Product Note"
    TECHNICAL_SPEC = "Technical Spec"
    REPORT = "Report"
    PROPOSAL = "Proposal"
    CUSTOM = "Custom"


class DiagramType(str, Enum):
    SEQUENCE = "sequence"
    FLOWCHART = "flowchart"
    ACTIVITY = "activity"


class JobStatus(str, Enum):
    PENDING = "pending"
    RETRIEVING = "retrieving"
    PLANNING = "planning"
    GENERATING_DIAGRAMS = "generating_diagrams"
    WRITING = "writing"
    REVIEWING = "reviewing"
    ASSEMBLING = "assembling"
    COMPLETED = "completed"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Document planning models
# ---------------------------------------------------------------------------

class SectionPlan(BaseModel):
    section_key: Optional[str] = None
    heading: str
    level: int = 1
    render_style: str = "body"
    content_instructions: str = ""
    prompt_instruction: str = ""
    include_table: bool = False
    include_diagram: bool = False
    diagram_type: str = "sequence"
    diagram_description: str = ""
    # When set (e.g. 1 for a one-paragraph intro), overrides default substantive paragraph minimum.
    validation_min_paragraphs: Optional[int] = None
    # Placeholder table shape if include_table is true but the model omits table_data (see docgen.content_fallbacks).
    table_fallback_profile: Optional[str] = None
    # If repair must prefer injecting numbered_items when substantive checks fail (declared in blueprint, not by section_key).
    validation_fill_numbered_items: bool = False


class DocumentPlan(BaseModel):
    title: str
    subtitle: str = ""
    doc_type: str = "BRD"
    include_cover_page: bool = True
    include_toc: bool = True
    document_meta: dict[str, Any] = Field(default_factory=dict)
    sections: list[SectionPlan] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Diagram models
# ---------------------------------------------------------------------------

class DiagramSpec(BaseModel):
    diagram_id: str
    section_index: int
    diagram_type: str
    description: str


# Sequence diagram
class SequenceMessage(BaseModel):
    from_actor: str
    to_actor: str
    label: str
    direction: str = "forward"  # "forward" | "backward" | "self"


class SequenceDiagramSpec(BaseModel):
    actors: list[str] = Field(default_factory=list)
    messages: list[SequenceMessage] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# Flowchart
class FlowNode(BaseModel):
    id: str
    label: str
    node_type: str = "process"  # start | end | process | decision


class FlowEdge(BaseModel):
    from_node: str
    to_node: str
    label: str = ""


class FlowchartSpec(BaseModel):
    nodes: list[FlowNode] = Field(default_factory=list)
    edges: list[FlowEdge] = Field(default_factory=list)


# Activity / swimlane
class ActivityItem(BaseModel):
    id: str
    label: str
    lane: str
    row: int


class ActivityEdge(BaseModel):
    from_id: str
    to_id: str
    label: str = ""


class ActivityDiagramSpec(BaseModel):
    lanes: list[str] = Field(default_factory=list)
    activities: list[ActivityItem] = Field(default_factory=list)
    edges: list[ActivityEdge] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Content generation models
# ---------------------------------------------------------------------------

class TableData(BaseModel):
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)


class GeneratedContent(BaseModel):
    section_key: Optional[str] = None
    section_heading: str
    render_style: str = "body"
    paragraphs: list[str] = Field(default_factory=list)
    bullet_points: list[str] = Field(default_factory=list)
    numbered_items: list[str] = Field(default_factory=list)
    table_data: Optional[TableData] = None
    code_blocks: list[str] = Field(default_factory=list)
    diagram_path: Optional[str] = None
    level: int = 1
    # Stable per-block identity for surgical (patch-based) editing. Sidecar maps
    # each content list + the table to permanent IDs, positionally aligned.
    # Assigned by app.docgen.block_ids; ignored by the docx renderer. Optional so
    # documents generated before IDs existed still validate (they gain IDs on
    # first load through the surgical editor).
    block_ids: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# API request / response models
# ---------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=10)
    doc_type: str = "BRD"
    document_title: Optional[str] = None
    version_number: Optional[str] = None
    classification: Optional[str] = None
    collection_name: Optional[str] = "default"
    reference_template: Optional[str] = None
    use_rag: bool = True
    include_diagrams: bool = True
    audience: Optional[str] = None
    desired_outcome: Optional[str] = None
    format_constraints: Optional[str] = None
    organization_name: Optional[str] = None
    reference_code: Optional[str] = None
    issue_date: Optional[str] = None
    recipient_line: Optional[str] = None
    subject_line: Optional[str] = None
    signatory_name: Optional[str] = None
    signatory_title: Optional[str] = None
    signatory_department: Optional[str] = None
    additional_context: Optional[str] = None
    session_id: Optional[str] = None


class GenerateResponse(BaseModel):
    job_id: str
    status: str
    message: str


class BundleGenerateRequest(BaseModel):
    """Single prompt that spawns BRD + TSD + Product Note + Circular in parallel."""
    prompt: str = Field(..., min_length=10)
    version_number: Optional[str] = None
    classification: Optional[str] = None
    collection_name: Optional[str] = "default"
    use_rag: bool = True
    include_diagrams: bool = True
    audience: Optional[str] = None
    desired_outcome: Optional[str] = None
    organization_name: Optional[str] = None
    reference_code: Optional[str] = None
    issue_date: Optional[str] = None
    recipient_line: Optional[str] = None
    subject_line: Optional[str] = None
    signatory_name: Optional[str] = None
    signatory_title: Optional[str] = None
    signatory_department: Optional[str] = None
    additional_context: Optional[str] = None
    # Optional per-type title overrides
    brd_title: Optional[str] = None
    tsd_title: Optional[str] = None
    product_note_title: Optional[str] = None
    circular_title: Optional[str] = None
    session_id: Optional[str] = None


class BundleJobDetail(BaseModel):
    doc_type: str
    job_id: str
    status: str
    progress: int
    current_step: str
    error: Optional[str] = None
    output_path: Optional[str] = None


class BundleStatusResponse(BaseModel):
    bundle_id: str
    overall_status: str   # pending | running | completed | partial | failed
    jobs: list[BundleJobDetail]


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    progress: int  # 0-100
    current_step: str
    error: Optional[str] = None
    output_path: Optional[str] = None


class RAGUploadResponse(BaseModel):
    filename: str
    collection_name: str
    chunks_added: int
    is_reference: bool
    message: str


class CollectionInfo(BaseModel):
    name: str
    count: int


class HealthResponse(BaseModel):
    status: str
    model_name: str
    ollama_url: str
    active_jobs: int
    total_jobs: int


class EditRequest(BaseModel):
    """Request to edit a specific section of an already-generated document."""
    section_heading: str = Field(..., description="Exact heading of the section to edit.")
    edit_instruction: str = Field(..., min_length=10, description="Natural language description of what to change.")


class EditResponse(BaseModel):
    job_id: str
    section_heading: str
    status: str          # "completed" | "failed"
    output_path: Optional[str] = None
    error: Optional[str] = None


class SessionDocumentInfo(BaseModel):
    job_id: str
    doc_type: str
    document_title: str
    output_path: str
    created_at: str      # ISO timestamp


# ---------------------------------------------------------------------------
# LangGraph state (plain dict — GraphState used for documentation only)
# ---------------------------------------------------------------------------

class GraphState(BaseModel):
    """Pydantic model documenting the shape of the LangGraph state dict."""
    job_id: str
    prompt: str
    doc_type: str = "BRD"
    document_title: Optional[str] = None
    version_number: Optional[str] = None
    classification: Optional[str] = None
    collection_name: Optional[str] = "default"
    reference_structure: Optional[str] = None
    use_rag: bool = True
    include_diagrams: bool = True
    audience: Optional[str] = None
    desired_outcome: Optional[str] = None
    format_constraints: Optional[str] = None
    organization_name: Optional[str] = None
    reference_code: Optional[str] = None
    issue_date: Optional[str] = None
    recipient_line: Optional[str] = None
    subject_line: Optional[str] = None
    signatory_name: Optional[str] = None
    signatory_title: Optional[str] = None
    signatory_department: Optional[str] = None
    additional_context: Optional[str] = None
    session_id: Optional[str] = None
    rag_context: str = ""
    rag_chunks: list[str] = Field(default_factory=list)
    document_plan: Optional[dict[str, Any]] = None
    diagram_specs: list[dict[str, Any]] = Field(default_factory=list)
    generated_diagrams: dict[str, str] = Field(default_factory=dict)
    generated_sections: list[dict[str, Any]] = Field(default_factory=list)
    output_path: Optional[str] = None
    status: str = "pending"
    error: Optional[str] = None
