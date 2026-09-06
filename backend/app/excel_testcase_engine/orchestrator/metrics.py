# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tiny JSONL metrics logger for standalone runs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def log_event(path: Path, event: dict[str, Any]) -> None:
    """Append one metrics event to a JSONL file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": datetime.now(timezone.utc).isoformat(), **event}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
