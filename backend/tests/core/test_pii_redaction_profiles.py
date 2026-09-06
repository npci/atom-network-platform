# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""T6 (THREAT_MODEL.md) — regression tests for PII redaction profiles.

The central risk this file guards is NOT "does redaction work" (that is the
easy half) but "does redaction avoid corrupting the specification content that
drives code generation". The aggressive default profile matches any 9-18 digit
run, which in a TSD/BRD legitimately means timeouts, epoch-millis timestamps,
byte budgets and the Authority response codes. Redacting those would turn a privacy
control into a correctness bug in generated code — so PROFILE_DOC must leave
them alone while still catching label-anchored PII.
"""
from __future__ import annotations

from app.core.pii_redaction import (
    PROFILE_DOC,
    PROFILE_FREETEXT,
    redact_doc_sections,
    redact_for_llm_prompt,
)

# Realistic specification prose: every number here is a CONTRACT VALUE that
# must survive redaction intact.
_SPEC_TEXT = (
    "Endpoint: POST /api/v2/network/collect\n"
    "Timeout: 30000 ms. Max amount: 100000 paise.\n"
    "Error code XU1234567890 maps to ResponseCode 1234567891.\n"
    "Timestamp field format: epoch millis e.g. 1735689600000\n"
    "Contact spec@npci.org.in for the schema.\n"
    "Retry after 120000 ms; budget 999999999 bytes.\n"
)


class TestDocProfilePreservesContractValues:
    """PROFILE_DOC must not touch bare numeric literals."""

    def test_spec_numbers_survive_redaction(self):
        out, count = redact_for_llm_prompt(_SPEC_TEXT, profile=PROFILE_DOC)
        assert count == 0, f"expected no redactions in pure spec text, got {count}"
        assert out == _SPEC_TEXT

    def test_individual_contract_values_survive(self):
        out, _ = redact_for_llm_prompt(_SPEC_TEXT, profile=PROFILE_DOC)
        for literal in ("30000", "100000", "1234567891", "1735689600000", "999999999"):
            assert literal in out, f"contract literal {literal} was wrongly redacted"

    def test_contact_email_survives_in_doc_profile(self):
        """An email-shaped token in a spec is a contact address or namespace
        fragment, not a consumer's the network handle."""
        out, _ = redact_for_llm_prompt(_SPEC_TEXT, profile=PROFILE_DOC)
        assert "spec@npci.org.in" in out


class TestDocProfileStillCatchesRealPii:
    """Conservative must not mean toothless — label-anchored PII is redacted."""

    def test_labelled_account_number_is_redacted(self):
        out, count = redact_for_llm_prompt(
            "Account No: 123456789012 belongs to the remitter.", profile=PROFILE_DOC)
        assert count == 1
        assert "123456789012" not in out
        assert "[REDACTED-PII-ACCOUNT]" in out

    def test_labelled_customer_id_is_redacted(self):
        out, count = redact_for_llm_prompt("Customer ID: CRN9988776655", profile=PROFILE_DOC)
        assert count == 1
        assert "CRN9988776655" not in out

    def test_bare_mobile_number_is_redacted(self):
        """Mobile numbers are high-signal enough to redact unconditionally."""
        out, count = redact_for_llm_prompt(
            "escalate to 9876543210 for details", profile=PROFILE_DOC)
        assert count == 1
        assert "9876543210" not in out

    def test_mpin_reference_is_redacted(self):
        out, count = redact_for_llm_prompt("user entered MPIN 1234", profile=PROFILE_DOC)
        assert count == 1
        assert "[REDACTED-PII-MPIN]" in out


class TestFreetextProfileIsUnchanged:
    """The default profile's behaviour must not drift — existing callers and
    the original T6 closure claim depend on it."""

    def test_default_profile_is_freetext(self):
        s = "Customer 9876543210 with VPA ram.kumar@okhdfcbank and account 123456789012"
        assert redact_for_llm_prompt(s) == redact_for_llm_prompt(s, profile=PROFILE_FREETEXT)

    def test_freetext_redacts_all_high_risk_classes(self):
        out, count = redact_for_llm_prompt(
            "Customer 9876543210 with VPA ram.kumar@okhdfcbank "
            "and account 123456789012 reported MPIN 1234 issue"
        )
        assert count == 4
        for leaked in ("9876543210", "ram.kumar@okhdfcbank", "123456789012"):
            assert leaked not in out

    def test_freetext_redacts_bare_numeric_runs(self):
        """Unlike PROFILE_DOC — in partner prose a naked digit string is far
        more likely an account/transaction reference than a constant."""
        out, count = redact_for_llm_prompt("ref 123456789012 please check")
        assert count == 1
        assert "123456789012" not in out


class TestEdgeCases:
    def test_empty_and_none_safe(self):
        assert redact_for_llm_prompt("") == ("", 0)
        assert redact_for_llm_prompt("", profile=PROFILE_DOC) == ("", 0)

    def test_unknown_profile_fails_loudly(self):
        """A typo'd profile must not silently fall through to no redaction —
        that would be a silent privacy regression."""
        import pytest
        with pytest.raises(ValueError):
            redact_for_llm_prompt("some text", profile="not-a-profile")

    def test_redaction_is_idempotent(self):
        once, _ = redact_for_llm_prompt("mobile 9876543210")
        twice, n = redact_for_llm_prompt(once)
        assert twice == once and n == 0


class TestRedactDocSections:
    def test_returns_new_dict_and_does_not_mutate_input(self):
        original = {"Overview": "Account No: 123456789012 is the remitter."}
        snapshot = dict(original)
        out, count = redact_doc_sections(original, doc_label="tsd")
        assert count == 1
        assert original == snapshot, "input map must never be mutated"
        assert out is not original

    def test_preserves_headings_and_counts_across_sections(self):
        sections = {
            "Interface Spec": "Timeout: 30000 ms",              # nothing to redact
            "Contact": "mobile number 9876543210",              # one redaction
        }
        out, count = redact_doc_sections(sections, doc_label="brd")
        assert set(out) == set(sections), "headings must be preserved exactly"
        assert count == 1
        assert "30000" in out["Interface Spec"]
        assert "9876543210" not in out["Contact"]

    def test_empty_map_passthrough(self):
        assert redact_doc_sections({}, doc_label="tsd") == ({}, 0)
