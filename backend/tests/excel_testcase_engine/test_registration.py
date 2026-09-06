# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""`register_excel_testcase_engine` must actually RUN, not merely import.

WHY THIS FILE EXISTS
The engine is wired in from `main.on_startup()`, never at import, so nothing in
the suite ever executed the registration path. The BRD/TSD-only refactor
(3b95b6df) deleted `adapters/rag.py` while `injector.py` kept importing and
configuring it; that raises ImportError *inside* registration, `main.py` turns it
into RuntimeError, and the app refuses to boot with `excel_engine_enabled=true`.
2746 tests passed the whole time.

So these tests call the real function against a real FastAPI app with stub
host dependencies. Anything the injector imports or configures has to exist.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from fastapi import FastAPI

MAIN_PY = Path(__file__).resolve().parents[2] / "app" / "main.py"


class _StubJobRegistry:
    """Only the five attributes the injector checks for."""

    def create_job(self, *a, **k): ...
    def update_job(self, *a, **k): ...
    def complete_job(self, *a, **k): ...
    def fail_job(self, *a, **k): ...
    def get_job(self, *a, **k): ...


def _register(app: FastAPI, tmp_path: Path, **overrides):
    from app.excel_testcase_engine import register_excel_testcase_engine

    kwargs = {
        "llm": {"stream": lambda *a, **k: None, "call": lambda *a, **k: None},
        "job_registry": _StubJobRegistry(),
        "db_session_factory": lambda: None,
        "artifacts_dir": tmp_path / "artifacts",
        "outputs_dir": tmp_path / "workbooks",
    }
    kwargs.update(overrides)
    return register_excel_testcase_engine(app, **kwargs)


def test_registration_runs_end_to_end(tmp_path):
    """The regression guard: every import and adapter bind inside must resolve."""
    app = FastAPI()
    _register(app, tmp_path)

    # The router is the observable outcome — the engine is reachable.
    # FastAPI 0.141 keeps an included router as an `_IncludedRouter` wrapper with
    # no `.path`, so a bare `r.path` comprehension raises AttributeError instead
    # of reporting on the routes. Ask the app what it would actually serve.
    paths = {
        route.path
        for route in app.routes
        if getattr(route, "path", None) is not None
    } | {
        inner.path
        for route in app.routes
        for inner in getattr(getattr(route, "original_router", None), "routes", [])
        if getattr(inner, "path", None) is not None
    }
    assert any("/changes" in p for p in paths), f"engine routes not mounted: {sorted(paths)}"


def test_registration_binds_the_llm_adapter(tmp_path):
    """Binding is the point of registration; assert it took, not just that it ran."""
    from app.excel_testcase_engine.adapters import llm as llm_adapter

    sentinel_stream, sentinel_call = (lambda *a, **k: "s"), (lambda *a, **k: "c")
    _register(FastAPI(), tmp_path, llm={"stream": sentinel_stream, "call": sentinel_call})

    assert llm_adapter._stream_fn is sentinel_stream
    assert llm_adapter._call_fn is sentinel_call


def test_there_is_no_rag_adapter(tmp_path):
    """The BRD/TSD-only engine has no retrieval layer.

    Pinned rather than left implicit: re-adding a `rag` parameter without the
    module behind it is exactly how this broke, and the failure only showed up
    at container boot.
    """
    import inspect

    from app.excel_testcase_engine import register_excel_testcase_engine

    assert "rag" not in inspect.signature(register_excel_testcase_engine).parameters

    with pytest.raises(ImportError):
        from app.excel_testcase_engine.adapters import rag  # noqa: F401


def test_main_calls_registration_with_arguments_it_accepts():
    """`main.py`'s call site must bind against the real signature.

    The test above pins the CALLEE and passed the whole time `main.py` was still
    passing the deleted `rag=`, because every test here builds its own kwargs.
    So the caller went unchecked and the TypeError surfaced only at container
    boot — the same "2746 tests passed" failure this file was written for, one
    level up.

    Read statically rather than by importing `app.main`: the import pulls the
    whole app graph, and the defect is visible in the source.
    """
    from app.excel_testcase_engine import register_excel_testcase_engine

    calls = [
        node
        for node in ast.walk(ast.parse(MAIN_PY.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "register_excel_testcase_engine"
    ]
    # An AST matcher that matches nothing is permanently, uselessly green.
    assert calls, f"no register_excel_testcase_engine(...) call found in {MAIN_PY}"

    sig = inspect.signature(register_excel_testcase_engine)
    for call in calls:
        assert all(kw.arg for kw in call.keywords), (
            f"{MAIN_PY.name}:{call.lineno} uses **kwargs — this gate cannot check it"
        )
        sig.bind(  # raises TypeError naming the offending argument
            *[object()] * len(call.args),
            **{kw.arg: object() for kw in call.keywords},
        )


def test_missing_llm_callables_are_rejected(tmp_path):
    """The injector's own contract: fail loudly at boot, not mid-workflow."""
    with pytest.raises(RuntimeError, match="llm dict"):
        _register(FastAPI(), tmp_path, llm={"stream": None, "call": None})


def test_incomplete_job_registry_is_rejected(tmp_path):
    class Partial:
        def create_job(self, *a, **k): ...

    with pytest.raises(RuntimeError, match="job_registry missing"):
        _register(FastAPI(), tmp_path, job_registry=Partial())
