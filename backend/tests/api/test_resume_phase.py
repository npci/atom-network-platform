# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""_resumable_phase (manual Resume): which phase a stalled/failed run resumes FROM.

The regression this guards: a run that died during its FIRST transition (the
pending->workspace_ready clone failed before recording any phase) used to return
None -> "no resumable phase found for this run", dead-ending Retry. It must now fall
back to `pending` so the run re-drives from the start (clone is idempotent, no edits yet)."""
from types import SimpleNamespace

from app.api.agentic import _resumable_phase


class _Q:
    def __init__(self, rows): self._rows = rows
    def filter(self, *a, **k): return self
    def order_by(self, *a, **k): return self
    def all(self): return self._rows


class _DB:
    def __init__(self, rows): self._rows = rows
    def query(self, *a, **k): return _Q(self._rows)


def test_non_terminal_run_resumes_from_its_own_phase():
    run = SimpleNamespace(phase="code_change", id="r1")
    assert _resumable_phase(_DB([]), run) == "code_change"


def test_terminal_run_resumes_from_last_non_terminal_checkpoint():
    # newest-first history: failed was preceded by context_ready.
    rows = [SimpleNamespace(payload={"to": "failed"}),
            SimpleNamespace(payload={"to": "context_ready"})]
    run = SimpleNamespace(phase="failed", id="r2")
    assert _resumable_phase(_DB(rows), run) == "context_ready"


def test_early_failure_with_no_checkpoint_falls_back_to_pending():
    # Died at the very first transition (clone failed) -> no non-terminal phase recorded.
    # Must re-drive from the start instead of returning None ("no resumable phase").
    run = SimpleNamespace(phase="failed", id="r3")
    assert _resumable_phase(_DB([]), run) == "pending"
