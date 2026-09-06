# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase B SQLAlchemy models — Design to Build."""
import enum
from datetime import datetime
from sqlalchemy import String, Text, Integer, Boolean, Enum, ForeignKey, JSON, DateTime
from sqlalchemy.orm import mapped_column, Mapped, relationship
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid, utcnow


# ── Enums ──────────────────────────────────────────────────────────────────────

class PhaseBRunStatus(str, enum.Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED   = "completed"
    BLOCKED     = "blocked"


class PhaseBStep(str, enum.Enum):
    CODE_CHANGE  = "code_change"
    CODE_REVIEW  = "code_review"
    IS_REVIEW    = "is_review"
    GIT          = "git"
    BUILD        = "build"
    DEPLOY       = "deploy"
    TEST_GEN     = "test_gen"
    TEST_EXEC    = "test_exec"
    TRIAGE       = "triage"
    COMPLETED    = "completed"


class IterationTrigger(str, enum.Enum):
    INITIAL             = "initial"
    USER_FEEDBACK       = "user_feedback"
    CODE_REVIEW_FEEDBACK = "code_review_feedback"
    IS_REVIEW_FEEDBACK  = "is_review_feedback"
    BUILD_FAILURE       = "build_failure"
    DEPLOY_FAILURE      = "deploy_failure"
    UAT_FAILURE         = "uat_failure"


class ReviewStatus(str, enum.Enum):
    CLEAN        = "clean"
    ISSUES_FOUND = "issues_found"


class GitEventStatus(str, enum.Enum):
    BRANCH_CREATED = "branch_created"
    COMMITTED      = "committed"
    MR_RAISED      = "mr_raised"
    MERGED         = "merged"


class BuildRunStatus(str, enum.Enum):
    QUEUED  = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"


class DeployRunStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"


class TestCaseCategory(str, enum.Enum):
    NEW_FEATURE = "new_feature"
    REGRESSION  = "regression"


class TestRunStatus(str, enum.Enum):
    RUNNING   = "running"
    COMPLETED = "completed"


class TestResultStatus(str, enum.Enum):
    PASS  = "pass"
    FAIL  = "fail"
    SKIP  = "skip"
    ERROR = "error"


class TriageVerdict(str, enum.Enum):
    CODE_BUG       = "code_bug"
    TEST_CASE_ISSUE = "test_case_issue"
    ENV_ISSUE      = "env_issue"


class CodePlanStatus(str, enum.Enum):
    """Slice 12 — HITL gate #3 status for a structured code plan."""
    DRAFT    = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"


def _enum(e):
    return Enum(e, values_callable=lambda x: [v.value for v in x])


# ── Models ─────────────────────────────────────────────────────────────────────

class PhaseBRun(Base):
    __tablename__ = "phase_b_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(String(36), ForeignKey("change_requests.id"), nullable=False)
    status: Mapped[PhaseBRunStatus] = mapped_column(_enum(PhaseBRunStatus), nullable=False, default=PhaseBRunStatus.IN_PROGRESS)
    current_step: Mapped[PhaseBStep] = mapped_column(_enum(PhaseBStep), nullable=False, default=PhaseBStep.CODE_CHANGE)
    iteration_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gitlab_repo: Mapped[str | None] = mapped_column(String(500), nullable=True)
    gitlab_branch: Mapped[str | None] = mapped_column(String(200), nullable=True, default="main")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    code_iterations: Mapped[list["CodeIteration"]] = relationship("CodeIteration", back_populates="run", cascade="all, delete-orphan", order_by="CodeIteration.iteration_number")
    git_events: Mapped[list["GitEvent"]] = relationship("GitEvent", back_populates="run", cascade="all, delete-orphan")
    build_runs: Mapped[list["BuildRun"]] = relationship("BuildRun", back_populates="run", cascade="all, delete-orphan")
    deployment_runs: Mapped[list["DeploymentRun"]] = relationship("DeploymentRun", back_populates="run", cascade="all, delete-orphan")
    uat_test_cases: Mapped[list["UATTestCase"]] = relationship("UATTestCase", back_populates="run", cascade="all, delete-orphan")
    uat_test_runs: Mapped[list["UATTestRun"]] = relationship("UATTestRun", back_populates="run", cascade="all, delete-orphan")


class CodeIteration(Base):
    __tablename__ = "code_iterations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    phase_b_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("phase_b_runs.id"), nullable=False)
    iteration_number: Mapped[int] = mapped_column(Integer, nullable=False)
    generated_output: Mapped[str | None] = mapped_column(Text, nullable=True)   # full agent response
    files_changed: Mapped[list | None] = mapped_column(JSON, nullable=True)      # [{path, content}]
    user_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger: Mapped[IterationTrigger] = mapped_column(_enum(IterationTrigger), nullable=False, default=IterationTrigger.INITIAL)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    run: Mapped["PhaseBRun"] = relationship("PhaseBRun", back_populates="code_iterations")
    code_review: Mapped["CodeReviewResult | None"] = relationship("CodeReviewResult", back_populates="iteration", uselist=False, cascade="all, delete-orphan")
    is_review: Mapped["ISReviewResult | None"] = relationship("ISReviewResult", back_populates="iteration", uselist=False, cascade="all, delete-orphan")


class CodeReviewResult(Base):
    __tablename__ = "code_review_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    code_iteration_id: Mapped[str] = mapped_column(String(36), ForeignKey("code_iterations.id"), nullable=False)
    status: Mapped[ReviewStatus] = mapped_column(_enum(ReviewStatus), nullable=False)
    issues: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    iteration: Mapped["CodeIteration"] = relationship("CodeIteration", back_populates="code_review")


class ISReviewResult(Base):
    __tablename__ = "is_review_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    code_iteration_id: Mapped[str] = mapped_column(String(36), ForeignKey("code_iterations.id"), nullable=False)
    status: Mapped[ReviewStatus] = mapped_column(_enum(ReviewStatus), nullable=False)
    findings: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    iteration: Mapped["CodeIteration"] = relationship("CodeIteration", back_populates="is_review")


class GitEvent(Base):
    __tablename__ = "git_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    phase_b_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("phase_b_runs.id"), nullable=False)
    branch_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String(100), nullable=True)
    mr_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    mr_iid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[GitEventStatus] = mapped_column(_enum(GitEventStatus), nullable=False, default=GitEventStatus.BRANCH_CREATED)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    run: Mapped["PhaseBRun"] = relationship("PhaseBRun", back_populates="git_events")


class BuildRun(Base):
    __tablename__ = "build_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    phase_b_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("phase_b_runs.id"), nullable=False)
    iteration_number: Mapped[int] = mapped_column(Integer, nullable=False)
    jenkins_build_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    jenkins_job_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[BuildRunStatus] = mapped_column(_enum(BuildRunStatus), nullable=False, default=BuildRunStatus.QUEUED)
    build_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Unified build+deploy fields (alembic 0030).
    core_branch: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Column name predates the rename and is retained for schema stability —
    # renaming a live column is a data migration, not a refactor.
    app_branch: Mapped[str | None] = mapped_column("upi2_branch", String(200), nullable=True)
    host: Mapped[str | None] = mapped_column(String(200), nullable=True)
    deploy_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    startup_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    deployed_artifacts: Mapped[list | None] = mapped_column(JSON, nullable=True)
    services_started: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Request-supplied build+deploy script (alembic 0137) — the resolved path
    # under PHASE_B_SCRIPT_ROOT that this run executed; NULL = the fixed
    # PHASE_B_BUILD_SCRIPT (or a mode that runs no script).
    script_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    run: Mapped["PhaseBRun"] = relationship("PhaseBRun", back_populates="build_runs")


class DeploymentRun(Base):
    __tablename__ = "deployment_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    phase_b_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("phase_b_runs.id"), nullable=False)
    build_run_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("build_runs.id"), nullable=True)
    iteration_number: Mapped[int] = mapped_column(Integer, nullable=False)
    target_server: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[DeployRunStatus] = mapped_column(_enum(DeployRunStatus), nullable=False, default=DeployRunStatus.RUNNING)
    deploy_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    health_check_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    health_check_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped["PhaseBRun"] = relationship("PhaseBRun", back_populates="deployment_runs")


class UATTestCase(Base):
    __tablename__ = "uat_test_cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    phase_b_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("phase_b_runs.id"), nullable=False)
    suite_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    test_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    category: Mapped[TestCaseCategory] = mapped_column(_enum(TestCaseCategory), nullable=False, default=TestCaseCategory.NEW_FEATURE)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    preconditions: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_method: Mapped[str | None] = mapped_column(String(10), nullable=True)
    endpoint: Mapped[str | None] = mapped_column(String(500), nullable=True)
    request_headers: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    request_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    expected_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    pass_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    run: Mapped["PhaseBRun"] = relationship("PhaseBRun", back_populates="uat_test_cases")
    results: Mapped[list["UATTestResult"]] = relationship("UATTestResult", back_populates="test_case", cascade="all, delete-orphan")


class UATTestRun(Base):
    __tablename__ = "uat_test_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    phase_b_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("phase_b_runs.id"), nullable=False)
    suite_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    iteration_number: Mapped[int] = mapped_column(Integer, nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    passed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    skipped: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[TestRunStatus] = mapped_column(_enum(TestRunStatus), nullable=False, default=TestRunStatus.RUNNING)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Script-based UAT (alembic 0137): the combined gen+exec step runs one
    # operator test script; `script_path` is the resolved path under
    # PHASE_B_SCRIPT_ROOT and `log` its full captured output (the artefact
    # the UI shows live and AI triage reads). NULL on legacy mock rows.
    script_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    log: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped["PhaseBRun"] = relationship("PhaseBRun", back_populates="uat_test_runs")
    test_results: Mapped[list["UATTestResult"]] = relationship("UATTestResult", back_populates="test_run", cascade="all, delete-orphan")
    triage_results: Mapped[list["UATTriageResult"]] = relationship("UATTriageResult", back_populates="test_run", cascade="all, delete-orphan")


class UATTestResult(Base):
    __tablename__ = "uat_test_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    test_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("uat_test_runs.id"), nullable=False)
    test_case_id: Mapped[str] = mapped_column(String(36), ForeignKey("uat_test_cases.id"), nullable=False)
    status: Mapped[TestResultStatus] = mapped_column(_enum(TestResultStatus), nullable=False)
    actual_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    test_run: Mapped["UATTestRun"] = relationship("UATTestRun", back_populates="test_results")
    test_case: Mapped["UATTestCase"] = relationship("UATTestCase", back_populates="results")
    triage: Mapped["UATTriageResult | None"] = relationship("UATTriageResult", back_populates="test_result", uselist=False, cascade="all, delete-orphan")


class UATTriageResult(Base):
    __tablename__ = "uat_triage_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    test_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("uat_test_runs.id"), nullable=False)
    test_result_id: Mapped[str] = mapped_column(String(36), ForeignKey("uat_test_results.id"), nullable=False)
    verdict: Mapped[TriageVerdict] = mapped_column(_enum(TriageVerdict), nullable=False)
    ai_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_override: Mapped[TriageVerdict | None] = mapped_column(_enum(TriageVerdict), nullable=True)
    final_verdict: Mapped[TriageVerdict | None] = mapped_column(_enum(TriageVerdict), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    test_run: Mapped["UATTestRun"] = relationship("UATTestRun", back_populates="triage_results")
    test_result: Mapped["UATTestResult"] = relationship("UATTestResult", back_populates="triage")


class PhaseBTriageReport(Base):
    """AI triage over the run's BUILD + UAT logs (alembic 0137).

    One row per triage invocation (latest wins in the UI). `report` is the
    structured verdict set the uat_triage agent produced from the logs;
    `walkthrough` is the plain-language developer + tester walkthrough shown
    beside it (reused from the agentic run when one exists, else generated).
    Log-derived, so it exists even when the script-based UAT step produced no
    per-case rows.
    """
    __tablename__ = "phase_b_triage_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    phase_b_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("phase_b_runs.id"), nullable=False)
    build_run_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("build_runs.id"), nullable=True)
    uat_test_run_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("uat_test_runs.id"), nullable=True)
    report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    walkthrough: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class CodePlan(Base):
    """Slice 12 — Structured code-change plan produced by the code_planner agent.

    Shape of `plan_data` (validated by `code_plan_schema.validate`):
      {
        "files": [
          {"path": "...", "action": "create" | "modify",
           "intent": "...", "repo": "...", "signatures_to_add": [...]}
        ],
        "tests": [
          {"path": "...", "action": "create", "cases": [...]}
        ],
        "notes": "optional free-text"
      }

    Currently written by the new planner but not yet consumed by the
    existing `code_change.py` streaming agent — HITL gate #3 wiring is a
    follow-up slice.
    """
    __tablename__ = "code_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(String(36), ForeignKey("change_requests.id"), nullable=False)
    phase_b_run_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("phase_b_runs.id"), nullable=True)
    status: Mapped[CodePlanStatus] = mapped_column(_enum(CodePlanStatus), nullable=False, default=CodePlanStatus.DRAFT)
    plan_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    reviewer_comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


# ── Multi-repo support (slice M-1, alembic 0026) ──────────────────────────────


class PhaseBRunRepo(Base):
    """Per-Phase-B-run record of which CodeRepo participates in this run.

    A Phase B run can span multiple repos (e.g., Core library + the network
    Switch application). Each repo gets its own row so we can:
      - track per-repo branch (typically the same branch name across repos
        for one run, but per-repo override is allowed)
      - track per-repo MR url/iid/state independently
      - route generated files to the correct repo by the LLM-emitted
        `[repo-label]` prefix on `<<FILE: ...>>` markers

    The legacy `PhaseBRun.gitlab_repo` and `gitlab_branch` columns continue
    to hold the "primary" repo info for backward-compatible single-repo
    display — multi-repo runs populate them with the FIRST repo so older
    UI states keep showing something meaningful.
    """

    __tablename__ = "phase_b_run_repos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("phase_b_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    repo_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("code_repos.id"),
        nullable=False,
    )
    # Per-run branch name. Defaults to a per-CR branch like
    # "change-<short>/iter-<n>" so all repos in the run share one name —
    # makes it easy for reviewers to find the linked MRs across repos.
    branch: Mapped[str] = mapped_column(String(200), nullable=False)
    # Populated after push_to_gitlab succeeds for this repo.
    mr_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    mr_iid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Free-form for now; tracks GitLab MR state ("opened" | "merged" | "closed").
    mr_state: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # sha256 of the files payload pushed for this repo (0114) — the Phase-B
    # analogue of the agentic pushed_manifest_hash. NULL = legacy row (unknown),
    # treated as pushed so historical runs are never surprise-re-pushed.
    pushed_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False,
    )
