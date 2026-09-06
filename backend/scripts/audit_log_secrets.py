# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Find (and optionally scrub) secrets left in log files on disk.

WHY THIS EXISTS
---------------
`RedactionFilter` was attached to the ROOT LOGGER rather than to each handler.
Python only runs a logger's own filters plus each HANDLER's filters, never an
ancestor logger's — so records from `logging.getLogger(__name__)` (i.e. almost
every line this platform emits) reached their sinks UNSCRUBBED. Two sinks were
affected for as long as that wiring was live:

    app.jsonl        the /api/logs + Admin-viewer store   (log_buffer.py)
    llm_calls.jsonl  one JSON row per LLM call            (observability.py)

The diagnostics files (codegen.log, commands.log, build/*.log, verify/*.log)
were NOT affected: `diag.py` already attached the filter per handler.

The wiring is fixed going forward, but files written before the fix can still
contain live credentials. This script finds them so you know what to rotate.

USAGE
-----
    cd backend
    python scripts/audit_log_secrets.py                     # scan default dirs
    python scripts/audit_log_secrets.py --path /var/log/x   # scan somewhere else
    python scripts/audit_log_secrets.py --show-secrets      # reveal values
    python scripts/audit_log_secrets.py --scrub             # rewrite in place

SAFETY
------
Read-only by default: it reports, it does not modify. Findings are MASKED
unless you pass --show-secrets, so the audit output is not itself a new leak.
`--scrub` rewrites files in place via a temp file + atomic replace, keeping a
`.bak` copy unless you pass --no-backup.

Exit codes:  0 = clean   1 = secrets found   2 = usage/IO error
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Reuse the SAME patterns the runtime filter uses. Importing rather than
# re-declaring them means this audit can never drift out of sync with what the
# application actually considers a secret.
from app.core.log_buffer import _LOG_REDACTIONS  # noqa: E402


# Files the buggy wiring could have written unredacted. Diagnostics files are
# deliberately excluded — diag.py always filtered per handler.
_AFFECTED_NAMES = ("app.jsonl", "llm_calls.jsonl")

# Human labels for each entry of `_LOG_REDACTIONS`, in declaration order. The
# replacement templates are regex artefacts (`\1`, `bearer [REDACTED]`), so
# deriving a name from them yields output an operator cannot act on. Indexing a
# parallel list keeps the patterns themselves as the single source of truth
# while still naming WHAT to rotate. Any pattern added without a label here
# degrades to "unknown-secret" rather than silently mislabelling.
_PATTERN_LABELS = (
    "Bearer token",
    "Authorization / Private-Token header",
    "API key / secret / token / password assignment",
    "Credentials embedded in a URL",
)


def _label_for(index: int) -> str:
    try:
        return _PATTERN_LABELS[index]
    except IndexError:
        return "unknown-secret"


def _default_roots() -> list[Path]:
    """Where the two affected sinks land, mirroring the runtime resolution."""
    roots: list[Path] = []

    # app.jsonl — log_buffer._LOG_DIR (env LOG_DIR, else /tmp/cm-platform-logs)
    roots.append(Path(os.environ.get("LOG_DIR", "/tmp/cm-platform-logs")))

    # llm_calls.jsonl — settings.llm_call_log_path, else the diagnostics dir.
    try:
        from app.core.config import settings
        if getattr(settings, "llm_call_log_path", ""):
            roots.append(Path(settings.llm_call_log_path).parent)
        if getattr(settings, "diag_log_dir", ""):
            roots.append(Path(settings.diag_log_dir))
    except Exception:  # noqa: BLE001 — config must never break an audit
        pass

    # The diagnostics candidates from diag.py, in the same order.
    roots.append(_BACKEND / "logs" / "diagnostics")
    roots.append(Path.home() / ".cm-diag")
    roots.append(Path(tempfile.gettempdir()) / "cm-diag")

    # Container bind-mount target, present when auditing inside the image.
    roots.append(Path("/app/logs"))

    seen, out = set(), []
    for r in roots:
        key = str(r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _candidate_files(roots: list[Path], explicit: list[Path]) -> list[Path]:
    """Every affected log file under `roots`, plus anything named explicitly.

    Rotated siblings (`app.jsonl.1`, `llm_calls.jsonl.3`) count too — they hold
    exactly the same unredacted history as the live file.
    """
    found: list[Path] = []

    for p in explicit:
        if p.is_dir():
            found.extend(sorted(x for x in p.rglob("*") if x.is_file()))
        elif p.is_file():
            found.append(p)

    for root in roots:
        if not root.is_dir():
            continue
        for base in _AFFECTED_NAMES:
            found.extend(sorted(root.glob(base)))       # app.jsonl
            found.extend(sorted(root.glob(base + ".*")))  # app.jsonl.1, .2 …

    seen, out = set(), []
    for f in found:
        try:
            key = str(f.resolve())
        except OSError:
            key = str(f)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def _mask(secret: str) -> str:
    """Show enough to identify WHICH credential without reprinting it.

    Long values keep a 4-char head and tail; short ones are fully hidden, since
    revealing most of a short secret defeats the purpose.
    """
    s = secret.strip()
    if len(s) <= 12:
        return f"<{len(s)} chars hidden>"
    return f"{s[:4]}…{s[-4:]} ({len(s)} chars)"


def scan_file(path: Path, show_secrets: bool) -> list[tuple[int, str, str]]:
    """Return [(line_no, pattern_label, evidence)] for one file. Never raises."""
    hits: list[tuple[int, str, str]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                for idx, (pattern, _replacement) in enumerate(_LOG_REDACTIONS):
                    for m in pattern.finditer(line):
                        matched = m.group(0)
                        # A line already scrubbed at write time still matches the
                        # pattern (e.g. "Authorization: [REDACTED]") — that is a
                        # correctly-handled line, not a leak.
                        if "[REDACTED]" in matched:
                            continue
                        evidence = matched if show_secrets else _mask(matched)
                        hits.append((n, _label_for(idx), evidence))
    except OSError as exc:
        print(f"  ! could not read {path}: {exc}", file=sys.stderr)
    return hits


def _redact_text(text: str) -> str:
    """Apply every runtime redaction pattern to one string.

    A match that ALREADY contains `[REDACTED]` is left alone. Without this the
    header pattern re-fires on its own output — `Authorization: [REDACTED] more
    text` would collapse to `Authorization: [REDACTED]`, quietly deleting the
    rest of a line that held no secret. Skipping keeps the scrub idempotent and
    keeps it consistent with `scan_file`, which treats such matches as clean.
    """
    for pattern, replacement in _LOG_REDACTIONS:
        text = pattern.sub(
            lambda m: m.group(0) if "[REDACTED]" in m.group(0) else m.expand(replacement),
            text,
        )
    return text


def _redact_json(node):
    """Walk a decoded JSON value, redacting string leaves in place.

    Structural scrubbing is REQUIRED for the .jsonl sinks. The header pattern
    ends in `.+`, which is right for a log *message* (the secret runs to the end
    of the message) but catastrophic against a raw JSON *line*: it eats the
    closing `"}` too, leaving

        {"msg": "GET /x Authorization: [REDACTED]

    which no longer parses. app.jsonl is read line-by-line by /api/logs and the
    Admin viewer, so a text-level scrub would trade a secret leak for a broken
    log viewer. Decoding first confines each substitution to a single string
    value, where the greedy tail is exactly the intended behaviour.
    """
    if isinstance(node, str):
        return _redact_text(node)
    if isinstance(node, list):
        return [_redact_json(v) for v in node]
    if isinstance(node, dict):
        # Keys are field names, not payloads — redacting them would rename
        # fields and break consumers that key off them.
        return {k: _redact_json(v) for k, v in node.items()}
    return node


def _scrub_line(line: str) -> str:
    """Redact one log line, preserving JSON structure when the line is JSON."""
    stripped = line.strip()
    if stripped.startswith(("{", "[")):
        try:
            decoded = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            pass  # not really JSON — fall through to the text path
        else:
            scrubbed = _redact_json(decoded)
            if scrubbed == decoded:
                # Nothing to change — return the ORIGINAL bytes rather than a
                # re-serialised copy, so a clean file is left byte-for-byte
                # intact instead of being silently reformatted.
                return line
            return json.dumps(scrubbed, ensure_ascii=False) + "\n"
    return _redact_text(line)


def scrub_file(path: Path, backup: bool) -> int:
    """Rewrite `path` with every pattern replaced. Returns lines changed.

    Atomic: writes a temp file in the same directory then `os.replace`s it, so a
    crash mid-scrub cannot leave a half-written log.
    """
    changed = 0
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".scrub")
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as src, \
             tmp_path.open("w", encoding="utf-8") as dst:
            for line in src:
                new = _scrub_line(line)
                if new != line:
                    changed += 1
                dst.write(new)

        if backup:
            shutil.copy2(str(path), str(path) + ".bak")
        shutil.copystat(str(path), str(tmp_path))
        os.replace(str(tmp_path), str(path))
    except OSError as exc:
        print(f"  ! could not scrub {path}: {exc}", file=sys.stderr)
        tmp_path.unlink(missing_ok=True)
        return 0
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Find secrets left in log files by the pre-fix redaction wiring.",
    )
    ap.add_argument("--path", action="append", default=[], metavar="PATH",
                    help="Extra file or directory to scan (repeatable).")
    ap.add_argument("--show-secrets", action="store_true",
                    help="Print matched values in full instead of masking them.")
    ap.add_argument("--scrub", action="store_true",
                    help="Rewrite affected files in place, replacing secrets.")
    ap.add_argument("--no-backup", action="store_true",
                    help="With --scrub, do not leave a .bak copy.")
    ns = ap.parse_args()

    roots = _default_roots()
    explicit = [Path(p) for p in ns.path]
    files = _candidate_files(roots, explicit)

    print("Scanning for secrets left in logs by the pre-fix redaction wiring.")
    print("(diagnostics files — codegen/commands/build/verify — were always filtered)\n")
    print("Locations checked:")
    for r in roots:
        print(f"  {'[found]' if r.is_dir() else '[absent]':9} {r}")
    for p in explicit:
        print(f"  {'[found]' if p.exists() else '[absent]':9} {p}   (--path)")

    if not files:
        print("\nNo affected log files present on this host. Nothing to rotate from here.")
        print("Run this on each machine that ran the backend or a Celery worker,")
        print("and inside containers (their /app/logs may be a bind mount).")
        return 0

    print(f"\n{len(files)} candidate file(s):\n")
    total_lines = 0
    per_kind: dict[str, int] = {}
    dirty: list[Path] = []

    for f in files:
        hits = scan_file(f, ns.show_secrets)
        size = f.stat().st_size if f.exists() else 0
        if not hits:
            print(f"  CLEAN  {f}  ({size:,} bytes)")
            continue
        dirty.append(f)
        # Count LINES, not raw matches: the header and bearer-token patterns
        # both fire on a single `Authorization: Bearer …` line, so a match count
        # would overstate how many secrets are actually present.
        affected_lines = {ln for ln, _, _ in hits}
        total_lines += len(affected_lines)
        print(f"  LEAK   {f}  ({size:,} bytes) — {len(affected_lines)} line(s) "
              f"with secrets, {len(hits)} pattern match(es)")
        for line_no, label, evidence in hits[:20]:
            print(f"           line {line_no}: {label} → {evidence}")
        if len(hits) > 20:
            print(f"           … and {len(hits) - 20} more")
        for _, label, _ in hits:
            per_kind[label] = per_kind.get(label, 0) + 1

    if not dirty:
        print("\nResult: no secrets found. Nothing to rotate.")
        return 0

    print(f"\nResult: {total_lines} log line(s) containing secrets "
          f"across {len(dirty)} file(s).")
    print("\nBy credential type — rotate each of these:")
    for kind, count in sorted(per_kind.items(), key=lambda kv: -kv[1]):
        print(f"  {count:5}  {kind}")

    if ns.scrub:
        print("\nScrubbing…")
        wrote_backup = False
        for f in dirty:
            # Never scrub a backup this tool made. A .bak exists precisely to
            # preserve the original for the rotation audit, and scrubbing it
            # would both destroy that evidence and spawn a .bak.bak on every
            # subsequent run. Deleting them is a separate, deliberate step.
            if f.name.endswith(".bak"):
                print(f"  skipped {f}  (backup from an earlier scrub — "
                      f"delete it once rotation is done)")
                continue
            n = scrub_file(f, backup=not ns.no_backup)
            note = "" if ns.no_backup else f" (backup: {f.name}.bak)"
            wrote_backup = wrote_backup or not ns.no_backup
            print(f"  rewrote {n} line(s) in {f}{note}")
        if wrote_backup:
            print("\n  NOTE: the .bak copies still contain the secrets in the clear.")
            print("  Delete them once you have finished rotating.")
    else:
        print("\nScrubbing is OFF. Re-run with --scrub to rewrite these files in place.")

    print("\nScrubbing the files does NOT make the credentials safe — assume anything")
    print("listed above is compromised and rotate it at the source.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
