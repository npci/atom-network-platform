# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Codegen preflight (infra readiness gate). The infra checks (redis/celery/toolchain)
are mocked; we test the URL masking and the branch wiring that turns a broken subsystem
into a human-readable problem."""
import pytest
from app.agents import codegen_preflight as P


def test_mask_redacts_password():
    assert P._mask("redis://:secret@redis:6379/0") == "redis://:***@redis:6379/0"
    assert P._mask("redis://user:pw@host:6379/0") == "redis://user:***@host:6379/0"
    assert P._mask("redis://host:6379/0") == "redis://host:6379/0"   # no creds → unchanged
    assert P._mask("") == ""


def test_redis_down_short_circuits(monkeypatch):
    # redis.from_url(...).ping() raising → the single redis problem, nothing downstream.
    import redis
    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(redis, "from_url", boom)
    problems = P.check_dependencies(include_worker=True)
    assert len(problems) == 1
    assert "job queue (Redis) is unreachable" in problems[0]


def test_missing_llm_key_is_flagged(monkeypatch):
    # redis ok, worker ok, toolchain ok, gitlab ok → only the missing-LLM-key branch fires.
    import redis
    monkeypatch.setattr(redis, "from_url", lambda *a, **k: type("R", (), {"ping": lambda self: True})())
    from app.services import celery_tasks
    monkeypatch.setattr(celery_tasks.celery_app.control, "ping", lambda timeout=2.0: [{"w": "ok"}])
    from app.agents import toolchain_report
    report = type("Rep", (), {"blocking_missing": [], "gitlab_token_present": True,
                              "tools": {}, "build_ready": True})()
    monkeypatch.setattr(toolchain_report, "build_toolchain_report", lambda: report)
    monkeypatch.setattr(P.settings, "gitlab_url", "https://gitlab.example.com")
    monkeypatch.setattr(P.settings, "llm_provider", "claude")
    monkeypatch.setattr(P.settings, "anthropic_api_key", "")
    problems = P.check_dependencies(include_worker=True)
    assert any("No API key configured for the LLM provider 'claude'" in p for p in problems)


def _stub_inspect(monkeypatch, active_queues):
    """Pin celery_app.control.inspect(...).active_queues() to a fixed reply.

    ``active_queues`` is the raw {worker: [{"name": q}, ...]} shape Celery returns,
    or None to simulate an inspection that answered nothing.
    """
    from app.services import celery_tasks
    stub = type("I", (), {"active_queues": lambda self: active_queues})()
    monkeypatch.setattr(celery_tasks.celery_app.control, "inspect", lambda timeout=2.0: stub)


def test_agentic_queue_unconsumed_is_flagged(monkeypatch):
    """A worker started WITHOUT `-Q agentic` answers ping but consumes only the
    default queue, so agentic.drive is never delivered — the run is created and
    sits at 'Getting ready' forever with nothing in any log. A liveness ping
    cannot see this; only the subscription can. (Live UAT outage, 2026-08-25.)"""
    _stub_inspect(monkeypatch, {"worker@host": [{"name": "celery"}]})
    problems = P._agentic_queue_problems()
    assert len(problems) == 1
    assert "No Celery worker is consuming the 'agentic' queue" in problems[0]
    assert "-Q agentic,celery" in problems[0]        # names the exact fix


def test_agentic_queue_subscribed_is_clean(monkeypatch):
    _stub_inspect(monkeypatch, {"worker@host": [{"name": "celery"}, {"name": "agentic"}]})
    assert P._agentic_queue_problems() == []


def test_agentic_queue_check_fails_open(monkeypatch):
    """Inspection unavailable (restricted control channel / older broker) must NOT
    manufacture a blocking problem — a false 'everything is broken' is worse than
    a diagnostic that couldn't run."""
    _stub_inspect(monkeypatch, None)
    assert P._agentic_queue_problems() == []

    from app.services import celery_tasks
    def boom(timeout=2.0):
        raise OSError("control channel unavailable")
    monkeypatch.setattr(celery_tasks.celery_app.control, "inspect", boom)
    assert P._agentic_queue_problems() == []


def test_assert_ready_or_message_none_when_clean(monkeypatch):
    monkeypatch.setattr(P, "check_dependencies", lambda **k: [])
    assert P.assert_ready_or_message() is None


def test_assert_ready_or_message_formats_problems(monkeypatch):
    monkeypatch.setattr(P, "check_dependencies", lambda **k: ["A is down", "B missing"])
    msg = P.assert_ready_or_message()
    assert msg is not None
    assert "can't run right now" in msg and "• A is down" in msg and "• B missing" in msg
