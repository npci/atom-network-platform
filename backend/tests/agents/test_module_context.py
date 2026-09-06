# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Multi-language module discovery (Rust migration support, §19)."""
import pytest


@pytest.fixture
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import app.models  # register every model so FKs resolve
    from app.models.code_repo import CodeRepo
    from app.models.module_context import ModuleContext
    from app.core.database import Base
    engine = create_engine("sqlite://")
    # Only the tables this test touches — other tables use pg-only types (JSONB).
    Base.metadata.create_all(engine, tables=[CodeRepo.__table__, ModuleContext.__table__])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _w(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_discover_rust_workspace_with_member_crates(tmp_path):
    from app.agents.module_context_generator import discover_modules, _key_types, _entry_points
    _w(tmp_path / "Cargo.toml", '[workspace]\nmembers = ["switch-core", "epfo-meta"]\n')
    _w(tmp_path / "switch-core/Cargo.toml",
       '[package]\nname = "switch-core"\nedition = "2021"\n\n[dependencies]\nserde = "1"\naxum = "0.7"\n')
    _w(tmp_path / "switch-core/src/lib.rs",
       "pub struct Transaction {}\npub enum Stage { Init }\npub trait Settler {}\n")
    _w(tmp_path / "switch-core/src/main.rs",
       '#[tokio::main]\nasync fn serve() {}\n\nfn main() {\n}\n')
    _w(tmp_path / "epfo-meta/Cargo.toml", '[package]\nname = "epfo-meta"\nedition = "2024"\n')

    mods = {m["module_path"]: m for m in discover_modules(tmp_path)}
    assert set(mods) == {".", "switch-core", "epfo-meta"}
    root = mods["."]
    assert root["lang"] == "rust" and "(workspace)" in root["artifact_id"]   # virtual workspace root
    crate = mods["switch-core"]
    assert crate["artifact_id"] == "switch-core" and crate["java_version"] == "rust 2021"
    assert crate["parent_module_path"] == "." and crate["depth"] == 1        # nesting-derived tree
    assert crate["depends_on"] == ["axum", "serde"]

    kt = _key_types(tmp_path, "switch-core", "rust")
    assert {"Transaction", "Stage", "Settler"} <= {t["name"] for t in kt}    # pub types found
    assert all(t["file"].endswith(".rs") for t in kt)                        # symbol → file
    eps = _entry_points(tmp_path, "switch-core", "rust")
    kinds = {(e["kind"], e["name"]) for e in eps}
    assert ("tokio::main", "serve") in kinds and ("main", "main") in kinds


def test_discover_mixed_java_rust_repo_one_tree(tmp_path):
    from app.agents.module_context_generator import discover_modules
    _w(tmp_path / "pom.xml",
       "<project><artifactId>network-parent</artifactId></project>")
    _w(tmp_path / "rust/settler/Cargo.toml", '[package]\nname = "settler"\nedition = "2021"\n')
    _w(tmp_path / "svc/go.mod", "module example.com/svc\n\ngo 1.22\n")
    mods = {m["module_path"]: m for m in discover_modules(tmp_path)}
    assert mods["."]["lang"] == "maven"
    assert mods["rust/settler"]["lang"] == "rust"
    assert mods["rust/settler"]["parent_module_path"] == "."   # nests under the maven root
    assert mods["svc"]["lang"] == "go" and mods["svc"]["java_version"] == "go 1.22"


def test_generate_module_context_persists_rust_rows(tmp_path, db_session):
    from app.agents.module_context_generator import generate_module_context
    from app.models.module_context import ModuleContext
    _w(tmp_path / "Cargo.toml", '[package]\nname = "switch-rs"\nedition = "2021"\n')
    _w(tmp_path / "src/lib.rs", "pub struct Ledger {}\n")
    n = generate_module_context(db_session, "repo-rs", tmp_path)
    assert n == 1
    row = db_session.query(ModuleContext).filter_by(repo_id="repo-rs").one()
    assert "Rust crate 'switch-rs'" in row.summary
    assert "Ledger" in {t["name"] for t in (row.key_types or [])}
    assert all(t.get("file") for t in (row.key_types or []))   # every type carries its file
    assert row.java_version == "rust 2021"
