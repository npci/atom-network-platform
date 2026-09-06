# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Phase A Excellence — Slice 3 cross-artifact grounding checks.

Direct unit tests for the three new deterministic checks plus an
end-to-end pass through the runner so we know they fire in the real
brd_to_tech_spec checkpoint without LLM.
"""
from __future__ import annotations

import pytest

from app.services.evaluation.deterministic import (
    CHECKS,
    check_no_http_codes_as_domain_errors,
    check_tech_spec_covers_all_brd_frs,
    check_domain_error_codes_are_valid,
    run_checks,
)


def _combined(artifacts: dict[str, dict]) -> dict:
    combined: dict = {}
    for v in artifacts.values():
        if isinstance(v, dict):
            combined.update(v)
    combined["_all"] = artifacts
    return combined


# ── check_tech_spec_covers_all_brd_frs ──────────────────────────────────────

class TestTechSpecCoversBrdFrs:
    def test_clean_when_every_fr_present_in_tech_spec(self):
        c = _combined({
            "brd_document": {"content": "FR-01 ...\nFR-02 ...\n"},
            "tech_spec_document": {
                "content": "## API\nFR-01 is implemented here.\nFR-02 also handled.\n",
            },
        })
        assert check_tech_spec_covers_all_brd_frs(c) == []

    def test_flags_missing_fr(self):
        c = _combined({
            "brd_document": {"content": "FR-01 do this\nFR-02 do that\nFR-03 do that other thing\n"},
            "tech_spec_document": {"content": "## API\nFR-01 is implemented.\n"},
        })
        findings = check_tech_spec_covers_all_brd_frs(c)
        assert len(findings) == 1
        assert "FR-02" in findings[0]
        assert "FR-03" in findings[0]
        assert "missing" in findings[0].lower(), "must contain 'missing' so judge maps to FAIL"

    def test_silent_when_brd_has_no_frs(self):
        c = _combined({
            "brd_document": {"content": "Just prose, no formal requirements yet."},
            "tech_spec_document": {"content": "## API\n"},
        })
        assert check_tech_spec_covers_all_brd_frs(c) == []

    def test_silent_when_either_artifact_missing(self):
        c = _combined({"brd_document": {"content": "FR-01"}})
        assert check_tech_spec_covers_all_brd_frs(c) == []
        c = _combined({"tech_spec_document": {"content": "FR-01"}})
        assert check_tech_spec_covers_all_brd_frs(c) == []


# ── check_domain_error_codes_are_valid ─────────────────────────────────────────

@pytest.fixture
def upi_pack(monkeypatch):
    """Pin the UPI pack for tests whose FIXTURES are UPI codes.

    These assertions are only true under UPI, and used to pass by inheriting
    whatever `DOMAIN_PACK` the shell happened to hold — so they broke the
    moment the suite was run the way the service actually runs (NLLN). Pinning
    makes the domain assumption explicit instead of ambient; the genericity is
    covered separately by `test_code_shape_follows_the_active_domain_pack`.
    """
    from pathlib import Path

    from app.core.domain import registry

    monkeypatch.setenv("DOMAIN_PACK", str(Path(registry.DEFAULT_PACK).resolve()))
    registry._load.cache_clear()
    yield
    registry._load.cache_clear()


class TestDomainErrorCodesAreValid:
    def test_clean_when_at_least_one_upi_code_present(self, upi_pack):
        c = _combined({
            "tech_spec_document": {
                "content": "## Error Code Table\n| U30 | VPA not registered |\n",
            },
        })
        assert check_domain_error_codes_are_valid(c) == []

    def test_clean_when_multiple_upi_codes_present(self, upi_pack):
        c = _combined({
            "tech_spec_document": {
                "content": "## Error Codes\nZ6, U30, RB, XT, 00 are all used.\n",
            },
        })
        assert check_domain_error_codes_are_valid(c) == []

    def test_flags_error_section_without_codes(self):
        c = _combined({
            "tech_spec_document": {
                "content": (
                    "## Error Code Table\n"
                    "Each failure should be returned with the appropriate code.\n"
                    "Codes follow the standard NPCI convention.\n"
                ),
            },
        })
        findings = check_domain_error_codes_are_valid(c)
        assert len(findings) == 1
        # Asserted domain-neutrally on purpose. The code shape now comes from
        # the active domain pack, so the message names THAT domain's shape
        # rather than UPI's — pinning the old "no recognised UPI error" wording
        # would re-hardcode the thing this check was genericised to stop.
        assert "error code" in findings[0].lower()
        assert "code shape" in findings[0].lower()

    def test_code_shape_follows_the_active_domain_pack(self, monkeypatch):
        """The check must grade against the ACTIVE domain's codes, not UPI's.

        Regression for the genericisation sweep. Before this, the UPI alphabet
        was a module constant, so a library-loan spec was graded on whether it
        contained `U30`/`Z6`/`00` — which it never does. It nonetheless PASSED,
        because `00` matched inside an ISO-8601 timestamp (`ts="...T10:00:00Z"`)
        in a sample XML block. Passing for a reason unrelated to error codes is
        worse than failing: the check looked green while asserting nothing.
        """
        from pathlib import Path

        from app.core.domain import registry

        packs = Path(registry.DEFAULT_PACK).resolve().parents[1]
        nlln = packs / "nlln" / "nlln.yaml"
        if not nlln.exists():                       # pack is optional in a trimmed tree
            pytest.skip("nlln pack not present in this checkout")

        # An NLLN error section: real NLLN codes, zero UPI codes, and a
        # timestamp carrying the `00` that used to carry the whole check.
        nlln_spec = _combined({
            "tech_spec_document": {
                "content": (
                    "## Error Code Table\n"
                    '<Head ts="2026-06-01T10:00:00Z"/>\n'
                    "| E004 | No copy of this ISBN is available |\n"
                    "| E009 | Language code is not valid ISO 639-1 |\n"
                ),
            },
        })

        monkeypatch.setenv("DOMAIN_PACK", str(nlln))
        registry._load.cache_clear()
        try:
            assert check_domain_error_codes_are_valid(nlln_spec) == [], \
                "real NLLN codes must satisfy the check under the NLLN pack"

            # And it must still be able to FAIL: strip the codes, keep the
            # timestamp. If the timestamp alone satisfies it, the check is
            # vacuous again.
            no_codes = _combined({
                "tech_spec_document": {
                    "content": (
                        "## Error Code Table\n"
                        '<Head ts="2026-06-01T10:00:00Z"/>\n'
                        "Failures are returned with the appropriate code.\n"
                    ),
                },
            })
            assert len(check_domain_error_codes_are_valid(no_codes)) == 1
        finally:
            registry._load.cache_clear()

    def test_silent_when_no_error_section_present(self):
        c = _combined({"tech_spec_document": {"content": "## API\nNothing else."}})
        assert check_domain_error_codes_are_valid(c) == []


# ── check_no_http_codes_as_domain_errors ───────────────────────────────────────

class TestNoHttpCodesAsNetworkErrors:
    def test_clean_when_only_upi_codes_in_error_section(self):
        c = _combined({
            "tech_spec_document": {"content": "## Error Code Table\n| U30 | bad VPA |\n"},
        })
        assert check_no_http_codes_as_domain_errors(c) == []

    def test_flags_http_codes_in_error_section(self):
        c = _combined({
            "tech_spec_document": {
                "content": "## Error Code Table\nReturn 404 when VPA not found, 500 on other failure.\n",
            },
        })
        findings = check_no_http_codes_as_domain_errors(c)
        assert len(findings) == 1
        assert "HTTP-style" in findings[0]

    def test_silent_when_section_absent(self):
        c = _combined({"tech_spec_document": {"content": "Return 404. Just prose."}})
        assert check_no_http_codes_as_domain_errors(c) == []


# ── Registry integration ────────────────────────────────────────────────────

class TestGroundingChecksRegistered:
    @pytest.mark.parametrize("name", [
        "check_tech_spec_covers_all_brd_frs",
        "check_domain_error_codes_are_valid",
        "check_no_http_codes_as_domain_errors",
    ])
    def test_in_checks_registry(self, name):
        assert name in CHECKS

    def test_run_checks_dispatches_grounding_check(self):
        findings = run_checks(
            ["check_tech_spec_covers_all_brd_frs"],
            {
                "brd_document":      {"content": "FR-01\nFR-02\n"},
                "tech_spec_document": {"content": "## API\nOnly FR-01 covered.\n"},
            },
        )
        assert any("FR-02" in f for f in findings)
