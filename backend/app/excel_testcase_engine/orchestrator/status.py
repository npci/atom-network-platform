# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Job status models for CLI and API progress reporting."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    """Pipeline states exposed to callers."""

    PENDING = "pending"
    ENHANCING = "enhancing"
    NEEDS_INPUT = "needs_input"
    PLANNING = "planning"
    WRITING = "writing"
    RENDERING = "rendering"
    VALIDATING = "validating"
    REPAIRING = "repairing"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"


class JobProgress(BaseModel):
    """Progress event for a generation job."""

    job_id: str
    status: JobStatus
    current: int = 0
    total: int = 0
    message: str = ""
    open_questions: list[str] = Field(default_factory=list)
