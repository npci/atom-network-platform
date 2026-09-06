# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

# INTEGRATION: prompts package marker.
#
# WHY this file exists: same hygiene reason as `schemas/__init__.py`.
# Nothing imports from here — prompts are read as raw markdown via
# `Path(...).read_text()` — but having an explicit __init__.py keeps the
# folder a proper package for tooling that checks for it (e.g. setuptools
# data discovery, mypy strict mode).
