# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Deterministic static Spring DI-wiring checks — Phase 1 of the context-load gate.

The pipeline's verify step stops at ``mvn clean install -DskipTests`` — it never
refreshes a Spring ApplicationContext, so boot-time wiring failures (missing bean,
ambiguous candidates, unbound ``@Value`` key, component outside the scan path,
constructor-injection cycle) pass every gate and crash at real deployment. Booting
the app in the agent workspace is not an option (context refresh needs Cassandra/
Kafka/config servers), so Phase 1 proves the wiring *statically*: regex-level
parsing of the workspace's ``src/main/java`` sources, cross-referencing every
injection point the change touched against the bean definitions that exist.

DELTA-SCOPED by design: only classes the change added/modified are checked (their
injection points, their scan-path placement, cycles passing through them). A legacy
codebase carries pre-existing wiring noise the agent must not be looped on — same
philosophy as ``agentic_legacy_test_compile_failopen``.

PRECISION-FIRST resolution: an injected type is only checked when it resolves
unambiguously to a project class in the corpus (by simple name, disambiguated via
imports). Framework/auto-configured types (no corpus class), generic containers,
``@Qualifier``/``@Value``/``@Nullable``/``@Lazy`` params, and unresolvable names are
all SKIPPED, never guessed — a false "missing bean" would loop the code agent on a
phantom. Bean candidates come from stereotype annotations, ``@Bean`` methods, AND
legacy XML ``<bean class=...>`` definitions (the switch codebase wires both ways).

Pure functions over (path, text) pairs; no I/O, no app imports beyond the shared
Finding/GateResult shapes; every check individually fail-open (error → no findings).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agents.contract_gate import Finding, GateResult

# ── Java source parsing (regex-level, comment-stripped) ───────────────────────

_STEREOTYPES = ("Component", "Service", "Repository", "Controller",
                "RestController", "Configuration", "ControllerAdvice")

# Generic containers / providers whose injection does not hard-fail on zero
# candidates (or whose element type we would have to guess) — never checked.
_CONTAINER_TYPES = frozenset({
    "List", "Set", "Map", "Collection", "Optional", "ObjectProvider", "Provider",
    "ObjectFactory", "ApplicationContext", "Environment", "BeanFactory",
})

# Spring Data repository base interfaces. An interface extending any of these —
# directly or transitively through a project base interface — is instantiated as a
# proxy bean by Spring Data at runtime: there is NO @Repository-annotated class,
# @Bean method, or XML entry for it in the source corpus. Demanding one is a
# guaranteed false "missing_bean" that loops the code agent on a phantom it can never
# satisfy (the injected type is an interface, so even annotating it @Repository can't
# help — `_candidates` only considers concrete classes). Observed live: an agentic run
# burned its full review-round budget re-"fixing" missing_bean:ConfigParamService.
# ConfigParamRepository, where ConfigParamRepository extends a ReactiveCrudRepository.
_SPRING_DATA_REPO_BASES = frozenset({
    "Repository", "CrudRepository", "ListCrudRepository", "PagingAndSortingRepository",
    "ListPagingAndSortingRepository", "JpaRepository", "JpaSpecificationExecutor",
    "QuerydslPredicateExecutor", "MongoRepository", "ReactiveMongoRepository",
    "ReactiveCrudRepository", "ReactiveSortingRepository", "RxJava2CrudRepository",
    "RxJava3CrudRepository", "R2dbcRepository", "CassandraRepository",
    "ReactiveCassandraRepository", "ElasticsearchRepository",
    "ReactiveElasticsearchRepository", "KeyValueRepository", "CouchbaseRepository",
    "ReactiveCouchbaseRepository", "Neo4jRepository", "ReactiveNeo4jRepository",
})

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+?)(\.\*)?\s*;", re.M)
# Type declaration + everything up to the opening brace (captures implements/extends
# even when wrapped) — annotations are collected from the preceding lines.
_DECL_RE = re.compile(
    r"^[ \t]*(?:public\s+|protected\s+|private\s+|final\s+|abstract\s+|static\s+)*"
    r"(class|interface|enum)\s+(\w+)([^{;]*)\{", re.M)
_ANNOTATION_LINE_RE = re.compile(r"^\s*@(\w+)(?:\(([^)]*)\))?\s*$")
_BEAN_METHOD_RE = re.compile(
    r"@Bean\b(?:\([^)]*\))?[\s\S]{0,200}?"
    r"(?:public|protected|private)?\s+(?:static\s+)?(?:final\s+)?"
    r"([\w.]+)(?:<[^>{;]*>)?\s+\w+\s*\(")
_AUTOWIRED_FIELD_RE = re.compile(
    r"@Autowired\b(?:\([^)]*\))?\s*\n"
    r"(?P<anns>(?:\s*@\w+(?:\([^)]*\))?\s*\n)*)"
    r"\s*(?:private|protected|public)?\s*(?:final\s+)?"
    r"(?P<type>[\w.]+)(?:<[^>;]*>)?\s+(?P<name>\w+)\s*;")
_VALUE_KEY_RE = re.compile(r'@Value\(\s*"\$\{([^}:"]+)(:[^}"]*)?\}"\s*\)')
_XML_BEAN_CLASS_RE = re.compile(r'<bean\b[^>]*\bclass="([\w.]+)"')
_XML_SCAN_RE = re.compile(r'component-scan[^>]*base-package="([^"]+)"')


@dataclass
class _Param:
    type_name: str            # simple name, generics stripped
    annotations: set[str]


@dataclass
class _JavaClass:
    name: str
    package: str
    path: str
    kind: str                                  # "class" | "interface" | "enum"
    annotations: set[str] = field(default_factory=set)
    implements: list[str] = field(default_factory=list)   # simple names
    extends: str | None = None
    ctor_params: list[_Param] | None = None    # None = no unambiguous injection ctor
    autowired_fields: list[_Param] = field(default_factory=list)
    imports: dict[str, str] = field(default_factory=dict)  # simple → fqn
    has_wildcard_import: bool = False

    @property
    def is_bean(self) -> bool:
        return bool(self.annotations & set(_STEREOTYPES))

    @property
    def fqn(self) -> str:
        return f"{self.package}.{self.name}" if self.package else self.name


def _strip_comments(text: str) -> str:
    return _LINE_COMMENT_RE.sub("", _BLOCK_COMMENT_RE.sub("", text))


def _simple(type_name: str) -> str:
    return type_name.split("<", 1)[0].strip().rsplit(".", 1)[-1]


def _split_params(raw: str) -> list[str]:
    """Split a parameter list at top-level commas (generics-aware)."""
    out, depth, cur = [], 0, []
    for ch in raw:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur))
    return [p.strip() for p in out if p.strip()]


def _parse_param(raw: str) -> _Param | None:
    anns = set(re.findall(r"@(\w+)", raw))
    bare = re.sub(r"@\w+(?:\([^)]*\))?", "", raw).replace("final ", " ").strip()
    m = re.match(r"([\w.]+(?:<[^>]*>)?(?:\[\])?)\s+\w+$", bare)
    if not m:
        return None
    return _Param(type_name=_simple(m.group(1)), annotations=anns)


def _class_annotations(lines: list[str], decl_line_idx: int) -> set[str]:
    """Collect single-line annotations immediately above a declaration."""
    anns: set[str] = set()
    i = decl_line_idx - 1
    while i >= 0:
        m = _ANNOTATION_LINE_RE.match(lines[i])
        if m:
            anns.add(m.group(1)); i -= 1
            continue
        if not lines[i].strip():
            i -= 1
            continue
        break
    return anns


def _find_ctor_params(body: str, class_name: str) -> list[_Param] | None:
    """The injection constructor's params: the single ctor, or the @Autowired one.
    Multiple ctors with no @Autowired → None (skip: Spring's choice is ambiguous)."""
    ctors = []
    for m in re.finditer(
            r"(?:@Autowired\b(?:\([^)]*\))?\s*)?(?:public|protected)\s+"
            + re.escape(class_name) + r"\s*\(([^)]*)\)", body):
        ctors.append((bool(m.group(0).lstrip().startswith("@Autowired")), m.group(1)))
    if not ctors:
        return None
    chosen = None
    if len(ctors) == 1:
        chosen = ctors[0][1]
    else:
        marked = [raw for auto, raw in ctors if auto]
        if len(marked) == 1:
            chosen = marked[0]
    if chosen is None:
        return None
    params = [_parse_param(p) for p in _split_params(chosen)]
    return [p for p in params if p is not None]


def parse_java(path: str, text: str) -> list[_JavaClass]:
    """Parse one Java source into class models. Never raises."""
    try:
        text = _strip_comments(text)
        pkg_m = _PACKAGE_RE.search(text)
        package = pkg_m.group(1) if pkg_m else ""
        imports: dict[str, str] = {}
        wildcard = False
        for m in _IMPORT_RE.finditer(text):
            if m.group(2):
                wildcard = True
            else:
                imports[m.group(1).rsplit(".", 1)[-1]] = m.group(1)
        lines = text.splitlines()
        # Map char offsets → line index for annotation lookup.
        line_starts = []
        off = 0
        for ln in lines:
            line_starts.append(off); off += len(ln) + 1
        out: list[_JavaClass] = []
        for m in _DECL_RE.finditer(text):
            kind, name, tail = m.group(1), m.group(2), m.group(3)
            decl_line = max(i for i, s in enumerate(line_starts) if s <= m.start())
            anns = _class_annotations(lines, decl_line)
            impl_m = re.search(r"\bimplements\s+([\w.,<>\s]+)", tail)
            impls = ([_simple(s) for s in _split_params(impl_m.group(1))]
                     if impl_m else [])
            ext_m = re.search(r"\bextends\s+([\w.<>]+)", tail)
            jc = _JavaClass(name=name, package=package, path=path, kind=kind,
                            annotations=anns, implements=impls,
                            extends=_simple(ext_m.group(1)) if ext_m else None,
                            imports=imports, has_wildcard_import=wildcard)
            if jc.is_bean and kind == "class":
                jc.ctor_params = _find_ctor_params(text, name)
                for fm in _AUTOWIRED_FIELD_RE.finditer(text):
                    f_anns = set(re.findall(r"@(\w+)", fm.group("anns") or "")) | {"Autowired"}
                    jc.autowired_fields.append(
                        _Param(type_name=_simple(fm.group("type")), annotations=f_anns))
            out.append(jc)
        return out
    except Exception:  # noqa: BLE001 — parser gap must not poison the gate
        return []


# ── Corpus index ──────────────────────────────────────────────────────────────

@dataclass
class _Corpus:
    by_simple: dict[str, list[_JavaClass]] = field(default_factory=dict)
    bean_method_types: set[str] = field(default_factory=set)   # simple return types of @Bean methods
    xml_bean_fqns: set[str] = field(default_factory=set)
    scan_roots: dict[str, set[str]] = field(default_factory=dict)  # repo prefix → base packages
    all_classes: list[_JavaClass] = field(default_factory=list)

    def resolve(self, cls: _JavaClass, type_simple: str) -> _JavaClass | None:
        """Resolve an injected simple type name to THE project class, or None when
        unknown/ambiguous (framework type, multi-match without import — skip)."""
        matches = self.by_simple.get(type_simple) or []
        if not matches:
            return None                       # not a project type (framework/auto-config)
        if len(matches) == 1:
            return matches[0]
        fqn = cls.imports.get(type_simple)
        if fqn:
            for c in matches:
                if c.fqn == fqn:
                    return c
        for c in matches:                     # same-package wins when unimported
            if c.package == cls.package:
                return c
        return None                           # ambiguous name — skip, never guess


def _repo_prefix(path: str) -> str:
    return path.replace("\\", "/").split("/", 1)[0]


def build_corpus(corpus_java: list[tuple[str, str]],
                 xml_files: list[tuple[str, str]] = ()) -> _Corpus:
    corpus = _Corpus()
    for path, text in corpus_java:
        for jc in parse_java(path, text):
            corpus.by_simple.setdefault(jc.name, []).append(jc)
            corpus.all_classes.append(jc)
        try:
            stripped = _strip_comments(text)
            for m in _BEAN_METHOD_RE.finditer(stripped):
                corpus.bean_method_types.add(_simple(m.group(1)))
            for m in re.finditer(r"@SpringBootApplication\b", stripped):
                pkg = _PACKAGE_RE.search(stripped)
                if pkg:
                    corpus.scan_roots.setdefault(_repo_prefix(path), set()).add(pkg.group(1))
            for m in re.finditer(r'@ComponentScan\w*\(([^)]*)\)', stripped):
                for lit in re.findall(r'"([\w.]+)"', m.group(1)):
                    corpus.scan_roots.setdefault(_repo_prefix(path), set()).add(lit)
        except Exception:  # noqa: BLE001
            pass
    for path, text in (xml_files or ()):
        try:
            for m in _XML_BEAN_CLASS_RE.finditer(text):
                corpus.xml_bean_fqns.add(m.group(1))
            for m in _XML_SCAN_RE.finditer(text):
                for pkg in m.group(1).split(","):
                    if pkg.strip():
                        corpus.scan_roots.setdefault(_repo_prefix(path), set()).add(pkg.strip())
        except Exception:  # noqa: BLE001
            pass
    return corpus


def _is_spring_data_repository(target: _JavaClass, corpus: _Corpus) -> bool:
    """True if `target` is a Spring Data repository interface — Spring generates a proxy
    bean for it at runtime, so no explicit bean definition exists in the corpus. An
    interface annotated ``@NoRepositoryBean`` is a shared base, NOT a bean, and is
    excluded. The extends-chain is walked through project base interfaces (e.g. a custom
    ``NoBeanRepository`` that itself extends ``ReactiveCrudRepository``)."""
    if target is None or target.kind != "interface" or "NoRepositoryBean" in target.annotations:
        return False
    seen: set[str] = set()
    frontier = [target]
    while frontier:
        iface = frontier.pop()
        base = iface.extends
        if not base or base in seen:
            continue
        if base in _SPRING_DATA_REPO_BASES:
            return True
        seen.add(base)
        frontier.extend(c for c in corpus.by_simple.get(base, []) if c.kind == "interface")
    return False


def _candidates(corpus: _Corpus, target: _JavaClass) -> list[_JavaClass]:
    """Bean definitions satisfying an injection of `target` (class or interface)."""
    out = []
    for classes in corpus.by_simple.values():
        for c in classes:
            if c.kind != "class":
                continue
            is_impl = (c.name == target.name
                       or target.name in c.implements
                       or c.extends == target.name)
            if not is_impl:
                continue
            if c.is_bean or c.fqn in corpus.xml_bean_fqns:
                out.append(c)
    if target.name in corpus.bean_method_types and not any(c.name == target.name for c in out):
        # A @Bean method returns this exact type — synthesise a candidate marker.
        out.append(target)
    return out


# ── Checks ────────────────────────────────────────────────────────────────────

_SKIP_PARAM_ANNS = {"Value", "Qualifier", "Nullable", "Lazy"}


def _injection_points(jc: _JavaClass):
    for p in (jc.ctor_params or []):
        yield p
    for p in jc.autowired_fields:
        yield p


def check_bean_resolution(changed: list[_JavaClass], corpus: _Corpus) -> list[Finding]:
    """Missing / ambiguous bean candidates for every injection point of a changed bean."""
    findings: list[Finding] = []
    try:
        for jc in changed:
            if not jc.is_bean:
                continue
            for p in _injection_points(jc):
                if p.annotations & _SKIP_PARAM_ANNS:
                    continue
                if p.type_name in _CONTAINER_TYPES:
                    continue
                target = corpus.resolve(jc, p.type_name)
                if target is None:
                    continue                  # framework / unresolvable — never guess
                if _is_spring_data_repository(target, corpus):
                    continue                  # Spring Data provides the bean at runtime
                cands = _candidates(corpus, target)
                if not cands:
                    findings.append(Finding(
                        check="missing_bean", severity="blocker",
                        key=f"{jc.name}.{p.type_name}", file=jc.path,
                        detail=(f"{jc.name} injects {p.type_name}, but no @Component/@Service/"
                                f"@Bean/XML bean definition for it exists — the Spring context "
                                f"will fail at startup (NoSuchBeanDefinitionException)"),
                        suggested_fix=(f"Annotate {p.type_name} (or an implementation of it) as a "
                                       f"@Component/@Service, define a @Bean method returning it, "
                                       f"or inject the implementation that is actually registered.")))
                elif (len(cands) > 1 and not any("Primary" in c.annotations for c in cands)
                      and "Qualifier" not in p.annotations):
                    findings.append(Finding(
                        check="ambiguous_bean", severity="warning",
                        key=f"{jc.name}.{p.type_name}", file=jc.path,
                        detail=(f"{jc.name} injects {p.type_name} with {len(cands)} candidates "
                                f"({', '.join(sorted(c.name for c in cands)[:4])}) and no "
                                f"@Primary/@Qualifier — startup may fail with "
                                f"NoUniqueBeanDefinitionException"),
                        suggested_fix="Add @Qualifier at the injection point or @Primary on the "
                                      "intended candidate."))
    except Exception:  # noqa: BLE001 — fail-open
        return []
    return findings


def _flatten_yaml_keys(text: str) -> set[str]:
    import yaml
    keys: set[str] = set()

    def walk(node, prefix):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{prefix}.{k}" if prefix else str(k))
        else:
            if prefix:
                keys.add(prefix)
    try:
        for doc in yaml.safe_load_all(text):
            walk(doc, "")
    except Exception:  # noqa: BLE001 — unparsable yml widens nothing
        pass
    return keys


def check_value_keys(diff_text: str, config_files: list[tuple[str, str]]) -> list[Finding]:
    """@Value("${key}") ADDED by the change with no default must exist in some config
    file. Warning (not blocker): prod may supply keys via config-server/env."""
    findings: list[Finding] = []
    try:
        added = "\n".join(ln[1:] for ln in (diff_text or "").splitlines()
                          if ln.startswith("+") and not ln.startswith("+++"))
        wanted = {m.group(1).strip() for m in _VALUE_KEY_RE.finditer(added)
                  if not m.group(2)}                       # has a `:default` → safe
        wanted = {k for k in wanted if not k.isupper()}    # env-var style — skip
        if not wanted:
            return []
        known: set[str] = set()
        prop_blob: list[str] = []
        for path, text in (config_files or ()):
            if path.endswith((".yml", ".yaml")):
                known |= _flatten_yaml_keys(text)
            else:
                prop_blob.append(text)
        props = "\n".join(prop_blob)
        for key in sorted(wanted):
            if key in known or re.search(r"^\s*" + re.escape(key) + r"\s*[=:]", props, re.M):
                continue
            findings.append(Finding(
                check="value_key_unbound", severity="warning", key=key,
                detail=(f'@Value("${{{key}}}") has no default and the key is not in any '
                        f"application*.yml/properties — context refresh fails unless a "
                        f"config server or environment supplies it"),
                suggested_fix=f'Add "{key}" to the application config (or give the @Value a '
                              ':default) so the context can start everywhere.'))
    except Exception:  # noqa: BLE001 — fail-open
        return []
    return findings


def check_scan_path(changed: list[_JavaClass], corpus: _Corpus,
                    new_paths: set[str]) -> list[Finding]:
    """A NEW stereotyped class whose package is outside every discovered scan root
    of its repo will silently never be instantiated."""
    findings: list[Finding] = []
    try:
        for jc in changed:
            if not (jc.is_bean and jc.package and jc.path in new_paths):
                continue
            roots = corpus.scan_roots.get(_repo_prefix(jc.path)) or set()
            if not roots:
                continue                       # no discovered roots — cannot judge
            if any(jc.package == r or jc.package.startswith(r + ".") for r in roots):
                continue
            findings.append(Finding(
                check="not_in_scan_path", severity="warning",
                key=jc.fqn, file=jc.path,
                detail=(f"new @{next(iter(jc.annotations & set(_STEREOTYPES)), 'Component')} "
                        f"{jc.name} lives in {jc.package}, outside every discovered scan root "
                        f"({', '.join(sorted(roots)[:3])}) — Spring will never instantiate it"),
                suggested_fix="Move the class under a scanned package or add its package to "
                              "@ComponentScan."))
    except Exception:  # noqa: BLE001 — fail-open
        return []
    return findings


def check_injection_cycles(changed: list[_JavaClass], corpus: _Corpus) -> list[Finding]:
    """Constructor-injection cycles passing through a changed class fail context
    refresh (BeanCurrentlyInCreationException) unless broken by @Lazy."""
    findings: list[Finding] = []
    try:
        beans = [c for c in corpus.all_classes if c.is_bean and c.kind == "class"]
        if len(beans) > 2000:                  # bounded: a graph this big is out of scope
            return []
        edges: dict[str, set[str]] = {}
        for c in beans:
            if "Lazy" in c.annotations:
                continue
            outs: set[str] = set()
            for p in (c.ctor_params or []):    # ctor injection only — field injection is lazy-ish
                if p.annotations & _SKIP_PARAM_ANNS or p.type_name in _CONTAINER_TYPES:
                    continue
                target = corpus.resolve(c, p.type_name)
                if target is None:
                    continue
                cands = _candidates(corpus, target)
                if len(cands) == 1 and "Lazy" not in cands[0].annotations:
                    outs.add(cands[0].fqn)
            edges[c.fqn] = outs
        changed_fqns = {c.fqn for c in changed if c.is_bean}
        seen_cycles: set[frozenset] = set()
        for start in changed_fqns:
            stack, on_path = [(start, iter(edges.get(start, ())))], [start]
            visited = {start}
            while stack:
                node, it = stack[-1]
                nxt = next(it, None)
                if nxt is None:
                    stack.pop(); on_path.pop()
                    continue
                if nxt in on_path:
                    cyc = frozenset(on_path[on_path.index(nxt):])
                    if cyc not in seen_cycles:
                        seen_cycles.add(cyc)
                        cyc_names = [f.rsplit(".", 1)[-1] for f in on_path[on_path.index(nxt):]] + \
                                    [nxt.rsplit(".", 1)[-1]]
                        findings.append(Finding(
                            check="injection_cycle", severity="blocker",
                            key=" → ".join(cyc_names),
                            detail=("constructor-injection cycle through the changed code — "
                                    "context refresh fails with BeanCurrentlyInCreationException"),
                            suggested_fix="Break the cycle: @Lazy one edge, switch one side to "
                                          "setter/field injection, or extract the shared logic."))
                    continue
                if nxt not in visited and nxt in edges:
                    visited.add(nxt)
                    stack.append((nxt, iter(edges.get(nxt, ()))))
                    on_path.append(nxt)
    except Exception:  # noqa: BLE001 — fail-open
        return []
    return findings


# ── Entry point ───────────────────────────────────────────────────────────────

def run_di_gate(changed_paths: set[str],
                corpus_java: list[tuple[str, str]],
                *,
                config_files: list[tuple[str, str]] | None = None,
                xml_files: list[tuple[str, str]] | None = None,
                new_paths: set[str] | None = None,
                diff_text: str = "") -> GateResult:
    """Run every static DI check, scoped to the classes in ``changed_paths``.

    ``corpus_java`` is (path, text) for every src/main/java file of the touched
    repos, paths prefixed with the repo id, CHANGED FILES INCLUDED with their
    post-change text. ``new_paths`` ⊆ ``changed_paths`` marks files the change
    created (scan-path check applies to those only). Pure; never raises."""
    result = GateResult()
    corpus = build_corpus(corpus_java, xml_files or [])
    changed = [c for c in corpus.all_classes if c.path in (changed_paths or set())]
    result.findings.extend(check_bean_resolution(changed, corpus))
    result.findings.extend(check_value_keys(diff_text, config_files or []))
    result.findings.extend(check_scan_path(changed, corpus, new_paths or set()))
    result.findings.extend(check_injection_cycles(changed, corpus))
    return result
