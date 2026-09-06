# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Decision Ledger service (accuracy S1) — append / supersede / active-tip / render.

The supersession-chain logic (a new answer for a question_key points supersedes_id at
the current TIP, and only tips are "active") is the non-trivial part the plan claimed
was unit-tested; these are those tests.
"""
import pytest


@pytest.fixture
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import app.models  # noqa: F401 — register models so metadata is complete
    from app.models.change_analysis import DecisionLedgerEntry
    from app.core.database import Base
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[DecisionLedgerEntry.__table__])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


CID = "change-1"


def test_append_single_is_active(db_session):
    from app.services import decision_ledger as DL
    e = DL.append_entry(db_session, CID, question_key="q1", kind="clarification",
                        question="Scope?", chosen="A", directive="do A")
    assert e.supersedes_id is None
    assert [a.id for a in DL.active_entries(db_session, CID)] == [e.id]


def test_supersede_chain_keeps_only_tip_active(db_session):
    from app.services import decision_ledger as DL
    e1 = DL.append_entry(db_session, CID, question_key="q1", kind="clarification", chosen="A")
    e2 = DL.append_entry(db_session, CID, question_key="q1", kind="clarification", chosen="B")
    e3 = DL.append_entry(db_session, CID, question_key="q1", kind="clarification", chosen="C")
    assert e2.supersedes_id == e1.id          # each new answer supersedes the prior tip
    assert e3.supersedes_id == e2.id
    assert [a.id for a in DL.active_entries(db_session, CID)] == [e3.id]   # only the tip


def test_distinct_keys_each_have_their_own_tip(db_session):
    from app.services import decision_ledger as DL
    DL.append_entry(db_session, CID, question_key="q1", kind="clarification", chosen="A")
    DL.append_entry(db_session, CID, question_key="q2", kind="plan", chosen="B")
    DL.append_entry(db_session, CID, question_key="q1", kind="clarification", chosen="A2")
    active = {e.question_key: e.chosen for e in DL.active_entries(db_session, CID)}
    assert active == {"q1": "A2", "q2": "B"}


def test_supersede_false_leaves_both_as_tips(db_session):
    from app.services import decision_ledger as DL
    DL.append_entry(db_session, CID, question_key="q1", kind="clarification", chosen="A")
    e2 = DL.append_entry(db_session, CID, question_key="q1", kind="clarification",
                         chosen="B", supersede=False)
    assert e2.supersedes_id is None
    assert len(DL.active_entries(db_session, CID)) == 2


def test_build_block_empty_is_blank(db_session):
    from app.services import decision_ledger as DL
    assert DL.build_decisions_block(CID, db_session) == ""


def test_build_block_renders_tip_and_excludes_superseded(db_session):
    from app.services import decision_ledger as DL
    DL.append_entry(db_session, CID, question_key="q1", kind="clarification",
                    question="Scope?", chosen="old", directive="OLD")
    DL.append_entry(db_session, CID, question_key="q1", kind="clarification",
                    question="Scope?", chosen="new", directive="NEW")
    DL.append_entry(db_session, CID, question_key="q2", kind="plan",
                    question="Approach?", chosen="reuse")   # no directive → falls back to chosen
    block = DL.build_decisions_block(CID, db_session)
    assert "BINDING" in block
    assert "[clarification] Scope? → NEW" in block
    assert "OLD" not in block                               # superseded answer excluded
    assert "[plan] Approach? → reuse" in block              # chosen used when no directive


def test_build_block_skips_entry_with_no_body(db_session):
    from app.services import decision_ledger as DL
    DL.append_entry(db_session, CID, question_key="q1", kind="note", question="fyi")  # no directive/chosen
    assert DL.build_decisions_block(CID, db_session) == ""   # only the header → blank


# ── Question identity: stable keys, artifact/semantic similarity, repeat breaker ──
# Regression cover for the run that asked ONE question seven times: the key was derived
# from the question PROSE, the agent reworded it every round, nothing ever superseded
# anything, and all seven contradictory answers were injected as simultaneously BINDING.


def test_stable_key_ignores_wording_noise():
    from app.services import decision_ledger as DL
    a = DL.stable_question_key("code_decision", 'Change txnPurpose "BG" to "GP".')
    b = DL.stable_question_key("code_decision", "  change   TxnPurpose 'BG' TO 'GP'  ")
    assert a == b                       # case/quoting/whitespace are not identity
    assert a.startswith("code_decision:")
    assert len(a) <= 128                # fits the question_key column


def test_stable_key_separates_genuinely_different_anchors():
    from app.services import decision_ledger as DL
    assert (DL.stable_question_key("code_decision", "change the xsd enum")
            != DL.stable_question_key("code_decision", "add a settlement leg"))


def test_stable_key_long_anchors_sharing_a_prefix_do_not_collide():
    from app.services import decision_ledger as DL
    pre = "reviewer finding " + ("x" * 200)
    assert (DL.stable_question_key("code_decision", pre + " change BG to GP")
            != DL.stable_question_key("code_decision", pre + " leave BG as is"))


def test_salient_artifacts_extracts_code_identity_and_drops_filler():
    from app.services import decision_ledger as DL
    got = DL.salient_artifacts('Change NET-Common.xsd txnPurpose simpleType from "BG" to '
                               '"GP"; see TRANSIT_UTP_PURPOSE_CODE in CommonConstant.java')
    assert {"net-common.xsd", "txnpurpose", "bg", "gp",
            "transit_utp_purpose_code", "commonconstant.java"} <= got
    assert "simpletype" not in got      # artifact-shaped but carries no identity


def test_artifact_containment_ignores_thin_evidence():
    from app.services import decision_ledger as DL
    # One shared file name links half the corpus — not evidence of the same question.
    score, shared = DL.artifact_containment({"net-common.xsd", "a", "b"},
                                            {"net-common.xsd", "c", "d"})
    assert shared == ["net-common.xsd"] and score == 0.0


def test_artifact_containment_uses_the_smaller_set():
    from app.services import decision_ledger as DL
    # A follow-up that discovered more call sites is still the same question.
    score, shared = DL.artifact_containment({"bg", "gp", "txnpurpose"},
                                            {"bg", "gp", "txnpurpose", "x", "y", "z"})
    assert score == 1.0 and len(shared) == 3


def test_degenerate_embedding_is_detected():
    from app.services import decision_ledger as DL
    assert DL._is_degenerate([0.0] * 768)   # embed_query's fail-soft zero vector
    assert DL._is_degenerate([]) and DL._is_degenerate(None)
    assert not DL._is_degenerate([0.0, 0.1])


def test_same_anchor_reuses_the_chain_without_embeddings(db_session, monkeypatch):
    from app.services import decision_ledger as DL
    monkeypatch.setattr(DL, "_embed_scores", lambda *a, **k: {})
    anchor = 'Reviewer finding: change NET-Common.xsd txnPurpose "BG" to "GP"'
    k1, m1 = DL.resolve_question_key(db_session, CID, prefix="code_decision",
                                     anchor=anchor, question="Should I change BG to GP?")
    assert m1["match"] == "new"
    DL.append_entry(db_session, CID, question_key=k1, kind="code_decision",
                    question="Should I change BG to GP?", chosen="yes")
    k2, m2 = DL.resolve_question_key(db_session, CID, prefix="code_decision",
                                     anchor=anchor, question="totally reworded question")
    assert k2 == k1 and m2["match"] == "exact" and m2["signal"] == "anchor"


def test_reworded_anchor_still_collapses_via_artifact_overlap(db_session, monkeypatch):
    """The seven-escalation regression: the agent reworded BOTH the question and the
    blocked item each round, so the anchor hash differs -- but it kept naming the same
    artifacts, and that is enough. Embeddings are stubbed OFF to prove the deterministic
    signal carries this on its own (the embedder can be down)."""
    from app.services import decision_ledger as DL
    monkeypatch.setattr(DL, "_embed_scores", lambda *a, **k: {})
    first_q = ('The reviewer blocked this because NET-Common.xsd txnPurpose is still "BG", '
               'colliding with TRANSIT_UTP_PURPOSE_CODE, while PURPOSE_GESTURE_PAYMENT is "GP".')
    k1, _ = DL.resolve_question_key(db_session, CID, prefix="code_decision",
                                    anchor='Reviewer finding: change PURPOSE_GESTURE_PAYMENT '
                                           'and the NET-Common.xsd enumeration',
                                    question=first_q, kind="code_decision")
    DL.append_entry(db_session, CID, question_key=k1, kind="code_decision",
                    question=first_q, chosen="use GP",
                    decided_against={"blocked_item": "NET-Common.xsd txnPurpose BG GP"})

    k2, m2 = DL.resolve_question_key(
        db_session, CID, prefix="code_decision",
        anchor='code_decision: "Allow the XSD edit: change NET-Common.xsd txnPurpose BG to GP"',
        question='I attempted the ratified edit changing NET-Common.xsd txnPurpose "BG" to '
                 '"GP" but edit_file refuses; PURPOSE_GESTURE_PAYMENT stays inconsistent.',
        kind="code_decision")
    assert k2 == k1                                  # same chain despite a full reword
    assert m2["match"] == "similar" and m2["signal"] == "artifact"
    assert "bg" in m2["related"][0]["shared"]


def test_unrelated_question_gets_its_own_chain(db_session, monkeypatch):
    from app.services import decision_ledger as DL
    monkeypatch.setattr(DL, "_embed_scores", lambda *a, **k: {})
    k1, _ = DL.resolve_question_key(db_session, CID, prefix="code_decision",
                                    anchor="change NET-Common.xsd txnPurpose BG to GP",
                                    question='Change "BG" to "GP" in txnPurpose?',
                                    kind="code_decision")
    DL.append_entry(db_session, CID, question_key=k1, kind="code_decision",
                    question='Change "BG" to "GP" in txnPurpose?', chosen="yes")
    k2, m2 = DL.resolve_question_key(
        db_session, CID, prefix="code_decision",
        anchor="Which timeout should RespTransferService use for the async callback?",
        question="Should RespTransferService.pollStatus use the 30s or the 90s httpTimeoutMillis?",
        kind="code_decision")
    assert k2 != k1 and m2["match"] == "new"


def test_semantic_signal_supersedes_when_no_artifacts_are_named(db_session, monkeypatch):
    """A reworded question that cites nothing concrete has no artifact overlap; the
    embedding band is what catches it."""
    from app.services import decision_ledger as DL
    k1, _ = DL.resolve_question_key(db_session, CID, prefix="code_decision",
                                    anchor="anchor one", question="Do we bill the payer?",
                                    kind="code_decision")
    e1 = DL.append_entry(db_session, CID, question_key=k1, kind="code_decision",
                         question="Do we bill the payer?", chosen="yes")
    monkeypatch.setattr(DL, "_embed_scores", lambda q, cands: {e1.id: 0.95})
    k2, m2 = DL.resolve_question_key(db_session, CID, prefix="code_decision",
                                     anchor="anchor two",
                                     question="Is the payer the one who gets charged?",
                                     kind="code_decision")
    assert k2 == k1 and m2["match"] == "similar" and m2["signal"] == "semantic"


def test_related_band_surfaces_without_merging(db_session, monkeypatch):
    from app.services import decision_ledger as DL
    k1, _ = DL.resolve_question_key(db_session, CID, prefix="code_decision",
                                    anchor="anchor one", question="Do we bill the payer?",
                                    kind="code_decision")
    e1 = DL.append_entry(db_session, CID, question_key=k1, kind="code_decision",
                         question="Do we bill the payer?", chosen="yes")
    monkeypatch.setattr(DL, "_embed_scores", lambda q, cands: {e1.id: 0.85})
    k2, m2 = DL.resolve_question_key(db_session, CID, prefix="code_decision",
                                     anchor="anchor two", question="Who pays the fee?",
                                     kind="code_decision")
    assert k2 != k1                                   # NOT merged — below the bar
    assert m2["match"] == "new" and m2["related"]     # but the human is shown the neighbour
    assert m2["related"][0]["verdict"] == "related"


def test_embedder_outage_falls_back_to_deterministic_key(db_session, monkeypatch):
    """embed_query is fail-soft and returns a ZERO vector when the gateway is down.
    Scoring 0.0 against it would masquerade as a confident 'different question', so the
    embedding signal must withdraw entirely rather than vote."""
    from app.services import decision_ledger as DL
    monkeypatch.setattr("app.rag.embeddings.embed_query", lambda t: [0.0] * 768)
    anchor = "change NET-Common.xsd txnPurpose BG to GP"
    k1, _ = DL.resolve_question_key(db_session, CID, prefix="code_decision",
                                    anchor=anchor, question="q", kind="code_decision")
    DL.append_entry(db_session, CID, question_key=k1, kind="code_decision",
                    question="q", chosen="yes")
    k2, m2 = DL.resolve_question_key(db_session, CID, prefix="code_decision",
                                     anchor=anchor, question="q", kind="code_decision")
    assert k2 == k1 and m2["match"] == "exact"        # anchor path still works


def test_embedder_exception_is_swallowed(db_session, monkeypatch):
    from app.services import decision_ledger as DL

    def boom(_t):
        raise RuntimeError("gateway exploded")

    monkeypatch.setattr("app.rag.embeddings.embed_query", boom)
    DL.append_entry(db_session, CID, question_key="k1", kind="code_decision",
                    question="a question", chosen="x")
    key, meta = DL.resolve_question_key(db_session, CID, prefix="code_decision",
                                        anchor="some anchor", question="another question")
    assert key.startswith("code_decision:") and meta["match"] == "new"


def test_repeat_state_counts_answers_and_flags_the_loop(db_session):
    from app.services import decision_ledger as DL
    assert DL.repeat_state(db_session, CID, "k")["count"] == 0
    DL.append_entry(db_session, CID, question_key="k", kind="code_decision",
                    question="q", chosen="A", directive="do A")
    s1 = DL.repeat_state(db_session, CID, "k")
    assert s1["count"] == 1 and not s1["is_defect"]   # one re-ask is a normal follow-up
    DL.append_entry(db_session, CID, question_key="k", kind="code_decision",
                    question="q", chosen="B", directive="do B")
    s2 = DL.repeat_state(db_session, CID, "k")
    # Answered twice and asking again: the agent cannot act on what it is being told.
    assert s2["count"] == 2 and s2["is_defect"]
    assert [p["chosen"] for p in s2["prior"]] == ["A", "B"]   # prior answers, oldest first


def test_collapsed_chain_renders_one_binding_line(db_session, monkeypatch):
    """The payoff: contradictory answers to the same question no longer arrive as
    simultaneously BINDING — only the newest survives into the prompt."""
    from app.services import decision_ledger as DL
    monkeypatch.setattr(DL, "_embed_scores", lambda *a, **k: {})
    rounds = [
        ('Reviewer: NET-Common.xsd txnPurpose "BG" vs PURPOSE_GESTURE_PAYMENT "GP"',
         'Change NET-Common.xsd txnPurpose "BG" to "GP" for PURPOSE_GESTURE_PAYMENT?',
         "use GP"),
        ('Ratified: edit NET-Common.xsd txnPurpose "BG" — PURPOSE_GESTURE_PAYMENT "GP"',
         'The txnPurpose "BG" / "GP" edit to NET-Common.xsd for PURPOSE_GESTURE_PAYMENT '
         'is refused — what now?',
         "reuse BG"),
        ('Reviewer finding again: NET-Common.xsd txnPurpose "BG" / "GP" '
         'PURPOSE_GESTURE_PAYMENT',
         'Replace the "BG" enumeration in NET-Common.xsd txnPurpose with "GP" for '
         'PURPOSE_GESTURE_PAYMENT?',
         "use GP, final"),
    ]
    for anchor, q, ans in rounds:
        key, _ = DL.resolve_question_key(db_session, CID, prefix="code_decision",
                                         anchor=anchor, question=q, kind="code_decision")
        DL.append_entry(db_session, CID, question_key=key, kind="code_decision",
                        question=q, chosen=ans, directive=ans,
                        decided_against={"blocked_item": anchor})
    assert len(DL.active_entries(db_session, CID)) == 1
    block = DL.build_decisions_block(CID, db_session)
    assert "use GP, final" in block and "reuse BG" not in block


# --- review finding 2: bare (unquoted) wire codes must be salient artifacts ----------
# An agent writes `value BG` at least as often as `value "BG"`. If only the quoted form
# counted, the code — frequently the ONLY thing distinguishing two otherwise identical
# questions — was invisible to the artifact signal.

def test_bare_wire_codes_are_captured_quoted_or_not():
    from app.services.decision_ledger import salient_artifacts as sa
    assert "bg" in sa("txnPurpose value BG collides with the transit code")
    assert "bg" in sa('txnPurpose value "BG" collides with the transit code')
    assert "gp" in sa("gesture payment should use GP instead")


def test_concise_question_still_clears_min_shared(db_session, monkeypatch):
    """Review case: "Should txnPurpose use BG or GP in NET-Common.xsd?" is SHORT — it names
    only two things besides the codes. If bare codes were not artifacts it would yield 2,
    below ARTIFACT_MIN_SHARED=3, and a reworded twin could not merge with embeddings offline.
    That is precisely the fragmentation the deterministic signal exists to prevent."""
    from app.services import decision_ledger as DL
    monkeypatch.setattr(DL, "_embed_scores", lambda *a, **k: {})
    q1 = "Should txnPurpose use BG or GP in NET-Common.xsd?"
    q2 = "txnPurpose in NET-Common.xsd: BG conflicts, use GP?"
    assert len(DL.salient_artifacts(q1)) >= DL.ARTIFACT_MIN_SHARED
    score, _ = DL.artifact_containment(DL.salient_artifacts(q1), DL.salient_artifacts(q2))
    assert score >= DL.ARTIFACT_SUPERSEDE, "a concise reworded twin must still merge"

    k1, _ = DL.resolve_question_key(db_session, CID, prefix="code_decision",
                                    anchor=q1, question=q1, kind="code_decision")
    DL.append_entry(db_session, CID, question_key=k1, kind="code_decision", question=q1,
                    chosen="GP", directive="use GP", decided_against={"blocked_item": q1})
    k2, meta = DL.resolve_question_key(db_session, CID, prefix="code_decision",
                                       anchor=q2, question=q2, kind="code_decision")
    assert k2 == k1 and meta["match"] == "similar"


def test_line_numbers_and_years_are_not_identity():
    """Guard on the narrower bare-code rule: digits-only tokens must NOT become artifacts.
    Two unrelated questions about the same file often share a line number and a year, and
    counting those as shared identity would merge questions that are genuinely different."""
    from app.services.decision_ledger import salient_artifacts as sa, artifact_containment
    c = sa("NET-Common.xsd line 1060 rejected 3 times in 2024 - txnPurpose issue")
    d = sa("NET-Common.xsd line 1060 seen 3 times in 2024 - unrelated pacsMessage issue")
    assert "1060" not in c and "2024" not in c
    score, _ = artifact_containment(c, d)
    assert score < 0.75, "shared line numbers must not manufacture a merge"


def test_pascal_case_type_names_are_not_truncated():
    """Regression: the camelCase branch required a lowercase first run, so `CommonConstant`
    was captured as `ommonconstant` — a silent corruption that still matched itself, so it
    never failed loudly, but it mangled every PascalCase type name in the evidence."""
    from app.services.decision_ledger import salient_artifacts as sa
    assert sa("CommonConstant") == {"commonconstant"}
    assert sa("ConverterUtils") == {"converterutils"}
    assert sa("PayTrans") == {"paytrans"}


def test_screaming_case_and_filenames_survive_the_bare_code_rule():
    """Alternation order matters: the bare-code branch must not shred `UTP_PURPOSE_CODES_LIST`
    into `UTP`, nor clip `NET` out of `NET-Common.xsd`."""
    from app.services.decision_ledger import salient_artifacts as sa
    assert sa("UTP_PURPOSE_CODES_LIST") == {"utp_purpose_codes_list"}
    assert sa("NET-Common.xsd") == {"net-common.xsd"}
    assert sa("TRANSIT_UTP_PURPOSE_CODE") == {"transit_utp_purpose_code"}


def test_all_caps_prose_is_not_mistaken_for_a_wire_code():
    """The prompts and the agent shout constantly (BINDING, MUST NOT, LOCKED, STILL OPEN).
    Treating that emphasis as identity would inflate overlap between unrelated questions."""
    from app.services.decision_ledger import salient_artifacts as sa
    assert sa("This is BINDING and MUST NOT change. The XSD is LOCKED. STILL OPEN.") == set()


def test_differing_wire_codes_keep_two_questions_apart(db_session, monkeypatch):
    """The point of capturing bare codes: two questions identical except for the code are
    DIFFERENT questions and must not collapse onto one chain."""
    from app.services import decision_ledger as DL
    monkeypatch.setattr(DL, "_embed_scores", lambda *a, **k: {})
    a1 = ("txnPurpose enumeration value BG in NET-Common.xsd collides with "
          "CommonConstant.TRANSIT_UTP_PURPOSE_CODE")
    q1 = ("The txnPurpose enumeration in NET-Common.xsd adds BG, but "
          "CommonConstant.TRANSIT_UTP_PURPOSE_CODE already binds BG. Use GP?")
    k1, _ = DL.resolve_question_key(db_session, CID, prefix="code_decision",
                                    anchor=a1, question=q1, kind="code_decision")
    DL.append_entry(db_session, CID, question_key=k1, kind="code_decision", question=q1,
                    chosen="use GP", directive="use GP",
                    decided_against={"blocked_item": a1})

    a2 = ("txnPurpose enumeration value QR in NET-Common.xsd collides with "
          "CommonConstant.QR_PURPOSE_CODE")
    q2 = ("The txnPurpose enumeration in NET-Common.xsd adds QR, but "
          "CommonConstant.QR_PURPOSE_CODE already binds QR. Use something else?")
    k2, meta = DL.resolve_question_key(db_session, CID, prefix="code_decision",
                                       anchor=a2, question=q2, kind="code_decision")
    assert k2 != k1, "a different wire code is a different question"
    assert meta["match"] != "similar"


def test_seven_real_escalations_still_collapse(db_session, monkeypatch):
    """End-to-end guard for the whole point of fix 1, with embeddings OFF so the
    deterministic artifact signal carries it alone. Seven asks must not stay seven chains."""
    from app.services import decision_ledger as DL
    monkeypatch.setattr(DL, "_embed_scores", lambda *a, **k: {})
    seven = [
        ('txnPurpose enumeration value BG collides with CommonConstant.TRANSIT_UTP_PURPOSE_CODE',
         'The XSD txnPurpose enumeration in NET-Common.xsd adds value BG for gesture payment, '
         'but BG is already CommonConstant.TRANSIT_UTP_PURPOSE_CODE used by transit UTP flows. '
         'Should gesture payment use GP instead?'),
        ('BG purpose code already used by UTP transit flows in NET-Common.xsd',
         'The BG purpose code in NET-Common.xsd is already consumed by UTP_PURPOSE_CODES_LIST '
         'in CommonConstant. Using BG for gesture payment would alias transit. Confirm GP.'),
        ('Gesture payment purpose code collides with transit UTP code BG',
         'Gesture payment purpose code collides with the transit UTP code BG in '
         'NET-Common.xsd; CommonConstant.TRANSIT_UTP_PURPOSE_CODE is BG. Use GP?'),
        ('txnPurpose BG vs GP for gesture payment - contradictory ratified answers',
         'This is the 7th time: txnPurpose BG vs GP for gesture payment in NET-Common.xsd. '
         'CommonConstant.TRANSIT_UTP_PURPOSE_CODE is BG and isUtpTransaction depends on it.'),
    ]
    keys = []
    for anchor, q in seven:
        k, _ = DL.resolve_question_key(db_session, CID, prefix="code_decision",
                                       anchor=anchor, question=q, kind="code_decision")
        keys.append(k)
        DL.append_entry(db_session, CID, question_key=k, kind="code_decision", question=q,
                        chosen="use GP", directive="use GP",
                        decided_against={"blocked_item": anchor})
    assert len(set(keys)) < len(seven), "repeat asks must share chains, not fragment"
    assert len(DL.active_entries(db_session, CID)) == len(set(keys))
