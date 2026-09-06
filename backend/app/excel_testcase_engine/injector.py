# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

# INTEGRATION: Single-call wiring point.
#
# WHY a single registration function:
#   - One line in `main.py` activates the engine. Easy to comment out for an
#     emergency rollback without leaving orphan imports anywhere.
#   - Validates host dependencies up front. If something the engine needs
#     is missing, we fail fast at startup with a clear error rather than
#     blowing up mid-workflow under user load.
#   - Keeps the host's import surface narrow: `app.excel_testcase_engine`
#     exposes ONLY this function as its public API.

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from fastapi import FastAPI
from sqlalchemy.orm import Session

logger = logging.getLogger("excel_engine.injector")


def register_excel_testcase_engine(
    app: FastAPI,
    *,
    llm: dict[str, Callable],
    job_registry,
    db_session_factory: Callable[[], Session],
    artifacts_dir: Path | None = None,
    outputs_dir: Path | None = None,
) -> None:
    """Wire the engine into the host FastAPI app.

    Args:
        app:                 the host's FastAPI instance.
        llm:                 dict with keys `stream` and `call`, holding
                             `app.core.llm.stream_llm` and `app.core.llm.call_llm`.
        job_registry:        `app.services.job_registry` module.
        db_session_factory:  `app.core.database.SessionLocal` (callable).
        artifacts_dir:       where stage artifacts go (defaults under
                             `artifacts/excel_engine/`). Host startup should
                             pass a mounted volume path.
        outputs_dir:         where xlsx/md/docx files land (defaults under
                             `artifacts/excel_engine/`).
    """

    # Validate host dependencies. WHY upfront: failing at engine module
    # import time is much harder to debug than failing here with a clear
    # message during app boot.
    if not callable(llm.get("stream")) or not callable(llm.get("call")):
        raise RuntimeError(
            "register_excel_testcase_engine: llm dict must contain callable "
            "`stream` and `call` (typically app.core.llm.stream_llm / call_llm).",
        )
    for name in ("create_job", "update_job", "complete_job", "fail_job", "get_job"):
        if not hasattr(job_registry, name):
            raise RuntimeError(f"register_excel_testcase_engine: job_registry missing `{name}`")
    if not callable(db_session_factory):
        raise RuntimeError("register_excel_testcase_engine: db_session_factory must be callable")

    # Bind the adapters. The engine's agents ask their own factory for
    # clients; the factory now routes through these.
    #
    # No RAG adapter: the BRD/TSD-only refactor (3b95b6df) deleted
    # `adapters/rag.py` along with the retrieval layer — the engine's inputs are
    # the BRD and TSD, nothing retrieved. This module kept importing and
    # configuring it, which raised ImportError inside registration; `main.py`
    # turns that into a RuntimeError, so the app would not boot at all with
    # `excel_engine_enabled=true`.
    from app.excel_testcase_engine.adapters import llm as llm_adapter
    from app.excel_testcase_engine.adapters import jobs as jobs_adapter

    llm_adapter.configure(stream_fn=llm["stream"], call_fn=llm["call"])
    jobs_adapter.configure(
        create_job=job_registry.create_job,
        update_job=job_registry.update_job,
        complete_job=job_registry.complete_job,
        fail_job=job_registry.fail_job,
        get_job=job_registry.get_job,
        session_factory=db_session_factory,
    )

    # Bind workspace paths. WHY default under artifacts/: Docker persists
    # /app/artifacts, but not /app/outputs. Keeping the fallback here makes
    # direct/local registration safer; main.py still passes settings.artifacts_dir
    # explicitly so production writes to the mounted volume.
    fallback_root = Path("artifacts") / "excel_engine"
    artifacts_root = artifacts_dir or (fallback_root / "artifacts")
    outputs_root = outputs_dir or (fallback_root / "workbooks")
    from app.excel_testcase_engine.orchestrator.graph import configure_paths
    configure_paths(artifacts_dir=artifacts_root, outputs_dir=outputs_root)

    # Register the XLSX download endpoint. Markdown / DOCX are already served
    # by the host's existing artifact endpoint; only XLSX is genuinely new.
    from app.excel_testcase_engine.api import router as engine_router
    app.include_router(engine_router, prefix="/api")

    logger.info(
        "excel_testcase_engine registered: artifacts=%s outputs=%s",
        artifacts_root, outputs_root,
    )


__all__ = ["register_excel_testcase_engine"]
