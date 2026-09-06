# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Pins the channel/certification split so it cannot quietly erode.

Every change-communication send now goes through the active domain pack's
channel. The only remaining direct `send_task_to_partner` calls are
certification-protocol messages, which belong to the transport they ride and
were never PartnerChannel's to carry.

This is a source-level guard rather than a behavioural one on purpose: the
failure mode is a future change re-introducing a direct send because it is the
shorter path, and that is invisible to any runtime assertion.
"""
import pathlib
import re

APP = pathlib.Path(__file__).resolve().parents[2] / "app"

# Task types that are certification protocol. These legitimately keep calling
# the A2A transport directly.
CERT_TASK_TYPES = {
    "CERT_QUERY", "CERT_STATUS_UPDATE", "CERT_READINESS_DECLARATION",
    "CERT_TEST_REQUEST", "CERT_TEST_RESPONSE", "CERT_ACKNOWLEDGEMENT",
    "CERT_COMPLETION_SIGNOFF", "CERT_OF_COMPLIANCE", "CERT_VERDICT_NOTIFICATION",
    "CERT_WAIVER_DECISION", "CERT_CONFIG_REQUEST", "CERT_SIGNOFF_NOTIFICATION",
}

# The adapter IS the transport wrapper; the orchestrators and the trigger
# endpoint are certification implementation.
ALLOWED_DIRECT = {
    "adapters/channel/a2a.py",
    "services/cert_orchestrator.py",
    "a2a_common/authority_handlers.py",
    "api/cert_a2a_trigger.py",
}


def _direct_sends() -> list[tuple[str, int, str]]:
    out = []
    for path in APP.rglob("*.py"):
        rel = str(path.relative_to(APP))
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, ln in enumerate(lines):
            if "send_task_to_partner(" in ln and "import" not in ln and "def " not in ln:
                window = "\n".join(lines[i:i + 12])
                m = re.search(r"task_type=[\w.]*?([A-Z_]{4,})", window)
                out.append((rel, i + 1, m.group(1) if m else "?"))
    return out


def test_every_remaining_direct_send_is_certification():
    offenders = [
        (rel, line, task) for rel, line, task in _direct_sends()
        if rel not in ALLOWED_DIRECT and task not in CERT_TASK_TYPES
    ]
    assert not offenders, (
        "change-communication sends must go through notify_partner / the pack's "
        f"channel, not the transport directly: {offenders}"
    )


def test_the_migrated_modules_hold_no_direct_sends():
    """Named explicitly so a regression points at the file, not a set diff."""
    migrated = [
        "services/change_dispatch.py",
        "services/negotiation_extended.py",
        "services/kit_revision_runner.py",
        "agents/feasibility_resolver.py",
        "api/negotiation_mgmt.py",
    ]
    for rel in migrated:
        src = (APP / rel).read_text(encoding="utf-8")
        # Match CALLS and IMPORTS, not mentions. change_dispatch.py explains in a
        # comment why it does not hand its session to the channel, and naming the
        # old function there is legitimate — a gate that fails on prose teaches
        # people to delete the prose.
        code = "\n".join(ln for ln in src.splitlines()
                          if not ln.lstrip().startswith("#"))
        assert "send_task_to_partner(" not in code, f"{rel} regressed to a direct send"
        assert "import send_task_to_partner" not in code, f"{rel} still imports the transport"
        assert "notify_partner" in code, f"{rel} no longer dispatches at all"


def test_phase_c_keeps_only_its_certification_sends():
    """phase_c holds both kinds. Its channel sends are migrated; its two
    certification sends stay, annotated with why."""
    src = (APP / "api/phase_c.py").read_text(encoding="utf-8")
    remaining = [t for rel, _, t in _direct_sends() if rel == "api/phase_c.py"]
    assert remaining, "expected phase_c to retain its certification sends"
    assert all(t in CERT_TASK_TYPES for t in remaining), remaining
    assert "notify_partner" in src


def test_no_module_imports_the_transport_without_using_it():
    """A leftover import is how a direct send creeps back in unnoticed."""
    for path in APP.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        if "import send_task_to_partner" in src:
            assert "send_task_to_partner(" in src, (
                f"{path.relative_to(APP)} imports the transport but never calls it"
            )
