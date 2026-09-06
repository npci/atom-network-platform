# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Pack-versus-pack diff (SIM S-5) — what publishing this would change.

Pure: two resolved pack contents in, a structured difference out. The review
surface an operator reads BEFORE publishing, because a pack changes what
"certified" means and the coverage number alone does not say what moved.

Field-level, not just API-level. "ReqTransfer changed" is not reviewable; "the
enum on ReqTransfer/Txn/@type lost PRE_ARB" is. So APIs are compared by name,
fields within them by path, and each changed field reports its changed
CONSTRAINT CELLS with before/after — the same six kinds the grader asserts
and the simulator enforces, which is why a diff here predicts what will
start failing.

Scenarios are compared by their identity (`when`), not by list position: a
reordering is not a behaviour change, but the same identity answering a
different response code is exactly the change worth catching.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = ["diff_packs"]

_CELLS = ("occurrence", "datatype", "length_rule", "mandatory",
          "condition_text", "enum_values", "pattern_rule")


def _by_api(content: Mapping[str, Any]) -> dict[str, dict]:
    return {str(a.get("api", "")).lower(): a for a in content.get("apis", [])}


def _by_path(api: Mapping[str, Any]) -> dict[str, dict]:
    return {str(f.get("path", "")): f for f in api.get("fields", [])}


def _scenario_key(s: Mapping[str, Any]) -> str:
    when = s.get("when", {}) or {}
    for k in ("variant_id", "tc_id"):
        if when.get(k):
            return f"{k}:{when[k]}"
    if when.get("field"):
        return f"field:{when['field']}={when.get('eq')}"
    return "unknown"


def _field_changes(before: Mapping[str, Any],
                   after: Mapping[str, Any]) -> dict[str, dict]:
    """Only the constraint cells that actually moved."""
    moved: dict[str, dict] = {}
    for cell in _CELLS:
        was, now = before.get(cell), after.get(cell)
        if was != now:
            moved[cell] = {"from": was, "to": now}
    return moved


def _api_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict:
    fb, fa = _by_path(before), _by_path(after)
    changed = {}
    for path in sorted(set(fb) & set(fa)):
        cells = _field_changes(fb[path], fa[path])
        if cells:
            changed[path] = cells
    out: dict[str, Any] = {
        "fields_added": sorted(set(fa) - set(fb)),
        "fields_removed": sorted(set(fb) - set(fa)),
        "fields_changed": changed,
    }
    for key in ("route", "wire_format", "request_template", "response_template"):
        if before.get(key) != after.get(key):
            out.setdefault("other", {})[key] = {
                "from": before.get(key), "to": after.get(key)}
    return out


def diff_packs(before: Mapping[str, Any] | None,
               after: Mapping[str, Any]) -> dict:
    """What `after` changes relative to `before`. A `before` of None is a
    first publication — everything is added, stated as such rather than
    rendered as a diff against an imaginary empty pack."""
    if before is None:
        apis = sorted(a.get("api", "") for a in after.get("apis", []))
        return {
            "baseline": None,
            "apis_added": apis, "apis_removed": [], "apis_changed": {},
            "scenarios_added": sorted(_scenario_key(s)
                                      for s in after.get("scenarios", [])),
            "scenarios_removed": [], "scenarios_changed": {},
            "coverage": {"from": None,
                         "to": (after.get("provenance") or {}).get("coverage")},
            "first_publication": True,
        }

    ab, aa = _by_api(before), _by_api(after)
    changed: dict[str, dict] = {}
    for name in sorted(set(ab) & set(aa)):
        d = _api_diff(ab[name], aa[name])
        if d["fields_added"] or d["fields_removed"] or d["fields_changed"] \
                or d.get("other"):
            changed[aa[name].get("api", name)] = d

    sb = {_scenario_key(s): s for s in before.get("scenarios", [])}
    sa = {_scenario_key(s): s for s in after.get("scenarios", [])}
    scen_changed = {
        k: {"from": sb[k].get("respond"), "to": sa[k].get("respond")}
        for k in sorted(set(sb) & set(sa))
        if sb[k].get("respond") != sa[k].get("respond")
    }

    return {
        "baseline": before.get("pack_ref"),
        "apis_added": sorted(aa[n].get("api", n) for n in set(aa) - set(ab)),
        "apis_removed": sorted(ab[n].get("api", n) for n in set(ab) - set(aa)),
        "apis_changed": changed,
        "scenarios_added": sorted(set(sa) - set(sb)),
        "scenarios_removed": sorted(set(sb) - set(sa)),
        "scenarios_changed": scen_changed,
        "coverage": {
            "from": (before.get("provenance") or {}).get("coverage"),
            "to": (after.get("provenance") or {}).get("coverage"),
        },
        "first_publication": False,
    }
