# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Git-guard hook — the remote-write security boundary (THE BOOK §22).

A runtime-owned, deterministic pre-execution hook in front of EVERY git op
(LLM-issued via run_command or runtime-issued). The model cannot bypass it.

Policy (§22.1):
* **Local** ops within the workspace are unrestricted — except ``reset --hard`` /
  ``clean`` which may only return the tree to the recorded ``base_commit_sha``.
* **Remote**: exactly ONE mutating op is allowed — ``git push`` of the **new
  branch this run created**, which must be **brand-new on the remote**, with no
  ``--force``, no delete, no tag, to ``origin``. Everything else to the remote —
  push to any other/existing branch, force, delete, tags, ``remote set-url``,
  merge — is DENIED. A human merges the MR; the agent never merges.

The hook is consulted by :func:`app.agents.platform_adapter.PlatformAdapter.run_command`
for any ``git`` argv when a per-run :class:`GitGuardPolicy` is active (set via the
contextvar), AND wrapped around the runtime's own push/rebase calls — one choke
point, both callers.

**Known boundary (deferred, §17-class):** the guard intercepts ``git`` argv only.
A different allowlisted tool that spawns git internally (e.g. ``mvn
release:perform``/``scm:checkin``) is NOT git-guarded. The real defense is to keep
credentials OUT of the clone's ``origin`` remote — S13's push injects the token
only for the single guarded push — so a tool-spawned ``git push`` has no auth and
cannot reach the remote. Until that lands, treat tool-spawned git as out of the
boundary (containment, not a sandbox). The policy is also **fail-open** when unset:
the clone/toolchain phases run git before a policy exists; S13 MUST ``set_policy``
before any LLM-driven ``run_command`` and the push phase.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass

# Local subcommands that only ever touch the workspace clone (never the remote).
_LOCAL_OK = frozenset({
    "status", "diff", "log", "show", "grep", "add", "commit", "rebase", "checkout",
    "restore", "switch", "fetch", "rev-parse", "rev-list", "ls-files", "ls-remote",
    "stash", "merge-base", "cat-file", "describe", "blame", "symbolic-ref",
    "for-each-ref", "update-index", "apply", "cherry", "name-rev", "branch", "tag",
})
# Subcommands that are flatly denied (remote mutation / merge).
_DENY = frozenset({"remote", "merge", "pull", "am", "format-patch", "send-email"})

_PUSH_FORBIDDEN_FLAGS = frozenset({
    "-f", "--force", "--force-with-lease", "--delete", "-d", "--mirror", "--all",
    "--tags", "--follow-tags", "--prune",
})

_PROTECTED_BRANCHES = frozenset({
    "main", "master", "develop", "release", "staging", "production", "prod",
})


@dataclass
class GitGuardPolicy:
    run_branch: str                       # the new branch this run created
    base_sha: str
    branch_exists_on_remote: bool = False  # from a live `git ls-remote` before push
    # Governance fix pushes append commits to the run's EXISTING feature branch.
    # This permits ONLY that exact case — non-force, exact run_branch; every other
    # push rule (single refspec, origin-only, no +/delete/tags) still applies.
    allow_existing_branch: bool = False


@dataclass
class GitDecision:
    allowed: bool
    reason: str = ""


def deny_remote_policy() -> "GitGuardPolicy":
    """A baseline policy for the model-driven agent loop: local git reads/edits are
    allowed, but EVERY remote write is denied (no run_branch ⇒ any push is rejected;
    `_DENY` blocks remote/pull/merge). Set this around the agent loop so a model-issued
    `git push` cannot reach origin BEFORE human approval — without it the guard is
    fail-open during tool execution. The approved push (`_git_push_branch`) sets its own
    tight allow-this-branch policy in a separate task, so it is unaffected."""
    return GitGuardPolicy(run_branch="\x00no-remote-write", base_sha="")


# Active policy for the current run (set by the orchestrator; None = no agentic
# run in scope → the guard is not enforced on legacy callers).
_policy: contextvars.ContextVar["GitGuardPolicy | None"] = contextvars.ContextVar(
    "agentic_git_guard_policy", default=None,
)


def set_policy(policy: GitGuardPolicy | None):
    return _policy.set(policy)


def reset_policy(token) -> None:
    _policy.reset(token)


def active_policy() -> "GitGuardPolicy | None":
    return _policy.get()


def _subcommand(argv: list[str]) -> tuple[str | None, list[str], list[str]]:
    """Skip ``git`` and global flags (``-C dir``, ``-c k=v``, ``--git-dir=…``) to
    the real subcommand + its remaining args. Also returns the global ``-c`` config
    values seen, so a push classifier can veto a ``pushurl``/``remote`` override that
    would redirect the write away from the guarded origin (F10)."""
    configs: list[str] = []
    i = 1 if argv and argv[0].split("/")[-1].split("\\")[-1].lower().startswith("git") else 0
    while i < len(argv):
        a = argv[i]
        if a in ("-C", "--git-dir", "--work-tree", "--namespace"):
            i += 2
            continue
        if a == "-c":
            if i + 1 < len(argv):
                configs.append(argv[i + 1])
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a, argv[i + 1:], configs
    return None, [], configs


# Config keys that can redirect/override where or how a push writes — a push carrying
# any of these via `-c` is denied outright (the guarded callers never need them).
_PUSH_UNSAFE_CONFIG_RE = ("remote.", "url.", "pushurl", "http.", "credential.", "core.sshcommand")


def _flag_base(tok: str) -> str:
    """The flag name without its ``=value`` — so ``--force-with-lease=X`` is caught
    by the same forbidden-flag set as the bare form (F10)."""
    return tok.split("=", 1)[0] if tok.startswith("--") and "=" in tok else tok


def _classify_push(rest: list[str], policy: GitGuardPolicy, configs: list[str] | None = None) -> GitDecision:
    for cfg in (configs or []):
        if any(k in cfg.lower() for k in _PUSH_UNSAFE_CONFIG_RE):
            return GitDecision(False, f"push carries an unsafe -c config override ({cfg!r})")
    for f in rest:
        # Match the flag NAME, so a `--force-with-lease=refs/…` value form is denied
        # exactly like the bare `--force-with-lease` (the exact-string check missed it).
        if _flag_base(f) in _PUSH_FORBIDDEN_FLAGS:
            return GitDecision(False, f"push flag {f} denied")
    positional = [a for a in rest if not a.startswith("-")]
    # EXACTLY `origin <one-refspec>`. A bare push could target a tracking branch
    # we don't control; a SECOND refspec (`origin good-branch main`) would push an
    # extra ref past a single-refspec check.
    if len(positional) != 2:
        return GitDecision(False, "push must be exactly: origin <new-branch> (one refspec)")
    remote, refspec = positional
    if remote != "origin":
        return GitDecision(False, f"push remote {remote!r} != origin")
    # `+src:dst` (or a leading `+`) is force-push syntax that bypasses the --force
    # flag check entirely.
    if "+" in refspec:
        return GitDecision(False, "force-push refspec (leading +) denied")
    if refspec.startswith(":"):
        return GitDecision(False, "ref deletion denied")
    if "refs/tags/" in refspec or refspec.startswith("tag"):
        return GitDecision(False, "tag push denied")
    # A `src:dst` refspec — the SOURCE must be our own work, never an arbitrary ref/commit
    # (`origin attacker:branch` would fast-forward unapproved commits into the expected
    # destination). Only HEAD or the run's own branch may be the source (F10/§5.2).
    if ":" in refspec:
        src = refspec.split(":", 1)[0].removeprefix("refs/heads/")
        if src not in ("HEAD", policy.run_branch):
            return GitDecision(False, f"push source {src!r} is not HEAD or this run's branch")
    # Resolve the destination branch of "src:dst" or "branch".
    dst = refspec.split(":", 1)[1] if ":" in refspec else refspec
    dst = dst.removeprefix("refs/heads/")
    if dst in _PROTECTED_BRANCHES:
        return GitDecision(False, f"push to protected branch {dst!r} denied")
    if dst != policy.run_branch:
        return GitDecision(False, f"push to {dst!r} != this run's branch {policy.run_branch!r}")
    if policy.branch_exists_on_remote:
        if policy.allow_existing_branch:
            return GitDecision(True, "allowed remote write: fast-forward append to this run's branch")
        return GitDecision(False, f"branch {dst!r} already exists on the remote (must be brand-new)")
    return GitDecision(True, "the one allowed remote write: new branch push")


def classify_git(argv: list[str], policy: GitGuardPolicy) -> GitDecision:
    """Classify a git argv under the run's policy. Pure + deterministic."""
    sub, rest, configs = _subcommand(argv)
    if sub is None:
        return GitDecision(False, "no git subcommand")
    if sub == "push":
        return _classify_push(rest, policy, configs)
    if sub in _DENY:
        return GitDecision(False, f"git {sub} denied (remote mutation / merge — a human merges the MR)")
    if sub == "reset":
        # --hard/--merge/--keep all rewrite the WORKING TREE to a commit; allow
        # only back to the recorded base SHA. Soft/mixed reset (HEAD/index only)
        # is local and free.
        if any(f in rest for f in ("--hard", "--merge", "--keep")):
            targets = [a for a in rest if not a.startswith("-")]
            if targets and all(t == policy.base_sha for t in targets):
                return GitDecision(True, "tree reset to the recorded base sha")
            return GitDecision(False, "destructive reset only to the recorded base_commit_sha")
        return GitDecision(True, "local reset (soft/mixed)")
    if sub == "clean":
        return GitDecision(True, "local clean")
    if sub in _LOCAL_OK:
        return GitDecision(True, f"local {sub}")
    return GitDecision(False, f"git {sub} not on the allowlist")


class GitGuardDenied(PermissionError):
    """Raised (or surfaced as an is_error tool_result) when the guard denies a git op."""


def enforce(argv: list[str]) -> None:
    """Enforce the active policy on a git argv. No-op when no policy is set
    (legacy callers). Raises :class:`GitGuardDenied` on a denied op."""
    policy = _policy.get()
    if policy is None:
        return
    decision = classify_git(argv, policy)
    if not decision.allowed:
        raise GitGuardDenied(decision.reason)
