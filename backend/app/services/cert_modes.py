# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Simulator-or-application mode, chosen per side, per run (ITA I-6b, §3.6).

Either end of a certification may point at a real deployed application instead
of a simulator, INDEPENDENTLY — four postures, all legitimate:

    npci=simulator  partner=simulator    the wiring works (the demo)
    npci=simulator  partner=application  the partner's generated code behaves
    npci=application partner=simulator   this platform's application behaves
    npci=application partner=application full integration

**The tunnel needs no change for this.** An alias already decouples *what to
call* from *where it is*, so `<x>_simulator` and `<x>_application` are two
entries in the same catalogue and the tunnel cannot tell them apart. The mode
lives on the run and SELECTS THE ALIAS. That is the easy part; these are not:

* **§3.6.1 — the trigger contract is symmetric.** A deployed application is a
  subject under test, not a test driver: swap a simulator for it and the
  control API that made "just call it" work disappears. So `application` mode
  on EITHER side needs the certification trigger contract, not a control call.
  `requires_trigger` says so for whichever side asks.
* **§3.6.3 — a simulator pass is WEAKER EVIDENCE, and must not be recorded
  identically.** Passing against a simulator proves message shape and wiring;
  passing against the application proves the application. `evidence()` is the
  sentence that goes on the run and the sign-off, so a reader a year later can
  tell what was actually on the other end.
* **§3.6.4 — real applications have real side effects.** Simulators reset;
  deployed systems do not, and the C-6 loop re-runs cases round after round.
  Hence: application mode is **never a default and never inherited** (see
  `resolve`), and the automated loop **refuses to auto-dispatch** into it
  unless explicitly permitted (`auto_dispatch_allowed`).

Pure: stdlib only. Aliases and permissions arrive as arguments, never read
from settings here — the caller owns configuration, this module owns the
rules.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "SIMULATOR", "APPLICATION", "MODES", "RunModes",
    "resolve", "alias_for", "requires_trigger", "auto_dispatch_allowed",
]

SIMULATOR = "simulator"
APPLICATION = "application"
MODES = (SIMULATOR, APPLICATION)


@dataclass(frozen=True)
class RunModes:
    npci: str = SIMULATOR
    partner: str = SIMULATOR

    @property
    def any_application(self) -> bool:
        return APPLICATION in (self.npci, self.partner)

    def evidence(self) -> str:
        """The §3.6.3 sentence — what this run's pass actually proves. Goes on
        the run record and the sign-off; a certificate that does not say this
        claims more than was verified."""
        if self.npci == APPLICATION and self.partner == APPLICATION:
            return ("Both ends were deployed applications: this run is "
                    "full-integration evidence.")
        if self.partner == APPLICATION:
            return ("The partner end was a deployed application (this "
                    "platform's end was a simulator): proves the partner's "
                    "implementation, not this platform's.")
        if self.npci == APPLICATION:
            return ("This platform's end was a deployed application (the "
                    "partner's end was a simulator): proves this platform's "
                    "implementation, not the partner's.")
        return ("Both ends were simulators: this run proves message shape and "
                "wiring ONLY — it is not evidence that either deployed "
                "application behaves correctly.")

    def as_dict(self) -> dict:
        return {"npci_mode": self.npci, "partner_mode": self.partner}


def _one(side: str, value: Any) -> str:
    if value is None:
        return SIMULATOR
    text = str(value).strip().lower()
    if text not in MODES:
        # Loud, not lenient. A typo'd "aplication" silently becoming
        # `simulator` would run a weaker test than the operator asked for and
        # label it with what they typed — the §3.6.3 over-claim, arrived at by
        # accident.
        raise ValueError(
            f"{side} mode {value!r} is not one of {MODES} — refusing rather "
            "than guessing which end this run was actually pointed at")
    return text


def resolve(requested: Mapping[str, Any] | None = None, *,
            prior: RunModes | None = None) -> RunModes:
    """The modes for a run. Defaults to simulator/simulator.

    `prior` (the previous round's modes) is accepted and **deliberately
    ignored for `application`**: §3.6.4 requires application mode to be an
    explicit per-run opt-in, never inherited. It is a parameter rather than
    absent so that call sites which HAVE the previous round cannot express
    inheritance by accident — passing it is safe by construction, and the
    signature documents the rule at every call site.

    A side is `application` only if THIS request says so.
    """
    req = requested or {}
    return RunModes(npci=_one("npci", req.get("npci_mode")),
                    partner=_one("partner", req.get("partner_mode")))


def alias_for(side: str, mode: str, *, simulator_alias: str,
              application_alias: str) -> str:
    """Which catalogue entry this side's mode points at. The tunnel resolves
    the alias against its own allowlist and cannot tell the two apart — which
    is exactly why mode selection is safe to express this way."""
    resolved = _one(side, mode)
    return application_alias if resolved == APPLICATION else simulator_alias


def requires_trigger(mode: str) -> bool:
    """§3.6.1: a side in `application` mode must be DRIVEN by the trigger
    contract — there is no control API on a real deployment."""
    return _one("side", mode) == APPLICATION


def auto_dispatch_allowed(modes: RunModes, *,
                          permitted: bool) -> tuple[bool, str]:
    """§3.6.4: the C-6 loop must not auto-dispatch a round that creates real
    side effects. Returns (allowed, reason) — the reason is written to the
    flow's halt record, so an operator sees WHY the loop stopped rather than
    finding a silently stalled certification."""
    if not modes.any_application:
        return True, ""
    if permitted:
        return True, ("application mode auto-dispatch explicitly permitted by "
                      "configuration")
    sides = [name for name, value in (("npci", modes.npci),
                                      ("partner", modes.partner))
             if value == APPLICATION]
    return False, (
        f"auto-dispatch refused: {' and '.join(sides)} side(s) in application "
        "mode, which has real side effects (duplicate records, notifications, "
        "downstream calls). A human dispatches this round, or configuration "
        "explicitly permits it.")
