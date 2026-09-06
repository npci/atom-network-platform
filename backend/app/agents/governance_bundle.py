# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Governance skill BUNDLES — safe archive handling, classification, safety gate.

A bundle is the industry Agent-Skill shape (Claude Code / grok / Codex): a
directory with SKILL.md + scripts + references + assets, uploaded as one
.zip/.tar.gz and stored verbatim as an immutable version. This module is the
pure layer: extraction safety, file classification, the static safety gate,
and exec-manifest validation. No DB, no execution (see governance_sandbox).

Security model (docs/designs/governance-skill-execution.md §4):
- Extraction is TRAVERSAL-SAFE: absolute paths, ``..`` components, symlinks,
  hardlinks and special files are rejected — an archive member can never write
  outside the extraction root.
- The STATIC SAFETY GATE screens every script before a bundle can be stored:
  hard violations (privilege escalation, docker/host access, download-and-
  execute, reading secret-shaped env vars) REJECT the upload; capability
  warnings (network-capable imports, absolute-path writes) are recorded on the
  manifest and surfaced to the admin — the sandbox is the runtime control.
- Scripts NEVER gate a review unless the exec manifest explicitly declares
  them ``role: validator`` — undeclared scripts default to ``generator``
  (context/artifacts only). A validator's findings are parsed per its declared
  contract (``findings_parse``), never inferred from exit codes: the forensic
  probe found a real validator that exits 0 even when it finds secrets.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shlex
import tarfile
import zipfile
from dataclasses import dataclass, field

# ── Limits ────────────────────────────────────────────────────────────────────
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024        # compressed upload cap
MAX_EXTRACTED_BYTES = 200 * 1024 * 1024     # zip-bomb guard (total uncompressed)
MAX_MEMBER_BYTES = 20 * 1024 * 1024         # single-file cap
MAX_MEMBERS = 2000
MAX_SKILL_MD_BYTES = 256 * 1024             # same ceiling as the single-doc path

_SCRIPT_EXT = {".py", ".sh", ".bash", ".rb", ".js", ".ts", ".pl", ".ps1"}
_ASSET_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".bin", ".woff", ".woff2"}
_DATA_EXT = {".csv", ".tsv", ".jsonl", ".ndjson", ".parquet", ".db", ".sqlite"}
_SCHEMA_EXT = {".xsd", ".proto", ".avsc"}
_DEP_MANIFESTS = {"requirements.txt", "requirements.in", "pyproject.toml", "setup.py",
                  "package.json", "go.mod", "gemfile", "cargo.toml", "pom.xml"}
_LOCKFILES = {"requirements.lock", "poetry.lock", "package-lock.json", "yarn.lock",
              "go.sum", "gemfile.lock", "cargo.lock", "uv.lock"}
_CI_NAMES = {".gitlab-ci.yml", ".gitlab-ci.yaml", "makefile", "justfile", "dockerfile"}
_SCANNER_CONF_HINTS = ("semgrep", "gitleaks", "bandit", "trivy", "checkov", "rules",
                       "queries", "patterns", "checkmarx")


@dataclass
class BundleFile:
    path: str
    size: int
    sha256: str
    classification: str
    content: bytes = field(repr=False, default=b"")

    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


@dataclass
class ParsedBundle:
    files: list[BundleFile]
    skill_md_path: str
    skill_md_text: str
    root_prefix: str                      # common leading dir stripped for display, "" if none
    warnings: list[dict]                  # capability warnings (recorded, not rejecting)

    @property
    def scripts(self) -> list[BundleFile]:
        return [f for f in self.files if f.classification == "script"]

    def manifest(self) -> list[dict]:
        return [{"path": f.path, "bytes": f.size, "sha256": f.sha256,
                 "classification": f.classification} for f in self.files]


class BundleError(ValueError):
    """Any reason the bundle cannot be accepted — always with the reason(s)."""


# ── Safe extraction ───────────────────────────────────────────────────────────

def _reject_member_name(name: str) -> str | None:
    if not name or name.startswith("/") or name.startswith("\\"):
        return "absolute path"
    parts = name.replace("\\", "/").split("/")
    if ".." in parts:
        return "path traversal ('..')"
    if any(p in (".git",) for p in parts):
        return ".git content"
    return None


def _extract(archive: bytes, filename: str) -> list[tuple[str, bytes]]:
    """Extract to MEMORY with the traversal/zip-bomb guards. Returns (path, bytes)
    per regular file; anything unsafe raises BundleError naming every offender."""
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise BundleError(f"archive exceeds {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB")
    problems: list[str] = []
    out: list[tuple[str, bytes]] = []
    total = 0
    lower = (filename or "").lower()
    try:
        if lower.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(archive)) as zf:
                infos = zf.infolist()
                if len(infos) > MAX_MEMBERS:
                    raise BundleError(f"archive has more than {MAX_MEMBERS} members")
                for zi in infos:
                    if zi.is_dir():
                        continue
                    why = _reject_member_name(zi.filename)
                    if why:
                        problems.append(f"{zi.filename!r}: {why}")
                        continue
                    if zi.file_size > MAX_MEMBER_BYTES:
                        problems.append(f"{zi.filename!r}: file exceeds member cap")
                        continue
                    # zipfile has no symlink extraction by default read(); external_attr
                    # symlinks decode as their target text — treat mode bits as the signal.
                    if (zi.external_attr >> 16) & 0o170000 == 0o120000:
                        problems.append(f"{zi.filename!r}: symlink")
                        continue
                    data = zf.read(zi)
                    total += len(data)
                    if total > MAX_EXTRACTED_BYTES:
                        raise BundleError("extracted size exceeds the zip-bomb guard")
                    out.append((zi.filename.replace("\\", "/"), data))
        elif lower.endswith((".tar.gz", ".tgz", ".tar")):
            mode = "r:gz" if lower.endswith(("gz", "tgz")) else "r:"
            with tarfile.open(fileobj=io.BytesIO(archive), mode=mode) as tf:
                members = tf.getmembers()
                if len(members) > MAX_MEMBERS:
                    raise BundleError(f"archive has more than {MAX_MEMBERS} members")
                for m in members:
                    if m.isdir():
                        continue
                    why = _reject_member_name(m.name)
                    if why:
                        problems.append(f"{m.name!r}: {why}")
                        continue
                    if not m.isreg():
                        problems.append(f"{m.name!r}: not a regular file "
                                        f"(symlink/hardlink/device are rejected)")
                        continue
                    if m.size > MAX_MEMBER_BYTES:
                        problems.append(f"{m.name!r}: file exceeds member cap")
                        continue
                    fobj = tf.extractfile(m)
                    data = fobj.read() if fobj else b""
                    total += len(data)
                    if total > MAX_EXTRACTED_BYTES:
                        raise BundleError("extracted size exceeds the zip-bomb guard")
                    out.append((m.name, data))
        else:
            raise BundleError("unsupported archive type — upload .zip or .tar.gz")
    except (zipfile.BadZipFile, tarfile.TarError) as e:
        raise BundleError(f"archive is corrupt or unreadable: {e}") from e
    if problems:
        raise BundleError("unsafe archive members: " + "; ".join(problems[:10]))
    if not out:
        raise BundleError("archive contains no files")
    return out


def _strip_common_root(items: list[tuple[str, bytes]]) -> tuple[str, list[tuple[str, bytes]]]:
    """`skill/SKILL.md` style single-root archives are normalised to root-relative."""
    roots = {p.split("/", 1)[0] for p, _ in items}
    if len(roots) == 1 and all("/" in p for p, _ in items):
        root = next(iter(roots))
        return root, [(p.split("/", 1)[1], d) for p, d in items]
    return "", items


# ── Classification (the forensic-probe taxonomy) ──────────────────────────────

def classify_file(path: str) -> str:
    p = path.lower()
    base = p.rsplit("/", 1)[-1]
    ext = "." + base.rsplit(".", 1)[-1] if "." in base else ""
    if base == "skill.md":
        return "skill_manifest"
    if base in _CI_NAMES:
        return "ci_config"
    if base in _LOCKFILES:
        return "lockfile"
    if base in _DEP_MANIFESTS:
        return "dependency_manifest"
    # Fixture dirs BEFORE the script-extension check: a .py under evals/fixtures is
    # sample input (often deliberately containing fake secrets), not an executable —
    # classifying it 'script' would make the smoke gate try to RUN test fixtures.
    if any(seg in ("fixtures", "fixture", "evals", "testdata", "test_data", "samples")
           for seg in p.split("/")):
        return "fixture"
    if ext in _SCRIPT_EXT:
        return "script"
    if ext in _SCHEMA_EXT:
        return "schema"
    if ext in _ASSET_EXT:
        return "asset"
    # Rules-as-data (semgrep ymls, the org's Checkmarx CSV catalog, PATTERNS tables):
    # csv included — the forensic probe's 21,862-row catalog is a .csv.
    if ext in (".yml", ".yaml", ".toml", ".json", ".csv") and any(h in p for h in _SCANNER_CONF_HINTS):
        return "scanner_rule_config"
    if ext in _DATA_EXT:
        return "data"
    if ext in (".md", ".rst", ".txt", ".adoc"):
        return "rulebook_prose"
    return "other"


# ── Static safety gate ────────────────────────────────────────────────────────
# Hard violations reject the upload; warnings ride on the manifest for the admin.
_HARD_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("privilege_escalation", re.compile(r"\bsudo\b|\bsetuid\b|\bos\.setuid|\bchroot\b")),
    ("docker_host_access", re.compile(r"/var/run/docker\.sock|\bdocker\s+(run|exec|build)\b")),
    ("download_and_execute", re.compile(
        r"(curl|wget)[^\n|]*\|\s*(ba)?sh|pip\s+install\s+https?://|"
        r"eval\s*\(\s*(requests|urllib|httpx)|exec\s*\(\s*(requests|urllib|httpx)")),
    ("secret_env_read", re.compile(
        r"os\.(environ|getenv)\s*[\[(]\s*['\"][A-Z0-9_]*(TOKEN|SECRET|PASSWORD|APIKEY|API_KEY|CREDENTIAL)")),
    ("host_mount", re.compile(r"-v\s+/(etc|root|home|var)\S*:")),
]
_WARN_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("network_capable", re.compile(
        r"^\s*(import|from)\s+(requests|httpx|aiohttp|urllib|http\.client|socket|ftplib|smtplib)\b"
        r"|subprocess[^\n]*(curl|wget)", re.MULTILINE)),
    ("absolute_path_write", re.compile(r"open\(\s*['\"]/(?!tmp/|dev/null)")),
]


def static_safety_scan(files: list[BundleFile]) -> tuple[list[dict], list[dict]]:
    """(violations, warnings) over every script + ci_config file. Violations name
    the category, path and first offending line — the upload is rejected with them."""
    violations, warnings = [], []
    for f in files:
        if f.classification not in ("script", "ci_config"):
            continue
        text = f.text()
        for cat, rx in _HARD_PATTERNS:
            m = rx.search(text)
            if m:
                line = text[: m.start()].count("\n") + 1
                violations.append({"category": cat, "path": f.path, "line": line,
                                   "excerpt": text.splitlines()[line - 1][:120]})
        for cat, rx in _WARN_PATTERNS:
            m = rx.search(text)
            if m:
                line = text[: m.start()].count("\n") + 1
                warnings.append({"category": cat, "path": f.path, "line": line,
                                 "excerpt": text.splitlines()[line - 1][:120]})
    return violations, warnings


# ── Exec manifest (per-script execution contract) ─────────────────────────────
_ROLES = ("validator", "generator")
_OUTPUT_FORMATS = ("json_stdout", "json_file", "sarif", "junit", "xml", "exit_code", "freetext")

# Interpreters an exec-manifest `invocation` may name as argv[0]. Matched on the
# BASENAME so "/usr/bin/python3" and "python3" are the same entry.
_INVOCATION_INTERPRETERS = frozenset({"python", "python3", "node", "bash", "sh"})


def _validated_invocation(invocation: str, script_path: str, scripts: set[str],
                          problems: list[str]) -> str:
    """Check that `invocation` runs a DECLARED script under a known interpreter.

    This is the gate that was missing. `invocation` arrives as a free-form string
    in the ``exec_manifest`` form field — it is not a file, so `static_safety_scan`
    (which classifies and scans bundle FILES) never sees it. `governance_sandbox.
    _render_invocation` then `shlex.split`s it into argv, so argv[0] was entirely
    caller-chosen; on a host with no Docker daemon `run_script` executes that argv
    directly, with only rlimits between it and the backend container.

    `run_script`'s docstring justifies its non-Docker fallback on the grounds that
    it "executes only DECLARED scripts — they pass the upload static gate". That
    was true of the scripts and false of the invocation. This restores the
    invariant the docstring already claims.
    """
    try:
        argv = shlex.split(invocation)
    except ValueError as e:                      # unbalanced quotes
        problems.append(f"{script_path}: invocation is not parseable as a command ({e})")
        return invocation
    if not argv:
        problems.append(f"{script_path}: invocation is empty")
        return invocation

    interp = argv[0].rsplit("/", 1)[-1]
    if interp not in _INVOCATION_INTERPRETERS:
        problems.append(
            f"{script_path}: invocation must start with one of "
            f"{sorted(_INVOCATION_INTERPRETERS)} — got {argv[0]!r}")
        return invocation

    # argv[1] must be a script the bundle actually declares. Placeholders are
    # substituted later ({target}/{output}/{bundle}), so a placeholder in this
    # position would smuggle an arbitrary path past the check.
    if len(argv) < 2:
        problems.append(f"{script_path}: invocation names no script to run")
        return invocation
    target_script = argv[1].replace("\\", "/")
    if target_script not in scripts:
        problems.append(
            f"{script_path}: invocation runs {target_script!r}, which is not a script "
            "in this bundle")
    return invocation


def exec_contract_from_frontmatter(skill_md_text: str) -> dict | None:
    """Read the execution contract from SKILL.md's OWN YAML frontmatter, so a
    standard Agent-Skill bundle is self-describing and needs no separate,
    platform-specific manifest file.

    A vanilla Claude/grok/Codex SKILL.md is ``name`` + ``description`` + a free
    ``metadata`` map; we read the contract from ``metadata.governance`` (the
    spec's documented extensibility point — other runtimes ignore unknown
    metadata), or a top-level ``governance`` / ``x-governance`` key. Returns the
    ``{scripts:[...]}`` dict for :func:`validate_exec_manifest`, or ``None`` when
    absent or unparseable — in which case the bundle still runs, agent-driven
    (the ordinary universal-standard behaviour), just without a deterministic
    floor. Malformed frontmatter never rejects the upload here; the structural
    SKILL.md parser owns that.
    """
    if not skill_md_text.startswith("---"):
        return None
    m = re.search(r"^---\s*$", skill_md_text[3:], re.MULTILINE)
    if not m:
        return None
    block = skill_md_text[3:3 + m.start()]
    try:
        import yaml
        fm = yaml.safe_load(block)
    except Exception:  # noqa: BLE001 — malformed frontmatter is tolerated, not fatal
        return None
    if not isinstance(fm, dict):
        return None
    meta = fm.get("metadata")
    gov = (meta.get("governance") if isinstance(meta, dict) else None) \
        or fm.get("governance") or fm.get("x-governance")
    if not isinstance(gov, dict) or not gov.get("scripts"):
        return None
    out = {"scripts": gov.get("scripts") or []}
    if isinstance(gov.get("smoke"), dict):
        out["smoke"] = gov["smoke"]        # bundle-level prove-it-runs fixtures
    return out


def validate_exec_manifest(exec_manifest: dict | None, bundle: "ParsedBundle") -> dict:
    """Normalise + validate the declared per-script contracts. Undeclared scripts
    default to role=generator (they can NEVER gate). Loud on: unknown script path,
    a validator without a findings contract, unknown role/format."""
    scripts = {f.path for f in bundle.scripts}
    all_paths = {f.path for f in bundle.files}
    out = {"scripts": []}
    problems: list[str] = []
    declared: set[str] = set()

    def _fixture(sm, ctx):
        """Validate a per-script/bundle smoke block: bad/good must name a bundle
        path (a file, or a directory prefix of one). Returns the cleaned block."""
        if sm is None:
            return None
        if not isinstance(sm, dict):
            problems.append(f"{ctx}: smoke must be a mapping")
            return None
        clean = {}
        for key in ("bad", "good"):
            p = sm.get(key)
            if p is None:
                continue
            p = str(p).replace("\\", "/").rstrip("/")
            if p not in all_paths and not any(f == p or f.startswith(p + "/") for f in all_paths):
                problems.append(f"{ctx}: smoke.{key} path {p!r} is not in the bundle")
                continue
            clean[key] = p
        if "expect_bad_min" in sm:
            try:
                clean["expect_bad_min"] = max(1, int(sm["expect_bad_min"]))
            except (TypeError, ValueError):
                problems.append(f"{ctx}: smoke.expect_bad_min must be an integer")
        return clean or None
    for s in ((exec_manifest or {}).get("scripts") or []):
        path = (s.get("path") or "").replace("\\", "/")
        if path not in scripts:
            problems.append(f"exec manifest names a script not in the bundle: {path!r}")
            continue
        role = s.get("role") or "generator"
        if role not in _ROLES:
            problems.append(f"{path}: unknown role {role!r} (validator|generator)")
            continue
        fmt = s.get("output_format") or "exit_code"
        if fmt not in _OUTPUT_FORMATS:
            problems.append(f"{path}: unknown output_format {fmt!r}")
            continue
        if role == "validator" and fmt != "exit_code" and not s.get("findings_parse"):
            # The probe's trap: a validator that exits 0 with findings in stdout JSON.
            # A structured-output validator MUST say where its findings live.
            problems.append(f"{path}: validator with output_format={fmt} needs findings_parse "
                            "(e.g. 'stdout.json.total_findings') — exit codes are not trusted")
        # scope: how the validator floor targets this script. "repo" (default) runs
        # it once per selected repo; "change" runs it ONCE against the merged
        # changed-files of the whole change — for report-graders whose artifact
        # (e.g. sa_review_report.json) is change-level, not per-repo, so a
        # multi-repo change doesn't yield one REPORT-MISSING per repo.
        scope = s.get("scope") or "repo"
        if scope not in ("repo", "change"):
            problems.append(f"{path}: unknown scope {scope!r} (repo|change)")
            continue
        declared.add(path)
        out["scripts"].append({
            "path": path, "role": role, "scope": scope,
            "invocation": _validated_invocation(
                s.get("invocation") or f"python3 {path} {{target}}",
                path, scripts, problems),
            "timeout_seconds": min(int(s.get("timeout_seconds") or 300), 1800),
            "output_format": fmt,
            "findings_parse": s.get("findings_parse"),
            "exit_semantics": s.get("exit_semantics") or "0=ok nonzero=error",
            "normalize": s.get("normalize") or [],
            "network": bool((s.get("network") or {}).get("needed")),
            "smoke": _fixture(s.get("smoke"), path),   # per-script fixture override
        })
    for path in sorted(scripts - declared):
        out["scripts"].append({"path": path, "role": "generator", "scope": "repo",
                               "invocation": f"python3 {path} {{target}}",
                               "timeout_seconds": 300, "output_format": "exit_code",
                               "findings_parse": None, "exit_semantics": "0=ok nonzero=error",
                               "normalize": [], "network": False, "smoke": None})
    if any(s["network"] for s in out["scripts"]):
        problems.append("a script declares network.needed=true — network egress is not "
                        "permitted for governance skill scripts (design §4)")
    bundle_smoke = _fixture((exec_manifest or {}).get("smoke"), "bundle smoke")
    if bundle_smoke:
        out["smoke"] = bundle_smoke                # applies to scripts with no own smoke block
    if problems:
        raise BundleError("; ".join(problems))
    return out


# ── Top-level parse ───────────────────────────────────────────────────────────

def parse_bundle(archive: bytes, filename: str) -> ParsedBundle:
    root, items = _strip_common_root(_extract(archive, filename))
    files: list[BundleFile] = []
    for path, data in sorted(items):
        files.append(BundleFile(path=path, size=len(data),
                                sha256=hashlib.sha256(data).hexdigest(),
                                classification=classify_file(path), content=data))
    skill_mds = [f for f in files if f.classification == "skill_manifest"]
    if not skill_mds:
        raise BundleError("no SKILL.md found — every skill bundle needs one at its root")
    # Prefer the shallowest SKILL.md (bundle root); nested ones are references.
    skill_md = sorted(skill_mds, key=lambda f: f.path.count("/"))[0]
    if skill_md.size > MAX_SKILL_MD_BYTES:
        raise BundleError(f"SKILL.md exceeds {MAX_SKILL_MD_BYTES // 1024} KB")
    try:
        skill_text = skill_md.content.decode("utf-8")
    except UnicodeDecodeError as e:
        raise BundleError(f"SKILL.md is not valid UTF-8: {e}") from e
    violations, warnings = static_safety_scan(files)
    if violations:
        raise BundleError("static safety gate: " + "; ".join(
            f"{v['category']} at {v['path']}:{v['line']} ({v['excerpt']!r})"
            for v in violations[:8]))
    return ParsedBundle(files=files, skill_md_path=skill_md.path,
                        skill_md_text=skill_text, root_prefix=root, warnings=warnings)


def bundle_sha256(archive: bytes) -> str:
    return hashlib.sha256(archive).hexdigest()
