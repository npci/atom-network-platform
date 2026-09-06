# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

from datetime import datetime
from pydantic import BaseModel, field_validator
from app.models.change_request import ChangeStatus
from app.schemas.user import UserResponse


class ChangeRequestCreate(BaseModel):
    # NO `title` — it is AI-generated from the idea / attached document, never typed.
    # A user-typed title outlives every later decision (BRD heading, MR slug, cert
    # feature_name) and is never revised when clarification supersedes a value it
    # embeds — that is exactly how "as 80" survived ratification in the BT/80
    # incident. An old client that still posts one is not an error: pydantic ignores
    # the extra field, so the generated title wins either way.
    initial_prompt: str

    @field_validator("initial_prompt")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        # "Idea or document" is enforced by the UI (the document flow derives a prompt
        # from the attachment name); the backend invariant is simply: never a blank
        # prompt — Phase A would otherwise run on nothing.
        if not (v or "").strip():
            raise ValueError("initial_prompt must be non-empty (with a document-only "
                             "change, derive it from the attachment, as the UI does)")
        return v


class ChangeRequestUpdate(BaseModel):
    title: str | None = None
    enhanced_prompt: str | None = None
    status: ChangeStatus | None = None


class PhaseSummary(BaseModel):
    """Compact per-phase status, suitable for dashboard chips.

    state ∈ {'not_started','in_progress','completed','blocked'}.
    label is the human-readable primary text; detail is optional supplementary text.
    """
    state: str
    label: str
    detail: str | None = None


class ChangeRequestResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    title: str | None
    initial_prompt: str
    enhanced_prompt: str | None
    status: ChangeStatus
    created_by: str
    created_at: datetime
    updated_at: datetime | None
    # Drives the agentic Phase-A/B panels on the XSD + Phase-B pages — without this
    # field pydantic silently DROPS the DB column and the UI can never enable agentic.
    agentic_enabled: bool = False
    # Accuracy S5 — drives the UI stage order (v2 = XSD before Tech Spec). Without
    # this field pydantic drops the DB column and the UI always renders the v1 order.
    workflow_version: int = 1
    # Source document (detailed BRD) attached at creation — name only, so the UI can show
    # the attachment; the extracted text stays server-side (it is prompt material).
    source_doc_name: str | None = None
    # Optional phase summaries — populated by list endpoint; None elsewhere.
    phase_a: PhaseSummary | None = None
    phase_b: PhaseSummary | None = None
    phase_c: PhaseSummary | None = None


class ChangeRequestDetailResponse(ChangeRequestResponse):
    creator: UserResponse


class ChangeRequestListResponse(BaseModel):
    total: int
    items: list[ChangeRequestResponse]
