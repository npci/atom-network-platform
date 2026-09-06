# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

# INTEGRATION: network Test-Case Excel Generator engine.
#
# WHY this package exists:
#   The ProductKit "cert_test_cases" doc-type used to be a markdown-only stub
#   that produced ~25 generic test cases. We're replacing the engine behind
#   that doc-type with a LangGraph workflow that produces the Authority-format Excel
#   workbooks with multi-step protocol flows, validation, and the
#   xlsx/markdown/docx companion downloads.
#
# WHY a self-contained package:
#   So the engine can be removed or feature-flagged off without touching the
#   rest of the app. Everything the engine needs from the host project goes
#   through `adapters/` so the engine has zero direct dependency on
#   `app.core.llm` or `app.services.job_registry`. (There is no RAG adapter
#   since the BRD/TSD-only refactor — the BRD and TSD are the only inputs.)
#   Replace the adapters and the engine runs anywhere.
#
# Public surface (called from the host application):
#   register_excel_testcase_engine(app, *, llm, job_registry, db_session_factory)
#       — call once from `main.py` after the API router is mounted.
#   run_workflow_streaming(...)
#       — async generator the WS handler in agents.py uses to drive cert_test_cases.

from app.excel_testcase_engine.injector import register_excel_testcase_engine

__all__ = ["register_excel_testcase_engine"]
