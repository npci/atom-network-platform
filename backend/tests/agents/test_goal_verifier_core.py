# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Goal-verifier core — schema/parse, gap-fingerprint, quorum, stall (pure)."""
from app.agents.goal_verifier_core import (
    Blocking, Outcome, SkepticVerdict, VerifierFinding,
    parse_verdict_json, skeptic_failure, gap_fingerprint, extract_path_line_tokens,
    normalize_scratch_paths, aggregate_verdicts, record_stall, fingerprint_source,
    STALL_THRESHOLD,
)


# ── parse (fail-closed) ────────────────────────────────────────────────────────

def test_parse_valid_verdict():
    v = parse_verdict_json({"refuted": True, "evidence": "Foo.java:12 no-op",
                            "confidence": "high", "blocking": "none",
                            "findings": [{"kind": "bug", "location": "Foo.java:12", "detail": "x"}]})
    assert v and v.refuted and v.confidence == "high"
    assert v.findings[0].kind == "bug"


def test_parse_rejects_missing_evidence():
    # empty/absent evidence is the rubber-stamp hole — must be rejected
    assert parse_verdict_json({"refuted": False, "evidence": ""}) is None
    assert parse_verdict_json({"refuted": False}) is None


def test_parse_rejects_nonbool_refuted():
    assert parse_verdict_json({"refuted": "yes", "evidence": "x"}) is None


def test_parse_normalises_unknown_confidence_and_blocking():
    v = parse_verdict_json({"refuted": False, "evidence": "ok", "confidence": "meh",
                            "blocking": "weird"})
    assert v.confidence == "medium" and v.blocking is Blocking.NONE


def test_parse_drops_empty_findings():
    v = parse_verdict_json({"refuted": True, "evidence": "x",
                            "findings": [{"kind": "", "location": "", "detail": ""},
                                         {"detail": "real"}]})
    assert len(v.findings) == 1 and v.findings[0].detail == "real"


def test_skeptic_failure_is_synthetic_refute():
    f = skeptic_failure(2, "transport timeout")
    assert f.refuted and f.synthetic and f.evidence == fingerprint_source(f)


# ── gap fingerprint ────────────────────────────────────────────────────────────

def test_fingerprint_stable_across_scratch_and_reorder():
    a = ["/tmp/run-abc/x/Foo.java:12 broken", "Bar.java:7 gap"]
    b = ["Bar.java:7 gap", "/tmp/run-XYZ/x/Foo.java:12 broken"]  # reordered + diff scratch id
    assert gap_fingerprint(a) == gap_fingerprint(b)


def test_fingerprint_changes_when_line_changes():
    assert gap_fingerprint(["Foo.java:1 x"]) != gap_fingerprint(["Foo.java:2 x"])


def test_fingerprint_empty_input_is_empty():
    assert gap_fingerprint([]) == ""
    assert gap_fingerprint(["", "   "]) == ""


def test_fingerprint_falls_back_to_lines_without_pathline():
    fp = gap_fingerprint(["Missing bean entirely", "No error code emitted"])
    assert fp and "missing bean entirely" in fp


def test_extract_path_line_requires_pathish_prefix():
    assert extract_path_line_tokens("time 12:30 not a path") == []
    assert extract_path_line_tokens("src/Foo.java:42 here") == ["src/foo.java:42"]


def test_normalize_scratch():
    assert "<scratch>" in normalize_scratch_paths("/var/folders/xy/tmpZ/File.java:1")


# ── quorum ─────────────────────────────────────────────────────────────────────

def _v(idx, refuted, conf="medium", blocking=Blocking.NONE, ev="e", synthetic=False):
    return SkepticVerdict(refuted=refuted, evidence=ev, confidence=conf, blocking=blocking,
                          skeptic_idx=idx, synthetic=synthetic,
                          findings=[VerifierFinding("gap", "F.java:1", "d")] if refuted else [])


def test_panel_all_pass_is_achieved():
    votes = [_v(0, False), _v(1, False), _v(2, False)]
    assert aggregate_verdicts(votes).outcome is Outcome.ACHIEVED


def test_panel_cold_majority_refute_not_achieved():
    votes = [_v(0, False), _v(1, True), _v(2, True)]  # 2 of 2 cold refute
    r = aggregate_verdicts(votes)
    assert r.outcome is Outcome.NOT_ACHIEVED and r.refuted and r.gaps


def test_skeptic0_high_confidence_refute_is_decisive():
    # cold panel would pass (both approve) but skeptic-0 high-conf refute fails the round
    votes = [_v(0, True, conf="high"), _v(1, False), _v(2, False)]
    assert aggregate_verdicts(votes).outcome is Outcome.NOT_ACHIEVED


def test_skeptic0_notrefuted_does_not_count_toward_approval():
    # skeptic0 approves but only 1 of 2 cold approve → not a strict majority → refuted stands
    votes = [_v(0, False), _v(1, False), _v(2, True)]
    # cold approvals =1, needed = 2//2+1 = 2 → quorum fails, and there's a refuter
    assert aggregate_verdicts(votes).outcome is Outcome.NOT_ACHIEVED


def test_all_refuters_blocking_routes_to_blocked():
    votes = [_v(0, True, blocking=Blocking.UNVERIFIABLE),
             _v(1, True, blocking=Blocking.CONTRADICTION)]
    r = aggregate_verdicts(votes)
    assert r.outcome is Outcome.BLOCKED and r.blocking is Blocking.CONTRADICTION


def test_all_synthetic_fails_closed_to_human():
    # C2: verifier infra failure must NOT fail-open to achieved — route to human (BLOCKED).
    votes = [skeptic_failure(0, "x"), skeptic_failure(1, "y")]
    r = aggregate_verdicts(votes)
    assert r.outcome is Outcome.BLOCKED and r.blocking is Blocking.UNVERIFIABLE and r.gaps


def test_minority_refuter_does_not_block_majority():
    # C1: cold majority approves + one MINORITY refuter (medium skeptic-0 or a dissenting cold)
    # → ACHIEVED. Previously the `not refuters` conjunct forced non-achieved (unanimity).
    # medium skeptic-0 refute, both cold approve:
    votes = [_v(0, True, conf="medium"), _v(1, False), _v(2, False)]
    assert aggregate_verdicts(votes).outcome is Outcome.ACHIEVED
    # a low-confidence dissenting cold with the cold majority still approving (panel of 5):
    votes5 = [_v(0, False), _v(1, False), _v(2, False), _v(3, False), _v(4, True, conf="low")]
    assert aggregate_verdicts(votes5).outcome is Outcome.ACHIEVED   # 3 of 4 cold approve


def test_panel_size_one_skeptic0_is_judge():
    assert aggregate_verdicts([_v(0, False)]).outcome is Outcome.ACHIEVED
    assert aggregate_verdicts([_v(0, True)]).outcome is Outcome.NOT_ACHIEVED


# ── stall ──────────────────────────────────────────────────────────────────────

def test_stall_fires_on_two_identical_fingerprints():
    fp = "foo.java:12"
    c1, s1 = record_stall(None, 0, fp)
    assert (c1, s1) == (1, False)
    c2, s2 = record_stall(fp, c1, fp)
    assert c2 == 2 and s2 is True                    # STALL_THRESHOLD reached


def test_stall_resets_on_new_fingerprint():
    c1, _ = record_stall("a.java:1", 1, "b.java:2")
    assert c1 == 1


def test_stall_empty_fingerprint_is_noop():
    assert record_stall("x", 3, "") == (3, False)    # degenerate round neither trips nor resets


def test_stall_relaxed_under_strategist():
    c, stalled = record_stall("f:1", 2, "f:1", strategist_active=True)
    assert c == 3 and stalled is False               # threshold relaxed to 5


def test_real_cc9e81b0_history_would_fire_stall():
    """Regression anchor: the SAME blocking finding recurred (TransactionStatusService /
    ComplaintRaisedStageHandler correctness) round after round while the legacy coarse
    basename|category key churned and never stopped. With path:line fingerprints, an
    identical refute across two rounds fires the stall — the loop stops instead of
    riding the round cap."""
    r3 = ["transaction-processor/.../TransactionStatusService.java:52 fabricates lastUpdatedTs"]
    r4 = ["transaction-processor/.../TransactionStatusService.java:52 fabricates lastUpdatedTs"]
    fp3, fp4 = gap_fingerprint(r3), gap_fingerprint(r4)
    assert fp3 == fp4 and fp3 != ""
    _, s = record_stall(fp3, 1, fp4)
    assert s is True
