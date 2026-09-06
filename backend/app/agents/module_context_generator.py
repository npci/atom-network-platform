# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Index-time module-wise context generation — "the heart" (THE BOOK §19).

When the code index is built, also write a per-module knowledge file
(module → submodule → sub-submodule) so the agent ORIENTS instead of looking in
the wrong place. The map is high-level and **orientation-only** — concrete
details are always re-derived via grep/read against the clone, so a coarse or
slightly-stale map degrades gracefully.

This module produces the DETERMINISTIC backbone (Maven module tree + java
version + intra-repo deps + key types from each module's own sources). The
``functional_flow`` narrative is an OPTIONAL cheap-LLM pass (``summarize``
callback) layered on top — absent it, a deterministic summary is stored, so the
generator is fully usable (and testable) without an LLM.

Gated by ``use_module_context_generation`` (default off). Idempotent per repo.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from lxml import etree

logger = logging.getLogger("app.agentic")

# Dirs never worth scanning for module poms (build output / VCS / tooling).
_PRUNE_DIRS = {".git", "target", "node_modules", "build", ".idea", ".gradle", "dist"}

_RE_CLASS = re.compile(r"\b(?:public\s+|final\s+|abstract\s+)*(class|interface|enum|record)\s+(\w+)")
_KEY_TYPE_CAP = 30
_ENTRY_CAP = 25

# Deterministic entry-point signals (§19) — annotations that mark a module's
# externally-reachable surface. Pure pattern match over the module's own sources.
_RE_ENTRY = re.compile(
    r"@(RestController|Controller|GetMapping|PostMapping|PutMapping|DeleteMapping|"
    r"RequestMapping|XmlRootElement|Scheduled|KafkaListener|RabbitListener|EventListener)\b"
    r"[^\n]*\n\s*(?:@[\w.]+[^\n]*\n\s*)*"               # skip any further annotations
    r"(?:public\s+|private\s+|protected\s+|static\s+|final\s+|abstract\s+|<[^>]+>\s*|[\w.\[\]]+\s+)*"
    r"(\w+)\s*[\(<]?")                                   # the annotated method/class name


def _parse_pom(path: Path):
    try:
        return etree.fromstring(path.read_bytes(),
                                etree.XMLParser(resolve_entities=False, recover=True))
    except Exception:  # noqa: BLE001
        return None


def _local(el) -> str:
    # Comment / processing-instruction nodes have a callable `.tag` (not a str);
    # QName() rejects them. Treat them as nameless so iteration stays safe — real
    # poms routinely carry comments inside <modules>, <dependencies>, etc.
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    return etree.QName(tag).localname


def _child_text(root, name: str) -> str | None:
    for c in root:
        if _local(c) == name and c.text:
            return c.text.strip()
    return None


def _modules(root) -> list[str]:
    out: list[str] = []
    for el in root.iter():
        if _local(el) == "modules":
            out += [m.text.strip() for m in el if _local(m) == "module" and m.text]
    return out


def _java_version(root) -> str | None:
    # properties: maven.compiler.release/source/target or java.version
    keys = ("maven.compiler.release", "maven.compiler.source", "maven.compiler.target", "java.version")
    for el in root.iter():
        if _local(el) in keys and el.text and el.text.strip():
            return el.text.strip()
    return None


def _dependencies(root) -> list[str]:
    deps: list[str] = []
    for el in root.iter():
        if _local(el) == "dependency":
            aid = _child_text(el, "artifactId")
            if aid:
                deps.append(aid)
    return sorted(set(deps))


def parse_pom_modules(repo_dir: Path) -> list[dict]:
    """One dict per Maven module: ``{module_path, artifact_id, parent_module_path,
    depth, java_version, depends_on}``.

    Modules are discovered by scanning the tree for ``pom.xml`` files, NOT by
    following ``<modules>`` declarations: real repos sometimes declare module
    paths that don't match the on-disk layout (e.g. a root pom that lists
    ``common-utils`` while the pom actually lives at ``deps/common-utils``), and a
    non-resolving declaration would silently drop that whole module's context.
    Parent/depth are derived from directory nesting among the discovered poms.
    Returns ``[]`` for a non-Maven repo (no pom.xml anywhere)."""
    repo_dir = Path(repo_dir)
    pom_dirs: list[str] = []
    for dirpath, dirnames, filenames in os.walk(repo_dir):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        if "pom.xml" in filenames:
            rel = os.path.relpath(dirpath, repo_dir)
            pom_dirs.append("." if rel == "." else rel.replace(os.sep, "/"))
    pom_set = set(pom_dirs)

    def _parent_of(rel: str) -> str | None:
        if rel == ".":
            return None
        parts = rel.split("/")
        for i in range(len(parts) - 1, 0, -1):
            cand = "/".join(parts[:i])
            if cand in pom_set:
                return cand
        return "." if "." in pom_set else None

    def _depth(rel: str) -> int:
        d, cur = 0, _parent_of(rel)
        while cur is not None:
            d, cur = d + 1, _parent_of(cur)
        return d

    out: list[dict] = []
    for rel in sorted(pom_dirs):
        pom = (repo_dir / "pom.xml") if rel == "." else (repo_dir / rel / "pom.xml")
        root = _parse_pom(pom)
        if root is None:
            continue
        out.append({
            "module_path": rel,
            "artifact_id": _child_text(root, "artifactId"),
            "parent_module_path": _parent_of(rel),
            "depth": _depth(rel),
            "java_version": _java_version(root),
            "depends_on": _dependencies(root),
        })
    return out


# ── Multi-language module discovery (Maven first-class; Rust first-class for the
#    ongoing Rust migration; Gradle/Node/Python/Go get name+deps orientation). ──────
# A dir with several build files takes the FIRST matching type (priority order).
_BUILD_FILES: list[tuple[str, str]] = [
    ("pom.xml", "maven"),
    ("Cargo.toml", "rust"),
    ("build.gradle", "gradle"), ("build.gradle.kts", "gradle"),
    ("package.json", "node"),
    ("pyproject.toml", "python"),
    ("go.mod", "go"),
]

# Rust: public surface + entry points (deterministic pattern match, like Java's).
_RE_RUST_TYPE = re.compile(r"^\s*pub\s+(struct|enum|trait)\s+(\w+)", re.M)
_RE_RUST_ENTRY = re.compile(
    r"#\[(get|post|put|delete|patch|tokio::main|actix_web::main|axum::debug_handler)"
    r"[^\]]*\]\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)"
    r"|(?:pub\s+)?(?:async\s+)?fn\s+(main)\s*\(")


def _parse_cargo(path: Path) -> dict:
    """Cargo.toml → {name, version_label, deps}. A virtual workspace root (no
    [package]) is named '<dir> (workspace)'. Uses stdlib tomllib (py3.11+)."""
    import tomllib
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 — malformed toml → name-only orientation
        data = {}
    pkg = data.get("package") or {}
    name = pkg.get("name") or (f"{path.parent.name or 'root'} (workspace)"
                               if "workspace" in data else path.parent.name)
    edition = pkg.get("edition")
    deps = sorted((data.get("dependencies") or {}).keys())
    return {"name": name, "version_label": f"rust {edition}" if edition else "rust",
            "deps": deps}


def _parse_other(path: Path, lang: str) -> dict:
    """Name/version/deps for gradle/node/python/go — orientation-grade only."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    if lang == "node":
        import json as _json
        try:
            data = _json.loads(text)
        except ValueError:
            data = {}
        return {"name": data.get("name") or path.parent.name,
                "version_label": ("node " + str((data.get("engines") or {}).get("node", ""))).strip(),
                "deps": sorted((data.get("dependencies") or {}).keys())}
    if lang == "python":
        import tomllib
        try:
            data = tomllib.loads(text)
        except Exception:  # noqa: BLE001
            data = {}
        proj = data.get("project") or {}
        return {"name": proj.get("name") or path.parent.name,
                "version_label": ("python " + str(proj.get("requires-python", ""))).strip(),
                "deps": sorted(str(d).split()[0].split(">=")[0].split("==")[0]
                               for d in (proj.get("dependencies") or []))}
    if lang == "go":
        mod = re.search(r"^module\s+(\S+)", text, re.M)
        ver = re.search(r"^go\s+([\d.]+)", text, re.M)
        return {"name": (mod.group(1).rsplit("/", 1)[-1] if mod else path.parent.name),
                "version_label": f"go {ver.group(1)}" if ver else "go", "deps": []}
    # gradle: project name from dir; intra-repo deps from project(':x') refs.
    deps = sorted({m.group(1) for m in re.finditer(r"project\(['\"]:([\w-]+)['\"]\)", text)})
    ver = re.search(r"sourceCompatibility\s*=?\s*['\"]?([\w.]+)", text)
    return {"name": path.parent.name, "version_label": f"java {ver.group(1)}" if ver else None,
            "deps": deps}


def discover_modules(repo_dir: Path) -> list[dict]:
    """One dict per module dir containing ANY known build file (pom.xml, Cargo.toml,
    build.gradle(.kts), package.json, pyproject.toml, go.mod) — same shape as
    ``parse_pom_modules`` plus ``lang``. Filesystem-driven (never trusts declared
    module lists); parent/depth from directory nesting across ALL discovered modules,
    so a mixed Java+Rust repo gets one coherent tree."""
    repo_dir = Path(repo_dir)
    found: dict[str, str] = {}                      # rel dir -> lang
    for dirpath, dirnames, filenames in os.walk(repo_dir):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        names = set(filenames)
        for fname, lang in _BUILD_FILES:
            if fname in names:
                rel = os.path.relpath(dirpath, repo_dir)
                found.setdefault("." if rel == "." else rel.replace(os.sep, "/"), lang)
                break
    mod_set = set(found)

    def _parent_of(rel: str) -> str | None:
        if rel == ".":
            return None
        parts = rel.split("/")
        for i in range(len(parts) - 1, 0, -1):
            cand = "/".join(parts[:i])
            if cand in mod_set:
                return cand
        return "." if "." in mod_set else None

    def _depth(rel: str) -> int:
        d, cur = 0, _parent_of(rel)
        while cur is not None:
            d, cur = d + 1, _parent_of(cur)
        return d

    out: list[dict] = []
    for rel in sorted(found):
        lang = found[rel]
        base = repo_dir if rel == "." else repo_dir / rel
        if lang == "maven":
            root = _parse_pom(base / "pom.xml")
            if root is None:
                continue
            name, ver, deps = _child_text(root, "artifactId"), _java_version(root), _dependencies(root)
        elif lang == "rust":
            p = _parse_cargo(base / "Cargo.toml")
            name, ver, deps = p["name"], p["version_label"], p["deps"]
        else:
            fname = next(f for f, lg in _BUILD_FILES if lg == lang and (base / f).is_file())
            p = _parse_other(base / fname, lang)
            name, ver, deps = p["name"], p["version_label"], p["deps"]
        out.append({"module_path": rel, "artifact_id": name,
                    "parent_module_path": _parent_of(rel), "depth": _depth(rel),
                    "java_version": (ver or None) and str(ver)[:20], "depends_on": deps,
                    "lang": lang})
    return out


def _key_types(repo_dir: Path, module_path: str, lang: str = "maven") -> list[dict]:
    """Top-level types declared in this module's OWN ``src``, as ``{name, kind, file}``
    so the map is a JUMP TARGET (symbol → file) the agent reads directly instead of
    grepping for the name. Submodules have their own dirs, so we don't pull their types
    up. Java + Rust supported."""
    base = repo_dir if module_path == "." else repo_dir / module_path
    src = base / "src"
    if not src.is_dir():
        return []
    types: list[dict] = []
    if lang == "rust":
        for rf in sorted(src.rglob("*.rs")):
            try:
                text = rf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = rf.relative_to(base).as_posix()
            for kind, name in _RE_RUST_TYPE.findall(text):
                types.append({"name": name, "kind": kind, "file": rel})
                if len(types) >= _KEY_TYPE_CAP:
                    return types
        return types
    for jf in sorted(src.rglob("*.java")):
        try:
            text = jf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # First REAL type declaration. _RE_CLASS also matches prose inside comments/javadoc
        # ("the interface for the HSM" → captures 'for'), and a bare search() lets that prose
        # SHADOW the real class. Iterate and take the first match whose name is PascalCase — a
        # Java type is always Capitalized, while keywords/prose ('for', 'to', 'the') are not —
        # so keyword/prose noise never lands as a key type.
        name = kind = None
        for mm in _RE_CLASS.finditer(text):
            cand = mm.group(2)
            if cand[:1].isupper():
                name, kind = cand, mm.group(1)
                break
        if name:
            types.append({"name": name, "kind": kind,
                          "file": jf.relative_to(base).as_posix()})
        if len(types) >= _KEY_TYPE_CAP:
            break
    return types


def _entry_points(repo_dir: Path, module_path: str, lang: str = "maven") -> list[dict]:
    """Externally-reachable surface of this module — annotated handlers / scheduled
    jobs / JAXB roots (Java), or main/handler fns (Rust: actix/axum/tokio) — from its
    OWN src (deterministic, ground truth)."""
    base = repo_dir if module_path == "." else repo_dir / module_path
    src = base / "src"
    if not src.is_dir():
        return []
    out: list[dict] = []
    if lang == "rust":
        for rf in sorted(src.rglob("*.rs")):
            try:
                text = rf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = rf.relative_to(base).as_posix()
            for m in _RE_RUST_ENTRY.finditer(text):
                kind = m.group(1) or "main"
                name = m.group(2) or m.group(3)
                out.append({"kind": kind, "name": name, "file": rel})
                if len(out) >= _ENTRY_CAP:
                    return out
        return out
    for jf in sorted(src.rglob("*.java")):
        try:
            text = jf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = jf.relative_to(base).as_posix()
        for m in _RE_ENTRY.finditer(text):
            out.append({"kind": m.group(1), "name": m.group(2), "file": rel})
            if len(out) >= _ENTRY_CAP:
                return out
    return out


_LANG_LABEL = {"maven": "Maven module", "rust": "Rust crate", "gradle": "Gradle module",
               "node": "Node package", "python": "Python package", "go": "Go module"}


def _deterministic_summary(mod: dict, key_types: list[str]) -> str:
    label = _LANG_LABEL.get(mod.get("lang", "maven"), "Module")
    parts = [f"{label} '{mod['artifact_id'] or mod['module_path']}'"]
    if mod["java_version"]:
        # Label the version — otherwise it renders as a bare number ("...; 21; ...") that
        # reads as noise. `java_version` is Java's release (e.g. 21) for maven/gradle modules.
        ver = mod["java_version"]
        parts.append(f"Java {ver}" if mod.get("lang", "maven") in ("maven", "gradle") else str(ver))
    if key_types:
        parts.append("key types: " + ", ".join(t["name"] for t in key_types[:10]))
    if mod["depends_on"]:
        parts.append(f"{len(mod['depends_on'])} declared dependencies")
    return "; ".join(parts) + ". Orientation only — verify with read_file."


def generate_module_context(db, repo_id: str, repo_dir: Path, base_commit_sha: str | None = None,
                            *, summarize=None) -> int:
    """Build + persist ``module_context`` rows for a repo (idempotent rebuild).

    ``summarize(mod, key_types) -> str`` is an optional cheap-LLM hook for the
    ``functional_flow`` narrative (§19); when None, a deterministic summary is
    stored. Returns the number of module rows written. Fail-soft per module."""
    from app.models.module_context import ModuleContext

    modules = discover_modules(Path(repo_dir))     # Maven + Rust + gradle/node/python/go
    db.query(ModuleContext).filter(ModuleContext.repo_id == repo_id).delete(synchronize_session=False)

    written = 0
    for mod in modules:
        try:
            lang = mod.get("lang", "maven")
            kt = _key_types(Path(repo_dir), mod["module_path"], lang)
            eps = _entry_points(Path(repo_dir), mod["module_path"], lang)
            summary = _deterministic_summary(mod, kt)
            flow = None
            if summarize is not None:
                try:
                    flow = summarize(mod, kt, eps)      # LLM narrative (low-authority)
                except Exception as e:  # noqa: BLE001 — LLM hiccup never fails indexing
                    logger.debug("module flow summarize failed for %s: %s", mod["module_path"], e)
            db.add(ModuleContext(
                repo_id=repo_id, module_path=mod["module_path"],
                parent_module_path=mod["parent_module_path"], depth=mod["depth"],
                summary=summary, key_types=kt, entry_points=eps, functional_flow=flow,
                java_version=mod["java_version"], depends_on=mod["depends_on"],
                base_commit_sha=base_commit_sha,
            ))
            written += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("module_context row failed for %s: %s", mod.get("module_path"), e)
    db.flush()
    return written


def _llm_functional_flow(repo_dir: Path):
    """Build the opt-in cheap-LLM ``summarize(mod, key_types, entry_points) -> str``
    callback (§19). The narrative is LOW-AUTHORITY orientation — the assembler
    labels it "verify with read" and the read-before-edit guard still applies, so
    a wrong summary can orient but never substitute for reading the code."""
    def summarize(mod: dict, key_types: list[str], entry_points: list[dict]) -> str | None:
        from app.core.llm import call_llm
        eps = ", ".join(f"{e['kind']}:{e['name']}" for e in entry_points[:10]) or "none detected"
        label = _LANG_LABEL.get(mod.get("lang", "maven"), "Module")
        prompt = (
            f"{label} '{mod.get('artifact_id') or mod['module_path']}' ({mod.get('java_version') or 'version n/a'}).\n"
            f"Key types: {', '.join(t['name'] for t in key_types[:12]) or 'n/a'}. Entry points: {eps}. "
            f"Depends on: {', '.join(mod.get('depends_on') or []) or 'none'}.\n\n"
            "In 2-3 sentences, describe how this module works end to end (its responsibility, "
            "the main flow from entry point to data/wire, key collaborators). Be concrete; do not invent APIs."
        )
        return _run_coro(call_llm(
            system="You summarize a code module's functional flow for orientation. "
                   "Stay factual to the facts given; never invent class/method names.",
            messages=[{"role": "user", "content": prompt}], max_tokens=300, agent_name="module_flow"))
    return summarize


def _run_coro(coro, *, timeout_s: float = 180.0):
    """Run an async coroutine from a SYNC function whether or not an event loop is
    already running. The orchestrator's sync phase bodies execute inside
    drive_run's loop, so a bare asyncio.run() would raise — fall back to a worker
    thread with its own loop.

    ``timeout_s`` HARD-bounds the call: these are best-effort, low-authority
    orientation LLM calls made during the workspace-indexing phase. In prod they
    route through the AiNxt gateway, whose SDK timeout is generous (minutes, ×
    retries) — an unreachable/stalled gateway would otherwise block the whole
    workspace phase with NO visible progress (the run looks "stuck at run
    created" forever). On timeout we cancel the coroutine (closing the in-flight
    HTTP request) so the thread unwinds and the caller's fail-soft handler skips
    the narrative instead of wedging the run."""
    import asyncio
    import concurrent.futures
    import time

    async def _bounded():
        return await asyncio.wait_for(coro, timeout=timeout_s)

    t0 = time.monotonic()
    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(_bounded())       # no loop running here → direct
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            # _bounded self-bounds via wait_for, so the worker thread is guaranteed to
            # return within ~timeout_s and the executor exits cleanly.
            return ex.submit(lambda: asyncio.run(_bounded())).result()
    except (asyncio.TimeoutError, TimeoutError):
        # Visible at WARNING — the fail-soft callers below swallow at debug, which would
        # otherwise hide a stalled gateway during indexing.
        logger.warning("_run_coro: indexing LLM call TIMED OUT after %.0fs (cap=%.0fs) — skipping narrative",
                       time.monotonic() - t0, timeout_s)
        raise


def maybe_generate_module_context(db, repo_id: str, repo_dir, base_commit_sha: str | None = None) -> int:
    """Gated, fail-soft entry the indexing pipeline calls (§19). No-op unless
    ``use_module_context_generation`` is on; never breaks an ingest run."""
    from app.core.config import settings
    if not settings.use_module_context_generation:
        return 0
    try:
        # Flag ON → include the opt-in low-authority LLM functional_flow narrative.
        return generate_module_context(db, repo_id, Path(repo_dir), base_commit_sha,
                                       summarize=_llm_functional_flow(Path(repo_dir)))
    except Exception as e:  # noqa: BLE001
        logger.warning("module_context generation skipped for repo=%s: %s", repo_id, e)
        return 0
