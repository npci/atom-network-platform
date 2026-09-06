# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Drive the certification state machine alongside the A2A conversation.

`precert_engine.state_machine` already defines the lifecycle and the only legal
moves through it; nothing had ever driven it. The cert path just sent messages,
so a run's phase existed only implicitly in which message had gone out last, and
an out-of-order exchange would not have been noticed by anything.

`FlowState` is the thin thing that binds the two: one trigger per message, phase
validated on every step.

WHY IT WARNS RATHER THAN RAISES
`next_phase` raises `IllegalTransition` on an illegal move, which is correct for
a pure state machine and wrong for this caller. Certification tracking is being
added to a flow that already works; a mistake in MY trigger sequence must not
abort a real bank's cert run. So `fire()` logs an ERROR (loud, greppable,
carries both phase and trigger) and holds the current phase. The strict
behaviour stays available — and tested — via `state_machine.next_phase` itself.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from app.services.cert_agent.state_machine import (
    IllegalTransition,
    Phase,
    Trigger,
    is_terminal,
    next_phase,
)
from app.services.cert_agent.tasks import OVERALL_STATES

logger = logging.getLogger(__name__)

# Phases the engine has but the spec's `cert_status_report.overall_state` enum
# does not (see tests/services/test_cert_agent_tasks.py, which pins the pair).
# Reporting either verbatim would put an off-vocabulary value on the wire, so
# each maps to the nearest state the spec does define:
#
#   CERTIFIED -> COMPLETED    the run is finished and successful; the spec ends
#                             its lifecycle at COMPLETED and treats sign-off as
#                             the artefact rather than a distinct state.
#   BLOCKED   -> FIX_PENDING  BLOCKED is only reachable via waiver_rejected, and
#                             a rejected waiver leaves exactly one way forward:
#                             the bank fixes the case.
_WIRE_OVERRIDES = {Phase.CERTIFIED: "COMPLETED", Phase.BLOCKED: "FIX_PENDING"}


def wire_overall_state(phase: Phase) -> str:
    """The phase as the spec's `overall_state` vocabulary spells it."""
    return _WIRE_OVERRIDES.get(phase, phase.value)


class FlowState:
    """Tracks one cert run's phase, persisted through an optional callable.

    C-0: the phase used to live only in process memory ("a run is a single
    process"); it now survives via `persist` — typically
    `flow_store.persister(...)` — invoked after every LEGAL transition. The
    class itself stays pure: no session, no SQLAlchemy import, and a raising
    `persist` is caught and logged, because a mistake in OUR bookkeeping must
    not abort a real partner's cert run (same posture as warn-don't-raise
    above).

    `flushed` is the persistence watermark: how many of THIS instance's history
    entries have reached the store. It deliberately counts what this FlowState
    wrote, not what the stored row holds — the orchestrator builds a fresh
    FlowState per round, so an instance's in-memory history starts empty
    against a row that already carries earlier rounds. Diffing against the
    stored length would silently drop every round-2 transition.
    """

    def __init__(self, cflow_id: str, *, phase: Phase = Phase.NOT_STARTED,
                 persist: Callable[["FlowState"], None] | None = None):
        self.cflow_id = cflow_id
        self.phase = phase
        # (trigger, resulting phase, at — ISO-8601 UTC)
        self.history: list[tuple[str, str, str]] = []
        self.persist = persist
        self.flushed: int = 0   # history entries already persisted by THIS instance

    def fire(self, trigger: Trigger) -> Phase:
        """Apply `trigger`. Logs and holds on an illegal move — see module docstring.

        An illegal move appends nothing and persists nothing: the audit must
        not show a transition that never happened.
        """
        try:
            new = next_phase(self.phase, trigger)
        except IllegalTransition as exc:
            logger.error("cert_flow %s: %s — holding at %s", self.cflow_id, exc, self.phase.value)
            return self.phase
        if new is not self.phase:
            logger.info("cert_flow %s: %s --%s--> %s",
                        self.cflow_id, self.phase.value, trigger.value, new.value)
        self.phase = new
        self.history.append(
            (trigger.value, new.value, datetime.now(timezone.utc).isoformat())
        )
        self._persist_safely()
        return new

    def _persist_safely(self) -> None:
        if self.persist is None:
            return
        try:
            self.persist(self)
        except Exception:  # noqa: BLE001 — a failed write must not abort the run
            logger.exception(
                "cert_flow %s: persist failed — continuing; missed entries retry on next save",
                self.cflow_id,
            )

    @property
    def overall_state(self) -> str:
        """Spec-vocabulary phase, safe to put in a `cert_status_report`."""
        state = wire_overall_state(self.phase)
        # Guard rather than trust the map: a phase added later without a wire
        # mapping would otherwise leak straight onto the wire.
        if state not in OVERALL_STATES:
            logger.error("cert_flow %s: phase %s has no spec overall_state — reporting RUNNING",
                         self.cflow_id, self.phase.value)
            return "RUNNING"
        return state

    @property
    def finished(self) -> bool:
        return is_terminal(self.phase)
