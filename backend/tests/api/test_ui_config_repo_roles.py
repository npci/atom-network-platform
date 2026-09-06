# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""`/config/ui` carries the active pack's repo topology to the SPA.

WHY THIS IS LOAD-BEARING: the repo-selection screens (AnalysisPanel, XSD) used
to hardcode UPI's "exactly one core + one app" rule, which made a single-repo
domain unable to start a run at all — the UI refused to submit a selection the
BACKEND would have accepted. Those screens now derive their rule from this
payload, so if the key silently disappears every domain quietly falls back to
the permissive single-repo default and UPI loses its two-repo guardrail with no
error anywhere. That is exactly the kind of silent degradation this suite exists
to catch.

Semantics must stay identical to `app.agents.repo_scope.validate_selection`,
which is the authoritative gate; this payload only lets the client pre-empt the
same 400.
"""
from app.api.agents import get_ui_config


def test_config_ui_exposes_repo_roles():
    cfg = get_ui_config()
    assert "repo_roles" in cfg, (
        "the SPA's repo-selection rule reads this key; dropping it silently "
        "reverts every domain to the permissive single-repo default"
    )
    assert isinstance(cfg["repo_roles"], list)


def test_declared_roles_round_trip_with_the_fields_the_spa_reads(monkeypatch, tmp_path):
    """The SPA reads key/label/required/multiple/builds_first off each entry.
    A renamed or dropped field would break the client rule without failing any
    server-side test, so pin the wire shape here."""
    from app.core.domain import registry

    pack = tmp_path / "topo.yaml"
    pack.write_text(
        "key: topo\n"
        "repo_roles:\n"
        "  - {key: core, label: Core, required: true, multiple: false, builds_first: true}\n"
        "  - {key: app, required: true}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOMAIN_PACK", str(pack))
    registry._load.cache_clear()
    try:
        roles = get_ui_config()["repo_roles"]
        assert [r["key"] for r in roles] == ["core", "app"]
        for field in ("key", "label", "required", "multiple", "builds_first"):
            assert field in roles[0], f"SPA reads {field!r} off every role entry"
        assert roles[0]["builds_first"] is True and roles[1]["builds_first"] is False
    finally:
        registry._load.cache_clear()


def test_a_domain_declaring_no_topology_sends_an_empty_list(monkeypatch, tmp_path):
    """Empty is the meaningful default (single-repo), NOT an error — the client
    shows a 'no topology configured' notice rather than blocking the user."""
    from app.core.domain import registry

    pack = tmp_path / "bare.yaml"
    pack.write_text("key: bare\n", encoding="utf-8")
    monkeypatch.setenv("DOMAIN_PACK", str(pack))
    registry._load.cache_clear()
    try:
        assert get_ui_config()["repo_roles"] == []
    finally:
        registry._load.cache_clear()


def test_a_broken_pack_degrades_to_empty_rather_than_500ing_the_whole_config(monkeypatch):
    """`/config/ui` is fetched before auth and drives dev_mode + the simulator
    link too. A pack that cannot resolve must not blank the entire SPA config."""
    from app.api import agents as agents_api

    def _boom():
        raise RuntimeError("pack exploded")

    monkeypatch.setattr(agents_api, "get_ui_config", get_ui_config, raising=False)
    monkeypatch.setattr("app.core.domain.registry.get_active_pack", _boom, raising=True)

    cfg = get_ui_config()
    assert cfg["repo_roles"] == []
    assert cfg["dev_mode"] is not None, "the rest of the config must survive"
