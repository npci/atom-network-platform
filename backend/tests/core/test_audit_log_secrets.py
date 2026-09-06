# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for scripts/audit_log_secrets.py — the on-disk secret audit tool.

The script exists because `RedactionFilter` was attached to the root LOGGER
instead of to each handler, so records from named loggers reached app.jsonl and
llm_calls.jsonl unscrubbed. These tests pin the two properties that make the
tool trustworthy:

  1. it FINDS planted secrets (a scanner that misses them is worse than none,
     because it certifies a dirty host as clean), and
  2. `--scrub` removes the secret WITHOUT corrupting the file — app.jsonl is
     parsed line-by-line by /api/logs and the Admin viewer, so a scrub that
     breaks JSON trades a leak for an outage.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2]
_SCRIPT = _BACKEND / "scripts" / "audit_log_secrets.py"


def _load_module():
    """Import the script by path — `scripts/` is not an importable package."""
    spec = importlib.util.spec_from_file_location("audit_log_secrets", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["audit_log_secrets"] = mod
    spec.loader.exec_module(mod)
    return mod


audit = _load_module()


SECRETS = {
    "bearer": "ghp_liveTokenAAAABBBBCCCCDDDDEEEE1234",
    "api_key": "dummy_key",
    "url_pw": "hunter2password",
    "nested": "nested_tok_ABCDEFGH1234",
}


def _fixture_lines() -> list[str]:
    return [
        '{"msg": "starting up, nothing sensitive here"}\n',
        '{"msg": "GET /v1/x Authorization: Bearer %s"}\n' % SECRETS["bearer"],
        '{"msg": "config GITLAB_API_KEY=%s"}\n' % SECRETS["api_key"],
        '{"msg": "connect postgres://dbuser:%s@db.internal:5432/app"}\n' % SECRETS["url_pw"],
        '{"msg": "already scrubbed Authorization: [REDACTED] control line"}\n',
        '{"msg": "n", "detail": {"headers": ["Authorization: Bearer %s"]}}\n' % SECRETS["nested"],
    ]


@pytest.fixture()
def dirty_log(tmp_path: Path) -> Path:
    p = tmp_path / "app.jsonl"
    p.write_text("".join(_fixture_lines()), encoding="utf-8")
    return p


class TestScan:
    def test_finds_every_planted_secret_kind(self, dirty_log: Path):
        hits = audit.scan_file(dirty_log, show_secrets=True)
        labels = {label for _, label, _ in hits}
        assert "Bearer token" in labels
        assert "API key / secret / token / password assignment" in labels
        assert "Credentials embedded in a URL" in labels

    def test_reports_the_lines_that_actually_hold_secrets(self, dirty_log: Path):
        # Lines 2, 3, 4 and 6 are dirty; 1 and 5 are clean.
        lines = {ln for ln, _, _ in audit.scan_file(dirty_log, show_secrets=False)}
        assert lines == {2, 3, 4, 6}

    def test_already_redacted_line_is_not_a_finding(self, tmp_path: Path):
        """A line scrubbed at write time still MATCHES the pattern.

        Counting it would make a correctly-handled host look compromised and
        send someone rotating credentials that never leaked.
        """
        p = tmp_path / "app.jsonl"
        p.write_text('{"msg": "Authorization: [REDACTED] control"}\n', encoding="utf-8")
        assert audit.scan_file(p, show_secrets=False) == []

    def test_masking_hides_the_value_by_default(self, dirty_log: Path):
        """The audit output must not itself become a copy of the secret."""
        blob = "\n".join(e for _, _, e in audit.scan_file(dirty_log, show_secrets=False))
        for name, secret in SECRETS.items():
            assert secret not in blob, f"{name} leaked into masked output"

    def test_show_secrets_reveals_them(self, dirty_log: Path):
        blob = "\n".join(e for _, _, e in audit.scan_file(dirty_log, show_secrets=True))
        assert SECRETS["bearer"] in blob

    def test_unreadable_file_does_not_raise(self, tmp_path: Path):
        """An audit that dies on one bad path would skip every later file."""
        assert audit.scan_file(tmp_path / "does_not_exist.jsonl", False) == []


class TestScrub:
    def test_removes_every_secret(self, dirty_log: Path):
        audit.scrub_file(dirty_log, backup=False)
        text = dirty_log.read_text(encoding="utf-8")
        for name, secret in SECRETS.items():
            assert secret not in text, f"{name} survived the scrub"

    def test_keeps_every_line_valid_json(self, dirty_log: Path):
        """The greedy `authorization:.+` pattern eats the closing `"}` when
        applied to a raw line; scrubbing must go through the decoded value."""
        audit.scrub_file(dirty_log, backup=False)
        for i, line in enumerate(dirty_log.read_text(encoding="utf-8").splitlines(), 1):
            try:
                json.loads(line)
            except json.JSONDecodeError as exc:
                pytest.fail(f"line {i} is no longer valid JSON: {exc}\n{line}")

    def test_scrubs_secrets_nested_in_objects_and_arrays(self, dirty_log: Path):
        audit.scrub_file(dirty_log, backup=False)
        last = json.loads(dirty_log.read_text(encoding="utf-8").splitlines()[5])
        assert SECRETS["nested"] not in last["detail"]["headers"][0]
        assert "[REDACTED]" in last["detail"]["headers"][0]

    def test_preserves_clean_lines_byte_for_byte(self, dirty_log: Path):
        original = dirty_log.read_text(encoding="utf-8").splitlines()
        audit.scrub_file(dirty_log, backup=False)
        after = dirty_log.read_text(encoding="utf-8").splitlines()
        assert after[0] == original[0]
        # The already-redacted control line keeps its trailing text: the header
        # pattern must not re-fire on its own "[REDACTED]" output and swallow it.
        assert after[4] == original[4]
        assert "control line" in after[4]

    def test_counts_only_the_lines_it_changed(self, dirty_log: Path):
        assert audit.scrub_file(dirty_log, backup=False) == 4

    def test_is_idempotent(self, dirty_log: Path):
        audit.scrub_file(dirty_log, backup=False)
        first = dirty_log.read_text(encoding="utf-8")
        assert audit.scrub_file(dirty_log, backup=False) == 0
        assert dirty_log.read_text(encoding="utf-8") == first

    def test_scrubbed_file_rescans_clean(self, dirty_log: Path):
        audit.scrub_file(dirty_log, backup=False)
        assert audit.scan_file(dirty_log, show_secrets=False) == []

    def test_backup_keeps_the_original_for_the_rotation_audit(self, dirty_log: Path):
        audit.scrub_file(dirty_log, backup=True)
        bak = Path(str(dirty_log) + ".bak")
        assert bak.is_file()
        assert SECRETS["bearer"] in bak.read_text(encoding="utf-8")

    def test_no_backup_leaves_no_copy(self, dirty_log: Path):
        audit.scrub_file(dirty_log, backup=False)
        assert not Path(str(dirty_log) + ".bak").exists()

    def test_leaves_no_temp_file_behind(self, dirty_log: Path):
        audit.scrub_file(dirty_log, backup=False)
        leftovers = [p.name for p in dirty_log.parent.iterdir() if ".scrub" in p.name]
        assert leftovers == []

    def test_handles_plain_text_logs(self, tmp_path: Path):
        """Not every affected line is JSON; the text path must still scrub."""
        p = tmp_path / "llm_calls.jsonl"
        p.write_text(f"2026-01-01 INFO call with Bearer {SECRETS['bearer']}\n", encoding="utf-8")
        audit.scrub_file(p, backup=False)
        text = p.read_text(encoding="utf-8")
        assert SECRETS["bearer"] not in text
        assert "[REDACTED]" in text


class TestFileDiscovery:
    def test_includes_rotated_siblings(self, tmp_path: Path):
        """app.jsonl.1 holds the same unredacted history as app.jsonl."""
        for name in ("app.jsonl", "app.jsonl.1", "app.jsonl.2", "llm_calls.jsonl"):
            (tmp_path / name).write_text("{}\n", encoding="utf-8")
        (tmp_path / "unrelated.txt").write_text("x\n", encoding="utf-8")

        names = {p.name for p in audit._candidate_files([tmp_path], [])}
        assert {"app.jsonl", "app.jsonl.1", "app.jsonl.2", "llm_calls.jsonl"} <= names
        assert "unrelated.txt" not in names

    def test_deduplicates_when_a_root_is_also_passed_explicitly(self, tmp_path: Path):
        (tmp_path / "app.jsonl").write_text("{}\n", encoding="utf-8")
        files = audit._candidate_files([tmp_path], [tmp_path])
        assert len(files) == len({str(f.resolve()) for f in files})


class TestPatternLabels:
    def test_every_runtime_pattern_has_a_human_label(self):
        """A new pattern in log_buffer without a label here would print
        'unknown-secret', which tells an operator nothing about what to rotate."""
        from app.core.log_buffer import _LOG_REDACTIONS

        assert len(audit._PATTERN_LABELS) == len(_LOG_REDACTIONS)
        for i in range(len(_LOG_REDACTIONS)):
            assert audit._label_for(i) != "unknown-secret"

    def test_unknown_index_degrades_instead_of_raising(self):
        assert audit._label_for(999) == "unknown-secret"
