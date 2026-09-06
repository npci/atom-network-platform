# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""S-0: the pack contract — canonical bytes, content address, honest refusal.

The golden fixture + the literal hash below are the CROSS-LANGUAGE pins: the
Java simulator (absent repo `oss-a2a-platform`) must reproduce both
byte-for-byte from the same data, or content addressing breaks silently.
Regenerate only on a deliberate contract change, never to make a test pass.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.sim_packs import contract as c

FIXTURE = Path(__file__).parent / "fixtures" / "sim_pack_golden.json"
SCHEMA_SNAPSHOT = (
    Path(__file__).parents[2] / "app" / "services" / "sim_packs" / "pack.schema.json")

# The cross-language pin. Java must derive this exact id from the golden pack.
GOLDEN_PACK_ID = "sha256:7f7a3b75101132af5da2c2f830a857529c21419783721504892cda8092142d6a"


def _golden() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# ── canonical bytes + content address ────────────────────────────────────────

def test_golden_fixture_round_trips_byte_identically():
    raw = FIXTURE.read_text(encoding="utf-8")
    pack = c.validate_pack(json.loads(raw))
    assert c.canonical_json(pack.canonical_dict()) == raw


def test_golden_pack_id_is_pinned_for_the_java_side():
    pack = c.validate_pack(_golden())
    assert pack.pack_id == GOLDEN_PACK_ID
    assert c.content_hash(pack.canonical_dict()) == GOLDEN_PACK_ID


def test_identity_and_provenance_stay_outside_the_hash():
    """Republishing identical behaviour under a new revision (or a new build
    timestamp) keeps its content address; changing behaviour does not."""
    base = _golden()
    relabelled = {**base, "pack_ref": "CHG-4711@4",
                  "provenance": {"generated_at": "2031-01-01T00:00:00+00:00"}}
    assert c.content_hash(relabelled) == c.content_hash(base)

    changed = json.loads(json.dumps(base))
    changed["scenarios"][0]["respond"]["rc"] = "00"
    assert c.content_hash(changed) != c.content_hash(base)


def test_canonical_json_is_sorted_compact_utf8():
    s = c.canonical_json({"b": 1, "a": {"z": "ä"}, "l": [1, 2]})
    assert s == '{"a":{"z":"ä"},"b":1,"l":[1,2]}', \
        "sorted keys, no whitespace, raw UTF-8 — the Java-matching rules"


def test_stamp_then_validate_is_the_publish_path():
    data = _golden()
    data.pop("pack_id")
    stamped = c.stamp(c.validate_pack(data))
    assert stamped.pack_id == GOLDEN_PACK_ID
    c.validate_pack(stamped.canonical_dict())  # integrity gate passes


# ── refusals, each naming its reason ─────────────────────────────────────────

def test_unknown_required_capability_is_named():
    data = _golden()
    data.pop("pack_id")     # requires is hashed content; keep integrity out of the way
    data["requires"] = ["scenario.delay", "sig.v9"]
    with pytest.raises(c.PackValidationError, match="sig.v9"):
        c.validate_pack(data, capabilities={"scenario.delay"})
    # No capability set given -> structural validation only (the gate belongs
    # to whoever talks to a concrete simulator).
    c.validate_pack(data)


def test_edited_after_stamping_is_refused():
    data = _golden()
    data["engine_min"] = "9.9"      # behaviour changed, address kept
    with pytest.raises(c.PackValidationError, match="immutable"):
        c.validate_pack(data)


def test_scenario_when_carries_exactly_one_identity():
    for bad_when in ({}, {"tc_id": "PR_7", "variant_id": "v-1"},
                     {"field": "Payer/@addr"}):
        data = _golden()
        data.pop("pack_id")
        data["scenarios"] = [{"when": bad_when, "respond": {"rc": "ZA"}}]
        with pytest.raises(c.PackValidationError):
            c.validate_pack(data)


def test_pack_ref_grammar_is_narrow():
    """A pack_ref rides in query strings and access logs."""
    for bad in ("CHG 4711@3", "CHG-4711", "a@b@c ", "x@?y"):
        data = _golden()
        data.pop("pack_id")
        data["pack_ref"] = bad
        with pytest.raises(c.PackValidationError):
            c.validate_pack(data)


# ── the contract stays pure and pinned ───────────────────────────────────────

def test_schema_snapshot_matches_the_model():
    assert c.canonical_json(c.json_schema()) == \
        SCHEMA_SNAPSHOT.read_text(encoding="utf-8"), \
        "pack.schema.json is the cross-language snapshot — regenerate it from " \
        "contract.json_schema() when the model deliberately changes"


def test_contract_module_is_pure():
    import inspect

    src = inspect.getsource(c)
    assert "from app." not in src.replace("from app.services.sim_packs", "") \
        and "import app" not in src, \
        "S-0 is pure: stdlib + pydantic only — no models, no DB, no packs"
