# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Static DI-wiring gate (Phase 1 context-load) — pure functions over (path, text).

Covers each check's positive + negative direction, the precision guards (framework
types, @Qualifier, containers, unresolvable names are SKIPPED — never guessed), the
delta-scoping (unchanged classes are not checked), and fail-open on garbage input.
"""
from app.agents.di_wiring_gate import (
    run_di_gate, parse_java, build_corpus,
)

RID = "network-core"


def _svc(name: str, ctor_types: list[str], package: str = "com.example.pay",
         imports: list[str] = (), anns: str = "@Service") -> str:
    imp = "\n".join(f"import {i};" for i in imports)
    params = ", ".join(f"{t} p{i}" for i, t in enumerate(ctor_types))
    return (f"package {package};\n{imp}\n{anns}\n"
            f"public class {name} {{\n"
            f"    public {name}({params}) {{}}\n}}\n")


def _path(name: str, pkg: str = "com/example/pay") -> str:
    return f"{RID}/src/main/java/{pkg}/{name}.java"


# ── parse_java ────────────────────────────────────────────────────────────────

def test_parse_java_extracts_bean_ctor_and_implements():
    src = ("package com.example.pay;\n"
           "import com.example.repo.TxnStore;\n"
           "@Service\n@Primary\n"
           "public class PayService implements PaymentHandler {\n"
           "    public PayService(TxnStore store, KafkaTemplate<String, String> kafka) {}\n"
           "}\n")
    (jc,) = parse_java(_path("PayService"), src)
    assert jc.is_bean and "Primary" in jc.annotations
    assert jc.implements == ["PaymentHandler"]
    assert [p.type_name for p in jc.ctor_params] == ["TxnStore", "KafkaTemplate"]
    assert jc.imports["TxnStore"] == "com.example.repo.TxnStore"


def test_parse_java_multiple_ctors_without_autowired_are_skipped():
    src = ("package p;\n@Service\npublic class S {\n"
           "    public S() {}\n"
           "    public S(Dep d) {}\n}\n")
    (jc,) = parse_java(_path("S", "p"), src)
    assert jc.ctor_params is None            # ambiguous — never guess


def test_parse_java_never_raises_on_garbage():
    assert parse_java("x.java", "") == []
    assert parse_java("x.java", "\x00\x01 not java {{{{") == []


# ── missing / ambiguous bean ─────────────────────────────────────────────────

def test_missing_bean_blocker_when_no_definition_exists():
    changed = _path("MandateService")
    corpus = [
        (changed, _svc("MandateService", ["MandateStore"])),
        # MandateStore EXISTS as a class but carries no stereotype / @Bean / XML
        (_path("MandateStore"), "package com.example.pay;\npublic class MandateStore {}\n"),
    ]
    r = run_di_gate({changed}, corpus)
    assert [f.check for f in r.findings] == ["missing_bean"]
    assert r.findings[0].severity == "blocker"
    assert "MandateService.MandateStore" == r.findings[0].key


def test_bean_satisfied_by_component_bean_method_or_xml():
    changed = _path("MandateService")
    base = [(changed, _svc("MandateService", ["MandateStore"]))]
    via_component = base + [(_path("MandateStore"),
                             "package com.example.pay;\n@Repository\npublic class MandateStore {}\n")]
    assert run_di_gate({changed}, via_component).findings == []

    via_bean = base + [
        (_path("MandateStore"), "package com.example.pay;\npublic class MandateStore {}\n"),
        (_path("Config"), "package com.example.pay;\n@Configuration\npublic class Config {\n"
                          "    @Bean\n    public MandateStore mandateStore() { return null; }\n}\n"),
    ]
    assert run_di_gate({changed}, via_bean).findings == []

    via_xml = base + [(_path("MandateStore"),
                       "package com.example.pay;\npublic class MandateStore {}\n")]
    xml = [(f"{RID}/src/main/resources/beans.xml",
            '<bean id="ms" class="com.example.pay.MandateStore"/>')]
    assert run_di_gate({changed}, via_xml, xml_files=xml).findings == []


def test_spring_data_repository_interface_is_not_missing_bean():
    """A Spring Data repository interface has no @Repository class / @Bean / XML entry —
    Spring generates the proxy bean at runtime. The gate must NOT report missing_bean
    (regression: an agentic run looped its whole review budget re-"fixing"
    missing_bean:ConfigParamService.ConfigParamRepository, which it never could)."""
    svc = _path("ConfigParamService")
    # Direct extends of a Spring Data base.
    direct = [
        (svc, _svc("ConfigParamService", ["ConfigParamRepository"])),
        (_path("ConfigParamRepository"),
         "package com.example.pay;\nimport org.springframework.stereotype.Repository;\n"
         "@Repository\npublic interface ConfigParamRepository "
         "extends CrudRepository<ConfigParam, String> {}\n"),
    ]
    assert run_di_gate({svc}, direct).findings == []

    # Transitive: extends a project base interface that itself extends a Spring Data base.
    transitive = [
        (svc, _svc("ConfigParamService", ["ConfigParamRepository"])),
        (_path("NoBeanRepository"),
         "package com.example.pay;\nimport org.springframework.data.repository.NoRepositoryBean;\n"
         "@NoRepositoryBean\npublic interface NoBeanRepository<T, ID> "
         "extends ReactiveCrudRepository<T, ID> {}\n"),
        (_path("ConfigParamRepository"),
         "package com.example.pay;\n@Repository\npublic interface ConfigParamRepository "
         "extends NoBeanRepository<ConfigParam, String> {}\n"),
    ]
    assert run_di_gate({svc, _path("ConfigParamRepository")}, transitive).findings == []


def test_interface_injection_resolved_via_stereotyped_impl():
    changed = _path("PayService")
    corpus = [
        (changed, _svc("PayService", ["TxnStore"])),
        (_path("TxnStore"), "package com.example.pay;\npublic interface TxnStore {}\n"),
        (_path("CassandraTxnStore"),
         "package com.example.pay;\n@Repository\n"
         "public class CassandraTxnStore implements TxnStore {}\n"),
    ]
    assert run_di_gate({changed}, corpus).findings == []


def test_ambiguous_bean_warning_and_primary_resolves_it():
    changed = _path("PayService")
    base = [
        (changed, _svc("PayService", ["TxnStore"])),
        (_path("TxnStore"), "package com.example.pay;\npublic interface TxnStore {}\n"),
        (_path("CassStore"), "package com.example.pay;\n@Repository\n"
                             "public class CassStore implements TxnStore {}\n"),
    ]
    two_impls = base + [(_path("JdbcStore"), "package com.example.pay;\n@Repository\n"
                                             "public class JdbcStore implements TxnStore {}\n")]
    r = run_di_gate({changed}, two_impls)
    assert [f.check for f in r.findings] == ["ambiguous_bean"]
    assert r.findings[0].severity == "warning"

    with_primary = base + [(_path("JdbcStore"), "package com.example.pay;\n@Repository\n@Primary\n"
                                                "public class JdbcStore implements TxnStore {}\n")]
    assert run_di_gate({changed}, with_primary).findings == []


def test_precision_guards_skip_framework_qualifier_and_containers():
    changed = _path("S")
    corpus = [(changed,
               "package p;\nimport org.springframework.kafka.core.KafkaTemplate;\n"
               "import java.util.List;\n@Service\npublic class S {\n"
               "    public S(KafkaTemplate kafka, List<Runnable> tasks,\n"
               "             @Qualifier(\"x\") Object dep) {}\n}\n")]
    # KafkaTemplate: no corpus class → framework → skip. List: container → skip.
    # @Qualifier param → skip. Object: no corpus class → skip.
    assert run_di_gate({changed}, corpus).findings == []


def test_delta_scoping_ignores_unchanged_classes():
    broken = _path("OldBroken")
    corpus = [
        (broken, _svc("OldBroken", ["GhostDep"])),          # pre-existing wiring hole
        (_path("GhostDep"), "package com.example.pay;\npublic class GhostDep {}\n"),
        (_path("NewThing"), _svc("NewThing", [])),
    ]
    # Only NewThing changed → OldBroken's missing bean is NOT the agent's problem.
    assert run_di_gate({_path("NewThing")}, corpus).findings == []


# ── @Value keys ───────────────────────────────────────────────────────────────

def _diff(path: str, lines: list[str]) -> str:
    return f"+++ b/{path}\n" + "".join(f"+{ln}\n" for ln in lines)


def test_value_key_unbound_warning_and_its_negatives():
    d = _diff(_path("S"), ['@Value("${network.mandate.expiry-days}") int days;',
                           '@Value("${network.mandate.retries:3}") int r;',      # default → safe
                           '@Value("${UPI_HOME}") String h;'])               # env-style → skip
    r = run_di_gate(set(), [], diff_text=d)
    assert [f.key for f in r.findings] == ["network.mandate.expiry-days"]
    assert r.findings[0].check == "value_key_unbound" and r.findings[0].severity == "warning"

    yml = [(f"{RID}/src/main/resources/application.yml",
            "network:\n  mandate:\n    expiry-days: 30\n")]
    assert run_di_gate(set(), [], config_files=yml, diff_text=d).findings == []

    props = [(f"{RID}/src/main/resources/application.properties",
              "network.mandate.expiry-days=30\n")]
    assert run_di_gate(set(), [], config_files=props, diff_text=d).findings == []


# ── scan path ─────────────────────────────────────────────────────────────────

def test_new_component_outside_scan_root_warns_inside_does_not():
    app = (f"{RID}/src/main/java/com/example/pay/App.java",
           "package com.example.pay;\n@SpringBootApplication\npublic class App {}\n")
    inside = _path("InScope")
    outside = f"{RID}/src/main/java/org/other/OutScope.java"
    corpus = [app,
              (inside, _svc("InScope", [])),
              (outside, _svc("OutScope", [], package="org.other"))]
    r = run_di_gate({inside, outside}, corpus, new_paths={inside, outside})
    assert [f.check for f in r.findings] == ["not_in_scan_path"]
    assert "OutScope" in r.findings[0].key
    # modified-but-not-new files are exempt (they were already scanned or already broken)
    assert run_di_gate({outside}, corpus, new_paths=set()).findings == []


# ── constructor-injection cycles ──────────────────────────────────────────────

def test_ctor_cycle_through_changed_class_blocks_and_lazy_breaks_it():
    a, b = _path("A"), _path("B")
    cyc = [(a, _svc("A", ["B"], package="p")), (b, _svc("B", ["A"], package="p"))]
    r = run_di_gate({a}, cyc)
    assert any(f.check == "injection_cycle" and f.severity == "blocker" for f in r.findings)

    lazy = [(a, _svc("A", ["B"], package="p", anns="@Service\n@Lazy")),
            (b, _svc("B", ["A"], package="p"))]
    assert not any(f.check == "injection_cycle"
                   for f in run_di_gate({a}, lazy).findings)


# ── fail-open ─────────────────────────────────────────────────────────────────

def test_run_di_gate_never_raises_on_garbage():
    r = run_di_gate({"x"}, [("x", "\x00 garbage {{{"), ("y.java", "")],
                    config_files=[("a.yml", ": : :bad yaml [")],
                    xml_files=[("b.xml", "<not-closed")],
                    diff_text="+++ nonsense\n+@Value(\"${broken\n")
    assert isinstance(r.findings, list)


def test_build_corpus_indexes_scan_roots_per_repo():
    corpus = build_corpus([
        (f"{RID}/src/main/java/com/example/App.java",
         "package com.example;\n@SpringBootApplication\npublic class App {}\n"),
        ("other-repo/src/main/java/com/x/App.java",
         "package com.x;\n@SpringBootApplication\npublic class App {}\n"),
    ])
    assert corpus.scan_roots[RID] == {"com.example"}
    assert corpus.scan_roots["other-repo"] == {"com.x"}
