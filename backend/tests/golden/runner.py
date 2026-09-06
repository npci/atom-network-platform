# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Golden-output suite runner.

    python -m tests.golden.runner --list
    python -m tests.golden.runner --score case_001 --candidate path/to/output.md
    python -m tests.golden.runner --self-check      # score the shipped fixtures

Scoring is offline and free. GENERATION is not wired here on purpose: capturing
a real golden needs credentials, a scrubbed input fixture, and a decision about
which model produced it. Keeping the runner generation-free means the scoring
half is usable and testable today, and the capture step can be added without
redesigning anything.

See docs/GOLDEN_OUTPUTS.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tests.golden.scoring import Score, structural_score

FIXTURES = Path(__file__).parent / "fixtures"


def load_case(case_id: str) -> dict:
    path = FIXTURES / f"{case_id}.json"
    if not path.exists():
        raise SystemExit(f"no such case: {case_id} (looked in {FIXTURES})")
    return json.loads(path.read_text(encoding="utf-8"))


def golden_text(case_id: str) -> str:
    return (FIXTURES / f"{case_id}.golden.md").read_text(encoding="utf-8")


def list_cases() -> list[str]:
    return sorted(p.stem for p in FIXTURES.glob("*.json"))


def score_candidate(case_id: str, candidate: str) -> Score:
    case = load_case(case_id)
    return structural_score(
        candidate=candidate,
        golden=golden_text(case_id),
        doc_type=case["doc_type"],
        terms=case.get("domain_terms", []),
    )


def _report(case_id: str, label: str, score: Score, floor: float) -> bool:
    verdict = "PASS" if score.value >= floor else "FAIL"
    print(f"[{verdict}] {case_id} / {label}: {score.value:.3f} (floor {floor:.2f})")
    for k, v in sorted(score.detail.items()):
        print(f"          {k}: {v:.3f}")
    for f in score.findings[:10]:
        print(f"          - {f}")
    if len(score.findings) > 10:
        print(f"          … {len(score.findings) - 10} more")
    return score.value >= floor


def capture_case(case_id: str, write: bool = False) -> str:
    """Generate this case's artifact with the real agent.

    Costs an LLM call. Writing overwrites the accepted baseline, so it is
    opt-in: a golden is a document a reviewer signed off, not merely the most
    recent output. Overwriting one silently is how a baseline drifts to match
    whatever the code now does — which measures nothing.
    """
    from tests.golden.capture import capture

    case = load_case(case_id)
    gen = case.get("generation") or {}
    artifact = gen.get("artifact") or case["doc_type"]
    text = capture(artifact, gen.get("inputs", {}))

    if write:
        path = FIXTURES / f"{case_id}.golden.md"
        if path.exists():
            backup = FIXTURES / f"{case_id}.golden.md.prev"
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"  previous golden kept at {backup.name}")
        path.write_text(text, encoding="utf-8")
        print(f"  wrote {path.name} ({len(text)} chars) — REVIEW IT before committing")
    return text


def raise_floor(case_id: str, new_floor: float) -> None:
    """Ratchet: a floor may go up, never down.

    Without this a failing phase can be made to pass by lowering its bar, and
    the suite records the regression as a success.
    """
    path = FIXTURES / f"{case_id}.json"
    case = json.loads(path.read_text(encoding="utf-8"))
    current = case.setdefault("thresholds", {}).get("structural_min", 0.0)
    if new_floor <= current:
        raise SystemExit(f"refusing to lower floor {current} -> {new_floor}; "
                         "floors ratchet upward only")
    case["thresholds"]["structural_min"] = round(new_floor, 3)
    path.write_text(json.dumps(case, indent=2) + "\n", encoding="utf-8")
    print(f"  {case_id} floor {current} -> {case['thresholds']['structural_min']}")


DEFAULT_FLOOR = 0.9


def case_floor(case: dict) -> float:
    """The case's structural floor, defaulting when unset.

    `.get("structural_min", DEFAULT_FLOOR)` is NOT enough: a case authored with
    an explicit `"structural_min": null` — the natural way to write "not chosen
    yet" — has the key, so `.get` returns None and every comparison raises
    TypeError instead of saying what is wrong.
    """
    value = (case.get("thresholds") or {}).get("structural_min")
    return DEFAULT_FLOOR if value is None else float(value)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--score", metavar="CASE_ID")
    ap.add_argument("--candidate", metavar="PATH")
    ap.add_argument("--self-check", action="store_true",
                    help="score the shipped golden and degraded fixtures")
    ap.add_argument("--capture", metavar="CASE_ID",
                    help="generate this case with the real agent (costs an LLM call)")
    ap.add_argument("--write", action="store_true",
                    help="with --capture: overwrite the accepted golden")
    ap.add_argument("--raise-floor", nargs=2, metavar=("CASE_ID", "FLOOR"),
                    help="ratchet a case's structural floor upward")
    args = ap.parse_args(argv)

    if args.raise_floor:
        raise_floor(args.raise_floor[0], float(args.raise_floor[1]))
        return 0

    if args.capture:
        from tests.golden.capture import CaptureUnavailable, can_capture

        case = load_case(args.capture)
        artifact = (case.get("generation") or {}).get("artifact") or case["doc_type"]
        ok, why = can_capture(artifact)
        if not ok:
            print(f"cannot capture {args.capture}: {why}")
            print("see docs/GOLDEN_OUTPUTS.md — capture needs a configured provider")
            return 2
        try:
            text = capture_case(args.capture, write=args.write)
        except CaptureUnavailable as exc:
            print(f"cannot capture: {exc}")
            return 2
        if not args.write:
            print(text[:2000])
            print(f"\n[{len(text)} chars] — re-run with --write to accept as the golden")
        return 0

    if args.list:
        for c in list_cases():
            print(c)
        return 0

    if args.self_check:
        ok = True
        for case_id in list_cases():
            case = load_case(case_id)
            floor = case_floor(case)
            ok &= _report(case_id, "golden",
                          score_candidate(case_id, golden_text(case_id)), floor)
            degraded = FIXTURES / f"{case_id}.degraded.md"
            if degraded.exists():
                s = score_candidate(case_id, degraded.read_text(encoding="utf-8"))
                # Inverted expectation: the degraded fixture MUST fail. A suite
                # where everything passes is not measuring anything.
                caught = s.value < floor
                print(f"[{'PASS' if caught else 'FAIL'}] {case_id} / degraded: "
                      f"{s.value:.3f} — expected BELOW floor {floor:.2f}")
                ok &= caught
        return 0 if ok else 1

    if args.score:
        if not args.candidate:
            raise SystemExit("--score requires --candidate PATH")
        text = Path(args.candidate).read_text(encoding="utf-8")
        case = load_case(args.score)
        floor = case_floor(case)
        return 0 if _report(args.score, args.candidate,
                            score_candidate(args.score, text), floor) else 1

    ap.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
