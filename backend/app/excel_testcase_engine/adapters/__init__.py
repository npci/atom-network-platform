# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

# INTEGRATION: Adapter layer.
#
# WHY: keeps the engine free of host-project imports. The engine calls
# `from app.excel_testcase_engine.adapters.llm import get_client`, never
# `from app.core.llm import stream_llm`. Re-pointing the engine at a
# different host (or test fakes) means swapping these adapter modules.
