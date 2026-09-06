# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Index-time flow-map generator (THE BOOK v3.4, reuse-first §).

The generator's pure parts: tolerant JSON parse of the LLM output, the prompt is
generic (never names a specific API), and the rendered map reads cleanly.
"""
from app.agents import flow_context_generator as F


def test_parse_flow_json_strict():
    out = F._parse_flow_json('{"summary":"txns post through the pay flow",'
                             '"transaction_apis":[{"api":"PayApi","why":"debits/credits"}],'
                             '"meta_apis":[{"api":"InitApi","why":"initiation"}],'
                             '"flows":[{"name":"withdraw","steps":["InitApi","PayApi"]}]}')
    assert out["summary"].startswith("txns")
    assert out["transaction_apis"][0]["api"] == "PayApi"
    assert out["flows"][0]["steps"] == ["InitApi", "PayApi"]


def test_parse_flow_json_strips_code_fence():
    out = F._parse_flow_json('```json\n{"summary":"x","transaction_apis":[],"meta_apis":[],"flows":[]}\n```')
    assert out["summary"] == "x" and out["transaction_apis"] == []


def test_parse_flow_json_garbage_keeps_raw_summary():
    out = F._parse_flow_json("the model rambled without JSON")
    assert "rambled" in out["summary"]
    assert out["transaction_apis"] == [] and out["flows"] == []


def test_build_prompt_is_generic_and_uses_facts():
    facts = {"entry_points": [{"kind": "controller", "name": "WithdrawController", "module": "api"}],
             "module_lines": ["- api: handles inbound requests"], "xsd_names": ["ReqWithdraw.xsd"]}
    p = F._build_prompt(facts)
    assert "WithdrawController" in p and "ReqWithdraw.xsd" in p
    assert "debit/credit" in p and "STRICT JSON" in p
    # It must NOT plant a specific transaction API — the model must discover it.
    assert "ReqTransfer" not in p


def _row():
    from types import SimpleNamespace
    return SimpleNamespace(summary="money posts through the pay flow",
                           transaction_apis=[{"api": "PayApi", "why": "real debit/credit"}],
                           meta_apis=[{"api": "InitApi", "why": "starts the request"}],
                           flows=[{"name": "withdraw", "steps": ["InitApi", "PayApi"]},
                                  {"name": "collect", "steps": ["CollectApi", "PayApi"]}])


def test_format_flow_index_lists_names_not_truth():
    from app.agents.context_assembler import _format_flow
    out = _format_flow(_row())
    assert "NOT a source of truth" in out and "INCOMPLETE" in out   # advisory framing
    assert "PayApi" in out                                          # txn leg surfaced
    assert "withdraw" in out and "collect" in out                   # names indexed
    assert "InitApi → PayApi" not in out                            # steps are pull-based


def test_format_flow_key_retrieval_pulls_one_flow():
    from app.agents.context_assembler import _format_flow
    out = _format_flow(_row(), flow="withdraw")
    assert "InitApi → PayApi" in out                                 # the requested flow's steps
    assert "collect" not in out.split("transaction")[0]              # other flows not dumped
    assert "NOT a source of truth" in out


def test_format_flow_miss_says_map_is_incomplete():
    from app.agents.context_assembler import _format_flow
    out = _format_flow(_row(), flow="epfo settlement")
    assert "no indexed flow matches" in out
    assert "may still EXIST in code" in out                          # absence ≠ nonexistence


def test_parse_flow_json_salvages_truncated_output():
    # Reply cut at max_tokens mid-string: fence + unterminated JSON.
    cut = ('```json\n{"summary": "txns flow through pay", '
           '"transaction_apis": [{"api": "PayApi", "why": "real debit/credit"}], '
           '"meta_apis": [{"api": "InitApi", "why": "starts the requ')
    out = F._parse_flow_json(cut)
    assert out["summary"] == "txns flow through pay"          # salvaged, not raw-dumped
    assert out["transaction_apis"][0]["api"] == "PayApi"
