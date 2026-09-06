# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The certification loop decision (CERT-6) — a pure function.

`decide` looks at the round history and says what happens next: sign off,
dispatch round N+1, or halt. No session, no wire, no LLM — every branch is
exercisable with plain data, and the ACTING side
(`authority_handlers.process_cert_fix_notification`) stays a thin caller
behind `settings.cert_auto_loop_enabled` (default OFF: the decided posture is
auto-fix with a human approving the round close).

ONLY FAIL COUNTS as failure. A SKIP (held for missing test data, ungradable)
or an ERROR (our infrastructure) is not a defect the partner can fix —
counting either would make the no-progress guard halt on a set the partner
could never change. `failed_cases` is where that rule lives; keep it the only
place.

Termination, in priority order (§3.1 of the CERT plan):
 1. zero failures → SIGNOFF — even at the round cap; converging on the last
    permitted round is success. The orchestrator already emitted
    `cert_signoff_notification` on the all-passed run; the loop REPORTS and
    stops, it never sends a second one.
 2. identical failed set two rounds running → HALT_NO_PROGRESS — this guard
    matters more than the cap: burning rounds 3–5 on a set that did not move
    in round 2 helps nobody. Differing sets count as progress even at the
    same size (a fix that repairs one case and breaks another changed the
    problem).
 3. round cap reached (inclusive) → HALT_ROUND_CAP.
 4. otherwise → DISPATCH.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

__all__ = ["LoopAction", "RoundOutcome", "LoopDecision", "failed_cases", "decide"]


class LoopAction(str, enum.Enum):
    SIGNOFF = "signoff"
    DISPATCH = "dispatch"
    HALT_NO_PROGRESS = "halt_no_progress"
    HALT_ROUND_CAP = "halt_round_cap"


@dataclass(frozen=True)
class RoundOutcome:
    round_number: int
    failed_case_ids: frozenset[str]


@dataclass(frozen=True)
class LoopDecision:
    action: LoopAction
    reason: str

    @property
    def halted(self) -> bool:
        return self.action in (LoopAction.HALT_NO_PROGRESS, LoopAction.HALT_ROUND_CAP)


def failed_cases(case_statuses: Mapping[str, Iterable[str]] | Mapping[str, str]) -> frozenset[str]:
    """The case ids that FAILED — the only status that counts as failure.

    Accepts either one status per case or an iterable of statuses (a case may
    execute several variants); any FAIL fails the case.
    """
    failed = set()
    for case_id, statuses in case_statuses.items():
        if isinstance(statuses, str):
            statuses = [statuses]
        if any((s or "").upper() == "FAIL" for s in statuses):
            failed.add(case_id)
    return frozenset(failed)


def decide(rounds: Sequence[RoundOutcome], *, max_rounds: int) -> LoopDecision:
    """What follows the latest round. `rounds` is ordered by round_number."""
    if not rounds:
        return LoopDecision(LoopAction.DISPATCH, "no rounds recorded — dispatch the first")
    latest = rounds[-1]

    if not latest.failed_case_ids:
        return LoopDecision(
            LoopAction.SIGNOFF,
            f"round {latest.round_number} passed every case — sign-off already "
            "issued by the run; the loop stops here")

    previous = rounds[-2] if len(rounds) >= 2 else None
    if previous is not None and previous.failed_case_ids == latest.failed_case_ids:
        return LoopDecision(
            LoopAction.HALT_NO_PROGRESS,
            f"round {latest.round_number} failed the same "
            f"{len(latest.failed_case_ids)} case(s) as round "
            f"{previous.round_number} — no progress; operator review needed")

    if latest.round_number >= max_rounds:
        return LoopDecision(
            LoopAction.HALT_ROUND_CAP,
            f"round cap {max_rounds} reached with "
            f"{len(latest.failed_case_ids)} case(s) still failing")

    return LoopDecision(
        LoopAction.DISPATCH,
        f"{len(latest.failed_case_ids)} case(s) failing after round "
        f"{latest.round_number} — dispatching round {latest.round_number + 1}")
