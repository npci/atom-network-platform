# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Per-run workspace: clone layout, leasing, reset, and GC (THE BOOK §6).

Layout: one workspace per run — ``WORKSPACE_ROOT/<run_id>/<repo_id>/`` (repo_id,
not a slug). Each selected repo is cloned ``--branch <ref>`` and its
``base_commit_sha`` recorded. A ``.lease`` file marks the dir's owning run.

**GC is mandatory** (clones are 200 MB–2 GB): it removes a workspace **only when**
its run is terminal AND past TTL AND lease-free. It never touches an active or
leased workspace.

All git is run through the platform-aware :data:`adapter` (§18.2) — argv only,
contained, no shell.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from sqlalchemy.orm import Session

from app.agents.platform_adapter import adapter
from app.core.config import settings
from app.models.agentic import AgenticRun, AgenticStatus, TERMINAL_STATUSES
from app.models.base import utcnow

logger = logging.getLogger("app.agentic")

_LEASE_FILE = ".lease"


def detect_crlf(raw: bytes) -> bool:
    """True when the byte content is CRLF-dominant (Windows-authored source)."""
    crlf = raw.count(b"\r\n")
    return crlf > 0 and crlf >= raw.count(b"\n") - crlf


def write_preserving_eol(target, content: str) -> None:
    """Write ``content`` matching the file's EXISTING dominant line endings.

    Every in-memory read normalizes to LF (universal newlines), so writing that text
    back verbatim converts a CRLF source to LF wholesale — git then shows EVERY line
    as changed and the real edit drowns (observed: a small agent edit on a 25K-line
    CRLF Java file rendered as −24,789/+24,890). Re-expanding to the file's own EOL
    keeps a 1-line edit a 1-line diff. New files (nothing on disk) are written LF."""
    content = content.replace("\r\n", "\n")
    try:
        crlf = target.is_file() and detect_crlf(target.read_bytes())
    except OSError:
        crlf = False
    if crlf:
        content = content.replace("\n", "\r\n")
    target.write_bytes(content.encode("utf-8"))


class WorkspaceError(RuntimeError):
    pass


# ── Layout ────────────────────────────────────────────────────────────────────

def workspace_root() -> Path:
    return Path(settings.agentic_workspace_root)


def run_dir(run_id: str) -> Path:
    return workspace_root() / run_id


def repo_dir(run_id: str, repo_id: str) -> Path:
    return run_dir(run_id) / repo_id


# ── Git ref validation (pure, testable) ───────────────────────────────────────

# Characters git itself permits in a ref, minus the ones that make a ref usable
# as an option. The leading char is constrained separately (see below) because a
# ref starting with '-' is parsed by git as a FLAG, not a value — and
# `--upload-pack=<cmd>` in a clone is a documented local-command-execution
# primitive. argv-only execution (no shell) already neutralises `;`, `|`, and
# `$()`; this closes the remaining argument-injection hole.
_GIT_REF_ALLOWED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def validate_git_ref(ref: str, *, field: str = "branch") -> str:
    """Return ``ref`` unchanged when it is a safe git ref, else raise ``ValueError``.

    Accepts what real branch and tag names look like — ``main``, ``release/2.0``,
    ``feature/NET-123_fix``, ``v1.2.3`` — so no working configuration changes.
    Rejects refs that begin with ``-`` (argument injection), contain whitespace,
    or contain any character git would refuse anyway.
    """
    value = (ref or "").strip()
    if not value:
        raise ValueError(f"{field} must not be empty")
    if len(value) > 255:
        raise ValueError(f"{field} is too long (max 255 characters)")
    if not _GIT_REF_ALLOWED.match(value):
        raise ValueError(
            f"invalid {field} {ref!r}: must start with a letter or digit and contain "
            "only letters, digits, dot, underscore, slash or hyphen"
        )
    # git's own ref rules — these would fail at clone time anyway, but failing
    # here gives a clear message instead of a raw git stderr dump.
    if ".." in value or value.endswith((".lock", "/", ".")) or "//" in value:
        raise ValueError(f"invalid {field} {ref!r}: not a well-formed git ref")
    return value


# ── Clone URL (pure, testable) ────────────────────────────────────────────────

def build_clone_url(gitlab_url: str, gitlab_repo: str, token: str) -> str:
    """``https://oauth2:<token>@host/<group>/<repo>.git`` from the configured base.

    ``localhost`` is rewritten to ``host.docker.internal`` so an in-container
    worker reaches a host-run GitLab (matches ``git_integrator``).
    """
    base = (gitlab_url or "").replace("://localhost", "://host.docker.internal").rstrip("/")
    parsed = urlparse(base)
    netloc = f"oauth2:{token}@{parsed.netloc}" if token else parsed.netloc
    path = f"{parsed.path}/{gitlab_repo.strip('/')}.git"
    return urlunparse((parsed.scheme or "https", netloc, path, "", "", ""))


# ── Clone + lease + base SHA ──────────────────────────────────────────────────

def _git(repo_path: Path, *args: str, timeout_s: int | None = None):
    res = adapter.run_command(repo_path, ["git", *args], timeout_s=timeout_s)
    return res


def _assert_disk_space(path: Path) -> None:
    """Refuse to clone when the workspace volume is below the configured free-space
    floor — clones are 200MB-2GB, so a full disk would otherwise wedge every run
    with a cryptic mid-clone error. A clear, early failure is far easier to triage."""
    floor_mb = getattr(settings, "agentic_min_disk_free_mb", 0) or 0
    if floor_mb <= 0:
        return
    try:
        free_mb = shutil.disk_usage(path).free // (1024 * 1024)
    except OSError:
        return  # can't stat → don't block on a measurement failure
    if free_mb < floor_mb:
        raise WorkspaceError(
            f"insufficient workspace disk: {free_mb} MB free < {floor_mb} MB required. "
            "Free space or raise AGENTIC_MIN_DISK_FREE_MB.")


def clone(run_id: str, repo_id: str, clone_url: str, branch: str) -> str:
    """Clone ``clone_url`` (branch ``branch``) into the run/repo dir; return the
    base commit SHA. Idempotent: an existing valid clone is reused (its current
    HEAD is returned), supporting crash-resume of ``workspace_ready``."""
    dest = repo_dir(run_id, repo_id)
    if dest.exists():
        # Reuse a VALID existing clone (crash-resume of workspace_ready). A clone
        # interrupted mid-fetch leaves a .git with no usable HEAD, and a leftover
        # non-clone dir makes `git clone` refuse "destination not empty" — in both
        # cases, discard and re-clone so resume is truly idempotent.
        if (dest / ".git").exists():
            probe = adapter.run_command(dest, ["git", "rev-parse", "HEAD"])
            if probe.ok:
                # Prefer the recorded clone-time base over current HEAD: a crash-resume
                # after the flow made a local commit must not silently re-anchor the
                # change-set to that commit.
                if (dest / _BASE_FILE).is_file():
                    return recorded_base(run_id, repo_id)
                return _record_base(run_id, repo_id)
        shutil.rmtree(dest, ignore_errors=True)

    dest.parent.mkdir(parents=True, exist_ok=True)
    _assert_disk_space(dest.parent)
    # Shallow clone (Lever 3) when a depth is configured: git makes `--depth N`
    # single-branch automatically, so this fetches only branch `branch` to N commits
    # — skipping the deep-history download that dominates clone time. The base commit
    # (HEAD) is present, so `git show <base_sha>:path` (XSD diff) is unaffected, and
    # the N-commit margin keeps recent `git log`/`blame` working. depth<=0 → full clone.
    depth = getattr(settings, "agentic_clone_depth", 0) or 0
    # Validate the ref before it reaches an option slot: a branch beginning with
    # '-' would otherwise be parsed by git as a flag (e.g. --upload-pack=<cmd>).
    safe_branch = validate_git_ref(branch)
    argv = ["git", "clone", "--branch", safe_branch]
    if depth > 0:
        argv += ["--depth", str(depth)]
    # `--` ends option parsing so the URL and target dir can never be read as flags.
    argv += ["--", clone_url, repo_id]
    res = adapter.run_command(dest.parent, argv)
    if not res.ok:
        # stderr may carry the token-bearing URL; the coding-log redactor masks
        # it, but keep the raised message generic.
        raise WorkspaceError(f"clone failed for {repo_id} (exit {res.exit_code})")

    write_lease(run_id, repo_id)
    return _record_base(run_id, repo_id)


def create_branch(run_id: str, repo_id: str, branch: str) -> bool:
    """Create + switch the clone to the run's feature branch (``checkout -B``).

    Called at PROVISIONING (§6), not at push time: the whole run — XSD edits,
    Phase-B code, verification — happens ON the feature branch, and Phase B
    adopts the same tree, so the branch born here is the one the combined MR
    ships. Runs before any git-guard policy is set (same window as the token
    scrub). Best-effort: a failure leaves the clone on the default branch,
    which the push-time ``checkout -B`` still recovers."""
    # Defence in depth: the name reaches a git option slot, so a value starting
    # with '-' would be read as a flag. Not currently attacker-reachable —
    # `agentic_push.branch_name()` slugifies to [a-z0-9-] behind a fixed prefix,
    # and the handoff_json path is internal (never populated from a request) —
    # but validating here means a future caller cannot reintroduce the hole.
    #
    # Returns False rather than raising: this function is documented best-effort
    # and its callers neither check the result nor catch exceptions, so a raise
    # would abort provisioning — a new failure mode. Every name branch_name()
    # can emit already validates, so behaviour is unchanged in practice.
    try:
        safe_branch = validate_git_ref(branch)
    except ValueError as exc:
        logger.warning("create_branch refused an unsafe ref %r: %s", branch, exc)
        return False
    return adapter.run_command(repo_dir(run_id, repo_id), ["git", "checkout", "-B", safe_branch]).ok


def set_remote(run_id: str, repo_id: str, url: str) -> bool:
    """Point the clone's ``origin`` at ``url``. Used to SCRUB the token after
    clone (§22): a credential-less origin means a tool-spawned ``git push``
    (e.g. ``mvn release:perform``) has no auth and cannot reach the remote — the
    runtime's approved push uses the GitLab API with the token directly. Must run
    before the git-guard policy is active (the guard denies ``remote set-url``)."""
    return adapter.run_command(repo_dir(run_id, repo_id), ["git", "remote", "set-url", "origin", url]).ok


def read_base_sha(run_id: str, repo_id: str) -> str:
    res = _git(repo_dir(run_id, repo_id), "rev-parse", "HEAD")
    if not res.ok:
        raise WorkspaceError(f"rev-parse HEAD failed for {repo_id}")
    return res.stdout.strip()


_BASE_FILE = ".base_sha"


def _record_base(run_id: str, repo_id: str) -> str:
    """Persist the clone-time HEAD as the durable diff anchor for this workspace."""
    sha = read_base_sha(run_id, repo_id)
    (repo_dir(run_id, repo_id) / _BASE_FILE).write_text(sha, encoding="utf-8")
    return sha


def recorded_base(run_id: str, repo_id: str) -> str:
    """The commit this workspace was CLONED at — the anchor that makes 'everything the
    agent changed' complete. Diffing against HEAD instead loses any work the flow
    locally committed (a failed push leaves its ``agentic:`` commit; the git-guard
    allows local ``git commit``), silently shrinking the change-set the human reviews
    and approves. Falls back to HEAD for legacy clones made before the marker.

    The marker must also RESOLVE: a well-formed sha naming a commit this repo no
    longer has (force-pushed base, workspace copied between environments) makes
    ``git diff <base>`` fail, and every caller treats that failure as "no tracked
    changes" — reinstating the very shrunken change-set this anchor exists to
    prevent, silently. Verifying here fixes all four consumers at once."""
    rd = repo_dir(run_id, repo_id)
    try:
        sha = (rd / _BASE_FILE).read_text(encoding="utf-8").strip()
        if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha.lower()):
            if _git(rd, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}").ok:
                return sha
            logger.warning(
                "recorded base %s for run=%s repo=%s does not resolve — falling back to "
                "HEAD; the change-set may omit locally-committed work",
                sha[:12], run_id, repo_id,
            )
    except OSError:
        pass
    return read_base_sha(run_id, repo_id)


# Build output / IDE dirs that must NEVER enter a change-set, even when a repo's
# .gitignore forgets them (a repo that doesn't ignore target/). The agent's own
# `mvn verify` produces target/, so reporting it as a "change" is always wrong: it is
# noise in the MR and target/classes can carry copied secrets (hsm cfg, properties).
# git's own --exclude-standard already drops these when gitignored; this is the
# belt-and-braces for repos that don't.
#
# `generated-sources`/`generated` catch JAXB/codegen output relocated OUT of target/
# but still named by convention (a module pointing its plugin at `src/generated-sources`
# yet forgetting to gitignore it). An *unconventionally*-named output dir is caught
# instead by :func:`jaxb_generated_prefixes`, which reads each pom's real plugin config.
_BUILD_OUTPUT_DIRS = frozenset({
    "target", "node_modules", "build", "dist", ".gradle", ".idea",
    "generated-sources", "generated",
})


def _is_build_output(path: str) -> bool:
    """True if any path segment is a build-output / IDE dir (e.g. ``mod/target/...``)."""
    return any(seg in _BUILD_OUTPUT_DIRS for seg in path.split("/"))


# Maven property substitution for a JAXB <outputDirectory>/<generateDirectory>/
# <sourceRoot> value, reduced to a MODULE-relative subpath. ${project.build.directory}
# → target (itself build output); ${basedir}/${project.basedir} ARE the module dir, so
# they drop to "".
def _resolve_pom_output_dir(out: str) -> str | None:
    """Module-relative generated-source subpath for a pom output-dir value, or None when
    it can't be placed (absolute path, or an unresolved ``${...}`` property)."""
    s = (out or "").strip().replace("\\", "/")
    if not s or s.startswith("/"):
        return None
    s = s.replace("${project.build.directory}", "target")
    s = s.replace("${project.basedir}", "").replace("${basedir}", "").strip("/")
    if "${" in s:                       # an unresolved property — don't guess a path
        return None
    return s or None


def jaxb_generated_prefixes(rd: Path) -> frozenset[str]:
    """Repo-relative dir prefixes a JAXB plugin generates sources into, for every pom.xml
    in the clone that relocates its output to a literal, tracked path.

    The default (``target/generated-sources/…``) and the conventional names are already
    caught by :data:`_BUILD_OUTPUT_DIRS`; this reads each pom's *real* plugin config so a
    module — e.g. netc-xsd-domain — that points JAXB at an unconventional path (and forgets
    to gitignore it) doesn't leak its DO-NOT-EDIT generated sources into the change-set as
    "new" files. Empty when no such module exists; fail-open on I/O."""
    from app.agents.jaxb_mapper import parse_pom_jaxb
    prefixes: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(rd):
        # prune build-output / git dirs in place — they hold no real source pom, and
        # pruning keeps the walk cheap on multi-GB clones.
        dirnames[:] = [d for d in dirnames if d not in _BUILD_OUTPUT_DIRS and d != ".git"]
        if "pom.xml" not in filenames:
            continue
        try:
            cfg = parse_pom_jaxb((Path(dirpath) / "pom.xml").read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        sub = _resolve_pom_output_dir(cfg["output_dir"]) if cfg else None
        if not sub:
            continue
        prefix = (Path(dirpath).relative_to(rd) / sub).as_posix().strip("/")
        if prefix and not _is_build_output(prefix):     # under target/… already handled
            prefixes.add(prefix)
    return frozenset(prefixes)


def _under_any(path: str, prefixes: frozenset[str]) -> bool:
    """True if ``path`` is, or lives under, one of the repo-relative ``prefixes``."""
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def changed_files(run_id: str, repo_id: str) -> list[tuple[str, str]]:
    """Disk = ground truth: the ``(op, repo_relative_path)`` the agent has produced
    so far, read from git — so a CONTINUED or crash-RESUMED run (whose in-memory
    changeset is empty) still verifies/freezes ALL edits, not just the last loop's.

    Anchored to the RECORDED clone-time base (not HEAD) so the change-set is complete
    regardless of local git state: uncommitted edits, locally-committed work (a failed
    push's leftover ``agentic:`` commit, an agent-made commit), and already-pushed
    changes all stay visible. Captures tracked modifies/deletes
    (``git diff --name-status <base>``) AND new files (``git ls-files --others`` —
    untracked files a plain ``git diff`` omits). Build output (``target/`` …) and JAXB
    generated sources (incl. pom-declared output dirs outside target/) are excluded
    even if the repo forgot to gitignore them."""
    rd = repo_dir(run_id, repo_id)
    gen = jaxb_generated_prefixes(rd)   # pom-declared generated-source dirs to also drop
    out: list[tuple[str, str]] = []
    seen: set[str] = {_LEASE_FILE, _BASE_FILE}   # never report our internal markers as changes
    diff = _git(rd, "diff", "--name-status", recorded_base(run_id, repo_id))
    if diff.ok:
        for line in diff.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            code, path = parts[0].strip(), parts[-1].strip()
            op = {"D": "delete", "A": "add"}.get(code[:1], "modify")
            if path and path not in seen and not _is_build_output(path) and not _under_any(path, gen):
                seen.add(path)
                out.append((op, path))
    others = _git(rd, "ls-files", "--others", "--exclude-standard")
    if others.ok:
        for path in others.stdout.splitlines():
            path = path.strip()
            if path and path not in seen and not _is_build_output(path) and not _under_any(path, gen):
                seen.add(path)
                out.append(("add", path))
    return out


def materialize_files(run_id: str, repo_id: str, files: list[dict]) -> int:
    """Write persisted ``{repo_id?, path, content}`` files into the repo clone and
    return the count written. Used by Phase B to restore Phase A's approved XSDs when
    the shared workspace was GC'd before code generation started (THE BOOK v3.4)."""
    rd = repo_dir(run_id, repo_id)
    n = 0
    for f in files or []:
        if f.get("repo_id") and f.get("repo_id") != repo_id:
            continue
        path, content = f.get("path"), f.get("content")
        if not path or content is None:
            continue
        p = (rd / path).resolve()
        if not str(p).startswith(str(rd.resolve())):
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        # Restore write permission before overwriting: a prior Phase-B run may have left an approved
        # file read-only (a legacy on-disk schema lock), which would fail write_text with
        # PermissionError. Best-effort — only works when we own the file.
        if p.exists():
            try:
                p.chmod(0o644)
            except OSError:
                pass
        write_preserving_eol(p, content)   # restore-over-base must not flip a CRLF file to LF
        n += 1
    return n


def export_zip(ws_id: str, repo_names: dict[str, str],
               extra_files: dict[str, str] | None = None) -> Path:
    """Zip the run's cloned working trees for developer download — one top-level
    folder per repo, named after the repo (``network/…``, ``network-2.0/…``).

    Excludes ``.git`` (FETCH_HEAD can carry the tokened clone URL — it must never
    leave the server), the ``.lease`` marker, build output and symlinks.
    ``extra_files`` are written verbatim at the archive root (e.g. the frozen
    per-repo diffs). Returns the temp zip path — the caller owns deletion."""
    import tempfile
    import zipfile
    roots = {name: repo_dir(ws_id, rid)
             for rid, name in repo_names.items() if repo_dir(ws_id, rid).is_dir()}
    if not roots:
        raise WorkspaceError("workspace is not on disk (not cloned yet, or cleaned up)")
    fd, tmp = tempfile.mkstemp(prefix="agentic-ws-", suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, root in roots.items():
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = [d for d in dirnames
                                   if d != ".git" and d not in _BUILD_OUTPUT_DIRS]
                    for fn in filenames:
                        if fn == _LEASE_FILE:
                            continue
                        p = Path(dirpath) / fn
                        if p.is_symlink():
                            continue
                        zf.write(p, f"{name}/{p.relative_to(root)}")
            for arcname, content in (extra_files or {}).items():
                zf.writestr(arcname, content)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return Path(tmp)


def write_lease(run_id: str, repo_id: str) -> None:
    (repo_dir(run_id, repo_id) / _LEASE_FILE).write_text(run_id, encoding="utf-8")


def lease_holder(run_id: str, repo_id: str) -> str | None:
    p = repo_dir(run_id, repo_id) / _LEASE_FILE
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


def _scrub_before_removal(d: Path) -> None:
    """T10 (THREAT_MODEL.md) — strip platform-written credentials from a
    workspace's git metadata, and optionally report source-repo secrets, BEFORE
    the directory is removed.

    Runs before `rmtree` on purpose. If removal succeeds the scrub was
    redundant, which costs a few file rewrites on git metadata only. If removal
    FAILS (permissions, a Windows file lock, a full disk) the directory
    survives — and that is exactly the case where the tokened reflog would
    otherwise sit on disk indefinitely, well past the TTL window this threat is
    scoped to. Scrubbing first makes the failure mode safe rather than silent.

    Never raises: this is defence-in-depth around the operation that actually
    deletes the secret, and must never be the reason deletion doesn't happen.
    """
    try:
        if not getattr(settings, "workspace_credential_scrub_enabled", True):
            return
        from app.agents.workspace_secret_scrub import scrub_and_scan
        scrub_and_scan(
            d,
            scan_enabled=bool(getattr(settings, "workspace_secret_scan_enabled", False)),
        )
    except Exception:  # noqa: BLE001 — never block cleanup/GC
        logger.exception("workspace secret scrub failed for %s (continuing with removal)", d.name)


def cleanup_workspace(run_id: str) -> bool:
    """Remove the workspace directory for a run that has reached a terminal state
    (COMPLETED, FAILED, CANCELLED). Called by the orchestrator immediately when a
    run finishes so disk is reclaimed without waiting for GC TTL.

    Returns True if the directory was removed, False if it didn't exist or removal
    failed (logged, not raised — cleanup is best-effort)."""
    d = run_dir(run_id)
    if not d.exists():
        return False
    _scrub_before_removal(d)   # T10 — see the helper's docstring for the ordering rationale
    try:
        shutil.rmtree(d)
        logger.info("workspace cleaned up immediately for run=%s", run_id)
        return True
    except OSError as exc:
        logger.warning("immediate workspace cleanup failed for run=%s: %s", run_id, exc)
        return False


def reset_to_base(run_id: str, repo_id: str, base_sha: str) -> None:
    """Reset the clone to the recorded base SHA + clean untracked files (§6).

    Called before re-running a phase so a crashed attempt's partial edits never
    leak into the next. The git-guard (§22) permits ``reset --hard`` only to the
    recorded base SHA — enforced there in S12; here we only ever pass base_sha.
    """
    d = repo_dir(run_id, repo_id)
    r1 = _git(d, "reset", "--hard", base_sha)
    r2 = _git(d, "clean", "-fd")
    if not (r1.ok and r2.ok):
        raise WorkspaceError(f"reset_to_base failed for {repo_id}")


# ── Garbage collection (§6) ───────────────────────────────────────────────────

def _has_active_dependent(db: Session | None, run_id: str | None,
                          now=None, ttl_hours: int = 0) -> bool:
    """True if a run still relies on ``run_id``'s workspace — a Phase-B (``code``) run
    adopts its Phase-A parent's tree (``parent_run_id`` / ``workspace_run_id``), so
    Phase A's clone must survive until the child finishes. A TERMINAL child that is
    still within its own resume TTL also counts: a failed/cancelled code run advertises
    "Re-run to continue" and Resume drives the SAME tree — collecting the parent
    workspace the moment the child dies destroys the unshipped on-disk edits that
    resume continues from (observed: run 74a785ac, GC'd 2 min before its resume)."""
    if db is None or not run_id:
        return False
    dependents = (AgenticRun.parent_run_id == run_id) | (AgenticRun.workspace_run_id == run_id)
    alive = AgenticRun.status.notin_([s.value for s in TERMINAL_STATUSES])
    if now is not None and ttl_hours:
        alive = alive | (AgenticRun.updated_at >= (now - timedelta(hours=ttl_hours)))
    exists_q = (
        db.query(AgenticRun.id)
        .filter(AgenticRun.id != run_id, dependents, alive)
        .first()
    )
    return exists_q is not None


def _push_pending(db: Session | None, run: AgenticRun) -> bool:
    """True when the human approved the manifest but the branch was never pushed
    (deferred push), OR when a repo was pushed under an OLDER manifest than the one
    now frozen (stale push — the tree is the source for the re-push). The working
    tree is still the push source, so GC must keep it even though the run is
    terminal. kind='xsd' runs never push themselves (Phase B raises the combined
    MR), so they are exempt."""
    if db is None or (getattr(run, "kind", None) or "full") == "xsd":
        return False
    from app.models.agentic import ChangeManifest, AgenticRunRepo
    man = (db.query(ChangeManifest).filter(ChangeManifest.run_id == run.id)
           .order_by(ChangeManifest.created_at.desc()).first())
    if man is None or man.approved_at is None:
        return False
    rows = db.query(AgenticRunRepo).filter(AgenticRunRepo.run_id == run.id).all()
    return (not rows
            or any(r.push_state != "pushed" for r in rows)
            or any(r.push_state == "pushed"
                   and getattr(r, "pushed_manifest_hash", None) is not None
                   and r.pushed_manifest_hash != man.manifest_hash for r in rows))


def _is_collectable(run: AgenticRun | None, now, ttl_hours: int, db: Session | None = None) -> bool:
    """A workspace is collectable only if its run is terminal AND past TTL AND
    lease-free AND no non-terminal child still needs it AND its approved manifest
    (if any) has been pushed. An orphaned dir with no matching run row is also
    collectable."""
    if run is None:
        return True  # no owning run → orphan
    if run.status not in {s.value for s in TERMINAL_STATUSES}:
        return False  # active / awaiting_human_approval / rebase_reverify → keep
    age_ok = run.updated_at is not None and run.updated_at < (now - timedelta(hours=ttl_hours))
    lease_free = run.lease_owner is None or (run.lease_expires_at is not None and run.lease_expires_at < now)
    if not (age_ok and lease_free):
        return False
    if _push_pending(db, run):
        return False  # approved but push deferred — the tree is the push source
    # keep Phase A's tree for an active OR recently-terminal (resumable) Phase B
    return not _has_active_dependent(db, run.id, now=now, ttl_hours=ttl_hours)


def _dir_size_mb(path: Path) -> float:
    """Best-effort recursive size of a directory tree, in MB. Never raises —
    a single unreadable file (permissions, race with concurrent GC/clone)
    must not abort the whole quota computation; that file's bytes are
    simply undercounted for this pass."""
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total / (1024 * 1024)


def gc_workspaces(db: Session, ttl_hours: int | None = None) -> list[str]:
    """Remove terminal+expired+lease-free workspaces. Returns removed run_ids.

    NEVER removes an active or leased workspace. Safe to run repeatedly.

    A17 (architecture review Medium #17, "Workspace GC is Time-Based, Not
    Quota-Based") — after the TTL-based pass below, ALSO enforces a quota
    backstop: if the workspace root is still over
    `agentic_workspace_max_total_mb` or `agentic_workspace_max_count` after
    the TTL sweep, additional collectable-but-not-yet-expired workspaces
    (terminal + lease-free + push-complete — the SAME safety checks
    `_is_collectable` applies, just without the TTL age requirement) are
    evicted oldest-`updated_at`-first until back under quota. This bounds
    disk usage independent of how long the operator's TTL window is, so a
    burst of completed runs cannot exhaust the workspace volume while
    waiting for the hourly sweep + TTL to catch up (the review's
    "new changes are blocked until the hourly GC runs" availability gap).
    """
    root = workspace_root()
    if not root.exists():
        return []
    ttl = ttl_hours if ttl_hours is not None else settings.agentic_workspace_ttl_hours
    now = utcnow()
    removed: list[str] = []
    remaining: list[tuple[Path, AgenticRun | None]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        run = db.get(AgenticRun, child.name)
        if _is_collectable(run, now, ttl, db=db):
            _scrub_before_removal(child)   # T10
            try:
                shutil.rmtree(child)
                removed.append(child.name)
                continue
            except OSError as exc:
                logger.warning("workspace GC failed to remove %s: %s", child, exc)
        remaining.append((child, run))

    max_total_mb = getattr(settings, "agentic_workspace_max_total_mb", 0) or 0
    max_count = getattr(settings, "agentic_workspace_max_count", 0) or 0
    if max_total_mb <= 0 and max_count <= 0:
        return removed  # quota backstop disabled — TTL-only behaviour unchanged

    # Quota-eligible = collectable EXCEPT for the TTL age check (i.e. terminal,
    # lease-free, push-complete, no active dependent — everything _is_collectable
    # checks except `age_ok`). Sort oldest-updated-first so the longest-idle
    # completed runs are reclaimed before more recently finished ones.
    def _quota_eligible(run: AgenticRun | None) -> bool:
        if run is None:
            return True  # orphan dir, no owning row — always eligible
        if run.status not in {s.value for s in TERMINAL_STATUSES}:
            return False
        lease_free = run.lease_owner is None or (
            run.lease_expires_at is not None and run.lease_expires_at < now)
        if not lease_free:
            return False
        if _push_pending(db, run):
            return False
        return not _has_active_dependent(db, run.id, now=now, ttl_hours=ttl)

    candidates = [(child, run) for child, run in remaining if _quota_eligible(run)]
    candidates.sort(key=lambda cr: (
        cr[1].updated_at if cr[1] is not None and cr[1].updated_at is not None
        else now - timedelta(days=3650)  # orphans with no row sort oldest-first
    ))

    if not candidates:
        return removed

    total_count = len(remaining)
    total_mb = None  # computed lazily — a full disk walk of every remaining dir
                     # is only worth paying for when a count-only quota isn't
                     # already enough to decide.

    for child, run in candidates:
        over_count = max_count > 0 and total_count > max_count
        if max_total_mb > 0:
            if total_mb is None:
                total_mb = sum(_dir_size_mb(c) for c, _ in remaining)
            over_size = total_mb > max_total_mb
        else:
            over_size = False
        if not (over_count or over_size):
            break
        _scrub_before_removal(child)   # T10
        try:
            freed_mb = _dir_size_mb(child)
            shutil.rmtree(child)
            removed.append(child.name)
            total_count -= 1
            if total_mb is not None:
                total_mb -= freed_mb
            logger.info(
                "workspace GC quota eviction: run=%s freed_mb=%.1f "
                "(over_count=%s over_size=%s)",
                child.name, freed_mb, over_count, over_size,
            )
        except OSError as exc:
            logger.warning("workspace GC quota eviction failed to remove %s: %s", child, exc)

    return removed
