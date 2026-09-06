# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""assumption_handler resolves its blocking keywords and safe defaults from the
ACTIVE domain pack — one domain's PM blockers and invented "safe" defaults must
never leak into another's clarification flow.
"""
from pathlib import Path

from app.agents.assumption_handler import apply
from app.core.domain import registry

_PACKS = Path(registry.__file__).resolve().parents[2] / "packs"


def _activate(monkeypatch, pack_yaml: Path) -> None:
    monkeypatch.setenv("DOMAIN_PACK", str(pack_yaml))
    registry._load.cache_clear()


def test_upi_pack_blocks_its_curated_keywords_and_keeps_its_defaults(monkeypatch):
    _activate(monkeypatch, _PACKS / "network" / "network.yaml")
    try:
        blocking, assumed = apply([
            {"key": "mandate_type_change"},
            {"key": "response_timeout"},
            {"key": "misc_copy_tweak"},
        ])
        assert blocking == ["mandate_type_change"]
        by_key = {g["key"]: g["default"] for g in assumed}
        assert by_key["response_timeout"] == "30 seconds (standard network SLA)"
        assert by_key["misc_copy_tweak"] == "Use Authority standard conventions"
    finally:
        registry._load.cache_clear()


def test_nlln_pack_gets_library_blockers_not_upi_ones(monkeypatch):
    _activate(monkeypatch, _PACKS / "nlln" / "nlln.yaml")
    try:
        blocking, assumed = apply([
            {"key": "mandate_type_change"},    # UPI blocker — must NOT block here
            {"key": "loan_period_extension"},  # NLLN blocker
            {"key": "response_timeout"},       # UPI default must NOT be assumed
        ])
        assert blocking == ["loan_period_extension"]
        by_key = {g["key"]: g["default"] for g in assumed}
        # No invented UPI SLA for a library domain — generic fallback instead.
        assert by_key["response_timeout"] == "Use NLLC standard conventions"
        assert "UPI" not in " ".join(by_key.values())
        assert "NPCI" not in " ".join(by_key.values())
    finally:
        registry._load.cache_clear()


def test_critical_gaps_block_regardless_of_pack_keywords(monkeypatch):
    _activate(monkeypatch, _PACKS / "nlln" / "nlln.yaml")
    try:
        blocking, assumed = apply([{"key": "anything_at_all", "critical": True}])
        assert blocking == ["anything_at_all"]
        assert assumed == []
    finally:
        registry._load.cache_clear()
