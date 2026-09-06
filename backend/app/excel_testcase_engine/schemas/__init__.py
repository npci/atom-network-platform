# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

# INTEGRATION: schemas package marker.
#
# WHY this file exists: explicit `__init__.py` keeps mypy / pytest /
# editable-install tooling happy. Python treats a folder without one as a
# namespace package, which works at runtime but breaks some packagers. The
# engine's other subpackages all have one — be consistent.
