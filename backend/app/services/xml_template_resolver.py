# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Resolves an API name → Mustache XML template for the cert simulator.

Lookup precedence:
  1. `network_xml_templates` table (Postgres) — LLM-generated or operator-edited
     templates persisted after a confirmed flow registration. Source =
     'llm' (awaiting approval), 'operator' (manually edited), or 'catalog'
     (mirrored snapshot).
  2. The active pack's `wire_templates.catalog:` — the same SHAPE as
     cert-agent's `xml_template_store._DEFAULTS`, with the envelope identity
     (root QName, namespace, orgId) held as sentinels and substituted from the
     pack's `wire_envelope:` — see `_WIRE_DEFAULTS`. Reading a local mirror
     (rather than calling cert-agent over HTTP at parse time) keeps
     `_stub_to_parsed` pure + synchronous and avoids the bootstrapping issue
     where the SyncDiffModal opens before cert-agent has flow_registry
     populated.
  3. None — caller should invoke `xml_template_generator` to
     synthesize a template for the new API.

When you bump cert-agent's catalog, also bump the deployment pack's
`wire_templates.catalog:`. The mirror is small and the duplication is justified
by the latency + bootstrapping wins.

`resolve()` is sync because every caller (`_stub_to_parsed`,
`/cert-simulator/diff`) is already synchronous. The LLM fallback path
(`resolve_or_generate`) is async and reaches the agent + DB cache.
"""
from __future__ import annotations

import re
from typing import Literal


TemplateSource = Literal["catalog", "llm", "operator", None]


# The pack's catalog mirrors certagent/cert-agent/app/certification/
# xml_template_store.py:_DEFAULTS. Pack templates carry `__WIRE_*__` sentinels
# that the cert-agent copy does not, so the two are NOT byte-identical and
# cannot be — an older note here said otherwise and would send the next person
# to sync them the wrong way. Keep the STRUCTURE aligned: same entries, same
# placeholders, and if cert-agent's dispatcher requires a new one, update both.
# ── Wire envelope identity ───────────────────────────────────────────────────
#
# The root QName, its namespace and the originator id are a WIRE CONTRACT: the
# counterparty validates generated payloads against a schema, so these must be
# the values THAT deployment's partner expects. They are therefore pack DATA
# (`wire_envelope:`), not constants of this repository.
#
# The values below are generic PLACEHOLDERS, not any real ecosystem's contract.
# A deployment talking to an existing counterparty MUST declare its own under
# `wire_envelope:` in its domain pack, e.g.
#
#     wire_envelope:
#       root_element: ReqPayload
#       namespace:    http://schemas.example-authority.test/v2/
#       org_id:       ACME_CERT
#
# This indirection exists because the identity was once hardcoded here and a
# vocabulary sweep changed it in place — every generated request would have
# been rejected by a schema-validating partner, with no code path looking wrong.
_WIRE_DEFAULTS = {
    "root_element": "NetworkRequest",
    "namespace":    "http://example.org/network/schema/",
    "org_id":       "AUTH_CERT",
}


# An XML element name (NCName, minus the characters we have no reason to allow).
# `root_element` becomes a TAG, so unlike the other two it cannot be escaped into
# safety — a bad value has to be refused.
_NCNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def _wire_envelope() -> dict[str, str]:
    """Pack-declared envelope identity, falling back to the placeholders.

    Deliberately UNGUARDED. A misconfigured `DOMAIN_PACK` (`UnknownPackError`)
    or a pack whose `wire_envelope:` is missing or incomplete (`ConfigPackError`)
    must propagate. `config_pack.load()` forces exactly those errors at boot so a
    deployment learns then; swallowing them here reinstates the placeholder
    envelope that the loader exists to prevent — a `<NetworkRequest
    xmlns="http://example.org/network/schema/">` going to a schema-validating
    counterparty that rejects every request, with nothing logged.

    `wire_envelope_of` already returns `{}` for a pack that simply declares no
    envelope, so no benign exception remains for a handler to catch.
    """
    from app.core.domain.contract import wire_envelope_of
    from app.core.domain.registry import get_active_pack

    values = dict(_WIRE_DEFAULTS)
    declared = wire_envelope_of(get_active_pack())
    for key in values:
        if declared.get(key):
            values[key] = declared[key]

    # Making the wire identity configurable put pack-supplied text directly into
    # XML. `_cert_mapping` checks only that the keys are present and non-empty,
    # so validation has to happen somewhere before interpolation: an unescaped
    # `&` in a namespace, or a space in the root element, produces a payload that
    # is not well-formed and fails at the counterparty's parser rather than here.
    root = values["root_element"]
    if not _NCNAME_RE.match(root):
        raise ValueError(
            f"wire_envelope.root_element must be a valid XML element name, got {root!r}"
        )
    return values


def _apply_envelope(xml: str) -> str:
    """Substitute the envelope sentinels in a catalog template.

    `namespace` and `org_id` land inside double-quoted attribute values, so they
    are escaped for that context; `root_element` is validated as an NCName in
    `_wire_envelope` instead, a tag name having no escaping to fall back on.
    """
    env = _wire_envelope()
    attr = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}

    def _esc(value: str) -> str:
        return "".join(attr.get(ch, ch) for ch in value)

    return (xml.replace("__WIRE_ROOT__", env["root_element"])
               .replace("__WIRE_NS__", _esc(env["namespace"]))
               .replace("__WIRE_ORG__", _esc(env["org_id"])))


# ── The catalog itself ───────────────────────────────────────────────────────
#
# The templates used to live here as a module constant, and they were one
# ecosystem's message set: its parties, its credential blocks, its account
# identifiers. That made this repository's answer for a library-lending or a
# charge-point deployment a payment request, which the counterparty's schema
# rejects — and nothing in the code path looks wrong.
#
# The catalog is pack DATA now (`wire_templates.catalog:`), by the same argument
# as `wire_envelope:` above. THE PLATFORM SHIPS NONE. That is a safe default:
# `resolve_for_flow` returning (None, None) already routes the caller to
# `xml_template_generator`, which is exactly what happened for any flow code the
# old catalog did not cover.
def _catalog() -> dict[str, str]:
    from app.core.domain.contract import wire_templates_of
    from app.core.domain.registry import get_active_pack

    return dict(wire_templates_of(get_active_pack()).catalog)


# Mustache placeholders a GENERATED template may use (contract README §65).
# Enforced for `xml_template_generator` output only — catalog templates use a
# richer set that the counterparty's dispatcher already substitutes.
#
# The fallback below is deliberately domain-NEUTRAL: every ecosystem has a
# correlation id, an amount-equivalent and a callback, but only some have an
# addressing handle, so the handle names come from the pack rather than from
# here. A pack declares its own under `wire_templates.placeholders:`.
_NEUTRAL_PLACEHOLDERS: frozenset[str] = frozenset({
    "txn_id", "run_id", "tc_id", "correlation_id",
    "amount", "currency", "remarks", "timestamp", "callback_url",
})


def allowed_placeholders() -> frozenset[str]:
    """The placeholder names a generated template may use, from the active pack."""
    from app.core.domain.contract import wire_templates_of
    from app.core.domain.registry import get_active_pack

    declared = wire_templates_of(get_active_pack()).placeholders
    return frozenset(declared) if declared else _NEUTRAL_PLACEHOLDERS


def __getattr__(name: str):
    """`ALLOWED_PLACEHOLDERS` stayed importable as a module attribute.

    It is now pack-derived, so it cannot be a literal evaluated at import — the
    active pack is not necessarily resolvable that early. PEP 562 defers the
    lookup to first ACCESS, which keeps `from … import ALLOWED_PLACEHOLDERS`
    working for `app.agents.xml_template_generator` (it bakes the set into its
    system prompt at module scope) without loading the pack when this module is
    merely imported. New code should call `allowed_placeholders()`.
    """
    if name == "ALLOWED_PLACEHOLDERS":
        return allowed_placeholders()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def resolve_for_flow(flow_code: str) -> tuple[str | None, TemplateSource]:
    """Look up a Mustache XML template by `flow_code` (PAY, COLLECT, ...).

    Catalog-only; does not consult the LLM cache. Returns (template, source)
    or (None, None) when the flow is unknown — including when the active pack
    declares no catalog at all, which routes the caller to the generator.

    For TCs whose flow is built-in, callers should pass the result through
    to cert-agent's TC `request_xml_template` field so the dispatched XML
    is self-contained instead of relying on cert-agent's own fallback at
    dispatch time. (Self-contained = the operator sees the actual XML in
    the cert-agent UI and can edit per-TC before runs.)
    """
    if not flow_code:
        return None, None
    code = flow_code.strip().upper()
    tpl = _catalog().get(code)
    if tpl is not None:
        # Envelope identity is stamped HERE, not stored in the catalog, so a
        # pack change takes effect without re-seeding templates.
        return _apply_envelope(tpl), "catalog"
    return None, None


def known_flows() -> set[str]:
    """Returns the set of flow_codes the active pack's catalog declares. Used by
    `_stub_to_parsed` and tests."""
    return set(_catalog())


# ── DB-backed cache + LLM fallback ────────────────────────────────────────────

async def resolve_or_generate(
    *,
    db,
    api_name: str,
    flow_code: str,
    direction: str = "",
    role: str = "",
    description: str = "",
) -> tuple[str | None, TemplateSource, bool]:
    """Resolve XML for a per-step `api_name` with LLM-fallback.

    Lookup chain (first hit wins):
      1. `network_xml_templates` row keyed by api_name (Postgres cache —
         operator-edited or already-approved LLM drafts).
      2. Catalog mirror by flow_code (built-in APIs).
      3. LLM via `xml_template_generator.generate()` — persists the draft
         with `source='llm'`, `approved_at=NULL`. Caller is responsible
         for the operator approval gate.

    Returns (template, source, requires_approval).
      * template:   the XML body (None on hard failure)
      * source:     'catalog' | 'llm' | 'operator' | None
      * requires_approval: True only when the row was just LLM-generated
        and has no approval yet. The /cert-simulator/apply path uses
        this to refuse flow registration until the operator clicks
        Approve in the SyncDiffModal.
    """
    from app.models.cert_sync import NetworkXmlTemplate  # local: avoid cycles

    api_name = (api_name or "").strip()
    flow_code = (flow_code or "").strip().upper()
    if not api_name:
        return None, None, False

    # 1. Postgres cache — exact match on api_name. An operator-edited or
    #    previously-approved LLM row wins over the catalog (the catalog is
    #    keyed by flow_code, which is coarser).
    row = db.get(NetworkXmlTemplate, api_name)
    if row and row.xml_template:
        requires = row.source == "llm" and row.approved_at is None
        return row.xml_template, row.source, requires

    # 2. Catalog mirror by flow_code.
    tpl, src = resolve_for_flow(flow_code)
    if tpl is not None:
        return tpl, src, False

    # 3. LLM generation. Caches the result so subsequent diffs don't re-prompt
    #    the model. Persist + return immediately; operator approves later via
    #    the SyncDiffModal's Approve button (POST /cert-simulator/templates/{api_name}/approve).
    from app.agents.xml_template_generator import (
        XmlTemplateValidationError,
        generate,
    )
    try:
        generated = await generate(
            api_name=api_name,
            flow_code=flow_code,
            direction=direction,
            role=role,
            description=description,
        )
    except XmlTemplateValidationError:
        # Operator types the XML manually in the modal; leave the cache
        # untouched so a retry can try the model again.
        return None, None, False

    row = NetworkXmlTemplate(
        api_name=generated.api_name,
        flow_code=generated.flow_code,
        xml_template=generated.xml,
        placeholders_used=generated.placeholders_used,
        source="llm",
    )
    db.add(row)
    db.flush()
    return generated.xml, "llm", True


def approve(
    *,
    db,
    api_name: str,
    actor_user_id: str,
    edited_xml: str | None = None,
) -> tuple[bool, str]:
    """Mark a cached LLM draft as approved.

    When `edited_xml` is supplied, the operator is approving an edited
    version — the row is upgraded to `source='operator'` so it's no longer
    "LLM-pending" in any downstream check. Otherwise the LLM draft is
    approved as-is.

    Returns (ok, reason).
    """
    from datetime import datetime, timezone
    from app.models.cert_sync import NetworkXmlTemplate

    api_name = (api_name or "").strip()
    if not api_name:
        return False, "api_name is required"
    row = db.get(NetworkXmlTemplate, api_name)
    if not row:
        return False, f"no template cached for api_name={api_name!r}"
    now = datetime.now(timezone.utc)
    if edited_xml is not None and edited_xml.strip() != row.xml_template.strip():
        row.xml_template = edited_xml
        row.source = "operator"
        # Re-validate the edited XML against the same gate the agent uses.
        from app.agents.xml_template_generator import _validate, XmlTemplateValidationError
        try:
            row.placeholders_used = _validate(edited_xml, api_name)
        except XmlTemplateValidationError as exc:
            return False, f"edited XML failed validation: {exc}"
    row.approved_by = actor_user_id
    row.approved_at = now
    row.updated_at = now
    db.flush()
    return True, "ok"
