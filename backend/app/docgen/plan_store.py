# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Utilities for persisting planning and validation artifacts.

Adapted from docgen: writes under the platform's ARTIFACTS_DIR/docgen/{job_id}/ so all
generated artifacts live alongside the platform's other per-change artifacts.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.docgen.config import settings


def artifact_dir(job_id: str) -> Path:
    path = Path(settings.output_dir) / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_json_artifact(job_id: str, name: str, payload: Any) -> str:
    path = artifact_dir(job_id) / name
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)
