# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Workspace credential scrubbing — closes THREAT_MODEL.md T10.

T10: "No secret-scrubbing pass on workspace contents before GC — if a cloned
repo happens to contain a credential file, it persists in the workspace for the
TTL window."

While implementing this, a CONCRETE instance of the threat was found that the
original finding did not name — and it is caused by this platform, not by source
repo hygiene:

    `git clone https://oauth2:<token>@host/group/repo.git` writes the FULL
    tokened URL into `.git/logs/HEAD` (and `.git/logs/refs/**`). The existing
    post-clone scrub (`workspace_local.set_remote`, THE BOOK §22) rewrites
    `.git/config` only — verified empirically that `git remote set-url` leaves
    every reflog entry untouched. So the GitLab token survives in the workspace
    reflog for the whole `agentic_workspace_ttl_hours` window.

That is why this module does two different things:

  1. `scrub_workspace_credentials()` — REMEDIATION for the known-real leak
     above. Rewrites reflog lines to strip embedded `user:password@` credentials
     in place. This is not heuristic: it targets a specific, reproducible
     artifact of how the platform clones.

  2. `scan_workspace_for_secrets()` — DETECTION for the original T10 concern (a
     credential committed into the SOURCE repo). Reports counts by rule; it
     deliberately does NOT modify tracked source files, because rewriting a
     developer's committed content under them would corrupt the diff the human
     reviews and approves. Source-repo hygiene is the source repo's to fix; this
     platform's job is to detect and report it loudly.

Pattern provenation
-------------------
The patterns come from the project's OWN `.gitleaks.toml`, whose header records
the lesson that a stock scanner gives false assurance on a codebase that mints
its own credential formats (`a2a_…` keys were invisible to stock rules). Keeping
these in step with that file is the same discipline it already asks for: "When
you add a new credential format, add a rule here in the same commit."

Cost control
------------
Clones are 200 MB–2 GB. A naive full-tree regex walk on every GC pass would be
a self-inflicted DoS on the very sweep that exists to protect disk. So the scan
skips build output and IDE dirs (reusing `workspace_local`'s own set), skips
binary and oversized files, and is bounded by a file budget. It is also OFF by
default — the reflog scrub, which is cheap and targets a known leak, is the part
that runs unconditionally.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger("app.agentic")

# ── Credential patterns (kept in step with /.gitleaks.toml) ──────────────────
#
# Each entry is (rule_id, compiled_pattern). Rule ids match .gitleaks.toml so a
# finding here and a finding there are traceable to the same rule.
_SECRET_RULES: tuple[tuple[str, re.Pattern], ...] = (
    # Project-minted A2A partner API key: f"a2a_{secrets.token_urlsafe(32)}".
    # The .gitleaks.toml allowlist excludes all-lowercase matches because Python
    # identifiers (e.g. long test names) collide with the length rule; the same
    # exclusion is applied below in `_is_allowlisted`.
    ("a2a-partner-api-key", re.compile(r"a2a_[A-Za-z0-9_\-]{40,}")),
    ("gitlab-pat", re.compile(r"glpat-[A-Za-z0-9_\-]{20,}")),
    ("anthropic-key", re.compile(r"sk-ant-api03-[A-Za-z0-9_\-]{20,}")),
    # Credential embedded in a URL — the class this platform itself creates via
    # build_clone_url(). Matches any scheme so an https:// or git:// remote with
    # inline credentials is caught.
    ("credential-in-url", re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^/\s:@]+:[^/\s@]+@")),
    ("private-key-pem", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
)

# Mirrors .gitleaks.toml's `regexes` allowlist for the cases that are provably
# not credentials. Only the entries that can plausibly appear in a cloned source
# tree are carried over (the doc-prose and test-fixture entries are specific to
# THIS repo's own files, which are never inside a workspace clone).
_ALLOWLIST: tuple[re.Pattern, ...] = (
    # Python identifiers beginning `a2a_` — 40+ chars of [a-z_]. A minted key is
    # token_urlsafe(32) from a 64-symbol alphabet; the chance of one being all
    # lowercase+underscore is ~(27/64)^43 ≈ 1e-16. See .gitleaks.toml's note.
    re.compile(r"^a2a_[a-z_]+$"),
    # Documented placeholders.
    re.compile(r"^glpat-(your-token|\.\.\.)$"),
    re.compile(r"^sk-ant-api03-\.\.\.$"),
)

# Files that cannot meaningfully hold a greppable credential, or that would make
# the scan pathologically slow. Binary detection is done by NUL sniffing rather
# than by extension so an unknown binary format is still skipped.
_SKIP_SUFFIXES = frozenset({
    ".jar", ".war", ".ear", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
    ".class", ".so", ".dll", ".dylib", ".exe", ".bin", ".o", ".a",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp",
    ".pdf", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3", ".avi",
})

_MAX_FILE_BYTES = 1 * 1024 * 1024      # skip files >1 MB — a credential in a
                                       # multi-MB generated blob is not the
                                       # threat T10 describes, and reading them
                                       # dominates the sweep's cost.
_DEFAULT_MAX_FILES = 20_000            # hard bound on files examined per run.

# Build-output / IDE directories to prune. Duplicated from
# `workspace_local._BUILD_OUTPUT_DIRS` DELIBERATELY rather than imported:
# importing it pulls in `platform_adapter` -> `core.config.settings`, which
# means this module could not be imported (or unit-tested) without a fully
# valid application configuration. A scrubber that needs a database URL present
# in order to check a directory name is the wrong coupling. Keep this set in
# step with workspace_local's; a drift here only costs scan time, never
# correctness (an unpruned build dir is scanned, not mis-reported).
_BUILD_OUTPUT_DIRS = frozenset({
    "target", "node_modules", "build", "dist", ".gradle", ".idea",
    "generated-sources", "generated",
})


def _is_allowlisted(match_text: str) -> bool:
    return any(rx.match(match_text) for rx in _ALLOWLIST)


# ── 1. Remediation: strip credentials the platform itself wrote ───────────────

# `git clone https://user:token@host/...` records the full URL in the reflog.
# Replace the credential portion, preserving the rest of the line so the reflog
# stays structurally valid (git tolerates the URL text changing — it is a
# free-form message field).
_URL_CRED_RE = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<cred>[^/\s:@]+:[^/\s@]+)@")

# The replacement deliberately contains NO colon. An obvious-looking marker like
# `***:***@` would itself match `_URL_CRED_RE` (which only needs
# `something:something@`), so every subsequent scrub pass would "find" and
# rewrite the same line again — the function would not be idempotent, and each
# GC sweep would pointlessly rewrite every reflog it had already cleaned. A
# colon-free marker cannot re-match, which makes a second pass a no-op.
_CRED_REDACTED = "[credentials-redacted]"


def _scrub_url_credentials(text: str) -> tuple[str, int]:
    """Replace `scheme://user:secret@` with `scheme://[credentials-redacted]@`.

    Idempotent: the replacement cannot itself match `_URL_CRED_RE` (see above).
    """
    return _URL_CRED_RE.subn(rf"\g<scheme>{_CRED_REDACTED}@", text)


def scrub_workspace_credentials(ws_dir: Path) -> dict:
    """Strip credentials the PLATFORM wrote into a workspace's git metadata.

    Targets `.git/logs/**` (reflogs), where `git clone` records the tokened
    clone URL and which `git remote set-url` does not clean. Also re-checks
    `.git/config` and `.git/FETCH_HEAD` for the same pattern, so this is
    idempotent and safe even where the existing §22 scrub already ran.

    Returns a summary dict: `{"files_rewritten": int, "credentials_removed": int}`.

    Never raises — a scrub failure must not block GC or a run's cleanup, which
    is the operation that actually removes the secret from disk. A failure here
    degrades to "the workspace is deleted with its reflog intact", i.e. today's
    behaviour, and is logged.
    """
    summary = {"files_rewritten": 0, "credentials_removed": 0}
    if not ws_dir.is_dir():
        return summary

    targets: list[Path] = []
    for repo_dir in [p for p in ws_dir.iterdir() if p.is_dir()]:
        git_dir = repo_dir / ".git"
        if not git_dir.is_dir():
            continue
        logs = git_dir / "logs"
        if logs.is_dir():
            targets.extend(p for p in logs.rglob("*") if p.is_file())
        for name in ("config", "FETCH_HEAD", "ORIG_HEAD", "packed-refs"):
            p = git_dir / name
            if p.is_file():
                targets.append(p)

    for path in targets:
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            original = path.read_text(encoding="utf-8", errors="surrogateescape")
        except (OSError, ValueError):
            continue
        if "@" not in original:
            continue  # cheap pre-filter — no credential-in-URL possible
        rewritten, n = _scrub_url_credentials(original)
        if n == 0:
            continue
        try:
            path.write_text(rewritten, encoding="utf-8", errors="surrogateescape")
        except OSError as exc:
            logger.warning("workspace credential scrub could not rewrite %s: %s", path.name, exc)
            continue
        summary["files_rewritten"] += 1
        summary["credentials_removed"] += n

    if summary["credentials_removed"]:
        # §13.2 structured telemetry. Never logs the credential or the full path
        # (the path embeds the run id, which is fine, but the value never is).
        logger.info(
            "SECURITY_EVENT event=workspace_credentials_scrubbed severity=low "
            "workspace=%s files_rewritten=%d credentials_removed=%d decision=redacted",
            ws_dir.name, summary["files_rewritten"], summary["credentials_removed"],
        )
    return summary


# ── 2. Detection: report credentials committed in the SOURCE repo ─────────────

def scan_workspace_for_secrets(ws_dir: Path, *,
                               max_files: int = _DEFAULT_MAX_FILES) -> dict:
    """Scan a workspace's working-tree files for credential patterns.

    Reports only — does NOT modify tracked source content. Rewriting a
    developer's committed file would corrupt the diff a human is about to
    review and approve, and the underlying problem (a secret committed to the
    source repo) can only be fixed in that repo.

    Returns `{"findings": {rule_id: count}, "files_scanned": int,
    "files_with_findings": int, "truncated": bool}`. `truncated` is True when
    the file budget was hit, so a caller never mistakes a bounded scan for a
    clean bill of health.

    Never raises.
    """
    result: dict = {"findings": {}, "files_scanned": 0,
                    "files_with_findings": 0, "truncated": False}
    if not ws_dir.is_dir():
        return result

    findings: dict[str, int] = {}
    scanned = 0
    flagged = 0
    try:
        import os
        for dirpath, dirnames, filenames in os.walk(ws_dir):
            # Prune build output, IDE dirs and .git. `.git` is excluded here
            # because it is the REMEDIATION path's concern (above) — scanning it
            # would double-report the platform's own clone URL as if it were a
            # source-repo hygiene problem.
            dirnames[:] = [d for d in dirnames
                           if d != ".git" and d not in _BUILD_OUTPUT_DIRS]
            for fn in filenames:
                if scanned >= max_files:
                    result["truncated"] = True
                    break
                p = Path(dirpath) / fn
                if p.suffix.lower() in _SKIP_SUFFIXES or p.is_symlink():
                    continue
                try:
                    if p.stat().st_size > _MAX_FILE_BYTES:
                        continue
                    raw = p.read_bytes()
                except OSError:
                    continue
                if b"\x00" in raw[:8192]:
                    continue  # binary
                scanned += 1
                try:
                    text = raw.decode("utf-8", errors="ignore")
                except Exception:  # noqa: BLE001
                    continue
                hit = False
                for rule_id, pattern in _SECRET_RULES:
                    for m in pattern.finditer(text):
                        if _is_allowlisted(m.group(0)):
                            continue
                        findings[rule_id] = findings.get(rule_id, 0) + 1
                        hit = True
                if hit:
                    flagged += 1
            if result["truncated"]:
                break
    except Exception:  # noqa: BLE001 — detection must never break the caller
        logger.exception("workspace secret scan failed for %s", ws_dir.name)

    result["findings"] = findings
    result["files_scanned"] = scanned
    result["files_with_findings"] = flagged

    if findings:
        # Deliberately WARNING, not INFO: a credential committed in a source
        # repo is a real hygiene defect the operator should see, even though
        # this platform cannot fix it. Counts by rule only — never the value,
        # never the matched line.
        logger.warning(
            "SECURITY_EVENT event=workspace_source_secrets_detected severity=medium "
            "workspace=%s files_with_findings=%d rules=%s truncated=%s "
            "decision=reported detail=\"credential pattern(s) found in cloned "
            "source content — fix in the SOURCE repository; this platform only reports\"",
            ws_dir.name, flagged, sorted(findings.items()), result["truncated"],
        )
    return result


def scrub_and_scan(ws_dir: Path, *, scan_enabled: bool = False) -> dict:
    """Convenience entry point for the workspace lifecycle: always scrub the
    platform-written credentials, optionally run the (more expensive) source
    scan. Returns the merged summary. Never raises."""
    out = {"scrub": {}, "scan": None}
    try:
        out["scrub"] = scrub_workspace_credentials(ws_dir)
    except Exception:  # noqa: BLE001
        logger.exception("workspace credential scrub failed for %s", ws_dir.name)
    if scan_enabled:
        out["scan"] = scan_workspace_for_secrets(ws_dir)
    return out
