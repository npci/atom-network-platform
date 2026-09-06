# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Item 0.2: the precert_engine shims re-export the SAME objects.

The relocation moved `state_machine.py` and `verdict.py` to
`services/cert_agent/`; the old modules became explicit re-import shims. If a
shim ever became a COPY instead, `Phase` identity comparisons and
`TRANSITIONS` lookups would fork between the two homes and fail in ways that
read like state-machine bugs. `is` is the whole test.
"""


def test_state_machine_shim_reexports_the_same_objects():
    from app.services.cert_agent import state_machine as canonical
    from app.services.precert_engine import state_machine as shim

    assert shim.Phase is canonical.Phase
    assert shim.Trigger is canonical.Trigger
    assert shim.TRANSITIONS is canonical.TRANSITIONS
    assert shim.IllegalTransition is canonical.IllegalTransition
    assert shim.next_phase is canonical.next_phase
    assert shim.is_terminal is canonical.is_terminal


def test_verdict_shim_reexports_the_same_objects():
    from app.services.cert_agent import verdict as canonical
    from app.services.precert_engine import verdict as shim

    assert shim.Verdict is canonical.Verdict
    assert shim.ResultStatus is canonical.ResultStatus
    assert shim.Expectation is canonical.Expectation
    assert shim.ResponseOutcome is canonical.ResponseOutcome
    assert shim.decide is canonical.decide
    assert shim.normalized_code is canonical.normalized_code


def test_package_reexports_still_resolve_through_the_shims():
    """`precert_engine/__init__` publishes both modules as package API; the
    staying modules (`runner`, `connector`) import `.verdict` relatively.
    Both paths must keep landing on the canonical objects."""
    from app.services import precert_engine
    from app.services.cert_agent import state_machine, verdict

    assert precert_engine.Phase is state_machine.Phase
    assert precert_engine.decide is verdict.decide