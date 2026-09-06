# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Resolves an API name → Mustache XML template for the cert simulator.

Lookup precedence:
  1. `upi_xml_templates` table (Postgres) — LLM-generated or operator-edited
     templates persisted after a confirmed flow registration. Source =
     'llm' (awaiting approval), 'operator' (manually edited), or 'catalog'
     (mirrored snapshot).
  2. `_CATALOG` mirror below — kept byte-identical to cert-agent's
     `xml_template_store._DEFAULTS`. Mirroring (rather than calling
     cert-agent over HTTP at parse time) keeps `_stub_to_parsed` pure +
     synchronous and avoids the bootstrapping issue where the SyncDiffModal
     opens before cert-agent has flow_registry populated.
  3. None — caller should invoke `xml_template_generator` (Phase 3b) to
     synthesize a template for the new API.

When you bump cert-agent's catalog, also bump `_CATALOG` here. The mirror
is small (~10 entries, ~150 lines) and the duplication is justified by
the latency + bootstrapping wins;   mirror-primitive
convention.

`resolve()` is sync because every caller (`_stub_to_parsed`,
`/cert-simulator/diff`) is already synchronous. The LLM fallback path
(`resolve_or_generate`) is async and reaches the agent + DB cache.
"""
from __future__ import annotations

from typing import Literal


TemplateSource = Literal["catalog", "llm", "operator", None]


# Mirror of certagent/cert-agent/app/certification/xml_template_store.py:_DEFAULTS.
# Keep these byte-identical to the cert-agent copy. If cert-agent's dispatcher
# is updated to require a new placeholder, update both.
_CATALOG: dict[str, str] = {
    "PAY": """<?xml version="1.0" encoding="UTF-8"?>
<NetworkRequest xmlns="http://example.org/network/schema/">
  <Head ver="2.0" ts="{{timestamp}}" orgId="AUTH_CERT"/>
  <Meta tc_id="{{tc_id}}" run_id="{{run_id}}"/>
  <Txn id="{{correlation_id}}" refId="{{correlation_id}}" type="PAY"
       note="{{remarks}}" ts="{{timestamp}}"/>
  <Payer addr="{{payer_vpa}}" code="CERT" type="PERSON">
    <Amount cur="{{currency}}" value="{{amount}}"/>
  </Payer>
  <Payees>
    <Payee addr="{{payee_vpa}}" code="CERT" type="ENTITY"/>
  </Payees>
  <CallbackUrl>{{callback_url}}</CallbackUrl>
</NetworkRequest>""",
    "COLLECT": """<?xml version="1.0" encoding="UTF-8"?>
<NetworkRequest xmlns="http://example.org/network/schema/">
  <Head ver="2.0" ts="{{timestamp}}" orgId="AUTH_CERT"/>
  <Meta tc_id="{{tc_id}}" run_id="{{run_id}}"/>
  <Txn id="{{correlation_id}}" refId="{{correlation_id}}" type="COLLECT"
       note="{{remarks}}" ts="{{timestamp}}" expiry="{{collect_expiry}}"/>
  <Payer addr="{{payer_vpa}}" code="CERT" type="PERSON">
    <Amount cur="{{currency}}" value="{{amount}}"/>
  </Payer>
  <Payees>
    <Payee addr="{{payee_vpa}}" code="CERT" type="ENTITY"/>
  </Payees>
  <CallbackUrl>{{callback_url}}</CallbackUrl>
</NetworkRequest>""",
    "MANDATE": """<?xml version="1.0" encoding="UTF-8"?>
<NetworkRequest xmlns="http://example.org/network/schema/">
  <Head ver="2.0" ts="{{timestamp}}" orgId="AUTH_CERT"/>
  <Meta tc_id="{{tc_id}}" run_id="{{run_id}}"/>
  <Txn id="{{correlation_id}}" refId="{{correlation_id}}" type="MANDATE" ts="{{timestamp}}"/>
  <Mandate type="{{mandate_type}}" frequency="{{frequency}}">
    <Payer addr="{{payer_vpa}}" code="CERT">
      <Amount cur="{{currency}}" value="{{amount}}"/>
    </Payer>
    <Payee addr="{{payee_vpa}}" code="CERT"/>
  </Mandate>
  <CallbackUrl>{{callback_url}}</CallbackUrl>
</NetworkRequest>""",
    "REFUND": """<?xml version="1.0" encoding="UTF-8"?>
<NetworkRequest xmlns="http://example.org/network/schema/">
  <Head ver="2.0" ts="{{timestamp}}" orgId="AUTH_CERT"/>
  <Meta tc_id="{{tc_id}}" run_id="{{run_id}}"/>
  <Txn id="{{correlation_id}}" refId="{{correlation_id}}" type="REFUND"
       note="{{remarks}}" ts="{{timestamp}}"/>
  <Payer addr="{{payer_vpa}}" code="CERT" type="ENTITY">
    <Amount cur="{{currency}}" value="{{amount}}"/>
  </Payer>
  <Payees>
    <Payee addr="{{payee_vpa}}" code="CERT" type="PERSON"/>
  </Payees>
  <CallbackUrl>{{callback_url}}</CallbackUrl>
</NetworkRequest>""",
    "BALANCE": """<?xml version="1.0" encoding="UTF-8"?>
<NetworkRequest xmlns="http://example.org/network/schema/">
  <Head ver="2.0" ts="{{timestamp}}" orgId="AUTH_CERT"/>
  <Meta tc_id="{{tc_id}}" run_id="{{run_id}}"/>
  <Txn id="{{correlation_id}}" refId="{{correlation_id}}" type="BAL_ENQUIRY" ts="{{timestamp}}"/>
  <Payer addr="{{payer_vpa}}" code="CERT" type="PERSON"/>
  <CallbackUrl>{{callback_url}}</CallbackUrl>
</NetworkRequest>""",
    "VALCUST": """<?xml version="1.0" encoding="UTF-8"?>
<NetworkRequest xmlns="http://example.org/network/schema/">
  <Head ver="2.0" ts="{{timestamp}}" orgId="AUTH_CERT"/>
  <Meta tc_id="{{tc_id}}" run_id="{{run_id}}"/>
  <Txn id="{{correlation_id}}" refId="{{correlation_id}}" type="VALCUST" ts="{{timestamp}}"/>
  <Payer addr="{{payer_vpa}}" code="CERT" type="PERSON">
    <Creds>
      <Cred type="PIN" subType="MPIN" format="{{mpin_format}}"/>
    </Creds>
    <Ac ifsc="{{ifsc}}" acType="{{account_type}}"/>
  </Payer>
  <CallbackUrl>{{callback_url}}</CallbackUrl>
</NetworkRequest>""",
    "CHKTXN": """<?xml version="1.0" encoding="UTF-8"?>
<NetworkRequest xmlns="http://example.org/network/schema/">
  <Head ver="2.0" ts="{{timestamp}}" orgId="AUTH_CERT"/>
  <Meta tc_id="{{tc_id}}" run_id="{{run_id}}"/>
  <Txn id="{{correlation_id}}" refId="{{correlation_id}}" type="CHKTXN" ts="{{timestamp}}"
       orgTxnId="{{original_txn_id}}"/>
  <Payer addr="{{payer_vpa}}" code="CERT" type="PERSON"/>
  <CallbackUrl>{{callback_url}}</CallbackUrl>
</NetworkRequest>""",
    "REVERSAL": """<?xml version="1.0" encoding="UTF-8"?>
<NetworkRequest xmlns="http://example.org/network/schema/">
  <Head ver="2.0" ts="{{timestamp}}" orgId="AUTH_CERT"/>
  <Meta tc_id="{{tc_id}}" run_id="{{run_id}}"/>
  <Txn id="{{correlation_id}}" refId="{{correlation_id}}" type="REVERSAL"
       orgTxnId="{{original_txn_id}}" ts="{{timestamp}}"/>
  <Payer addr="{{payer_vpa}}" code="CERT" type="PERSON">
    <Amount cur="{{currency}}" value="{{amount}}"/>
  </Payer>
  <Payees>
    <Payee addr="{{payee_vpa}}" code="CERT" type="ENTITY"/>
  </Payees>
  <CallbackUrl>{{callback_url}}</CallbackUrl>
</NetworkRequest>""",
    "MINISTMT": """<?xml version="1.0" encoding="UTF-8"?>
<NetworkRequest xmlns="http://example.org/network/schema/">
  <Head ver="2.0" ts="{{timestamp}}" orgId="AUTH_CERT"/>
  <Meta tc_id="{{tc_id}}" run_id="{{run_id}}"/>
  <Txn id="{{correlation_id}}" refId="{{correlation_id}}" type="MINISTMT" ts="{{timestamp}}"/>
  <Payer addr="{{payer_vpa}}" code="CERT" type="PERSON"/>
  <CallbackUrl>{{callback_url}}</CallbackUrl>
</NetworkRequest>""",
}


# Allowed Mustache placeholders for LLM-generated templates (contract README §65).
# This is enforced for `xml_template_generator` output only — built-in catalog
# templates use a richer set that cert-agent's dispatcher already substitutes.
ALLOWED_PLACEHOLDERS: frozenset[str] = frozenset({
    "txn_id", "run_id", "tc_id", "correlation_id",
    "payer_vpa", "payee_vpa", "amount", "currency",
    "remarks", "timestamp", "callback_url",
})


def resolve_for_flow(flow_code: str) -> tuple[str | None, TemplateSource]:
    """Look up a Mustache XML template by `flow_code` (PAY, COLLECT, ...).

    Catalog-only; does not consult the LLM cache. Returns (template, source)
    or (None, None) when the flow is unknown.

    For TCs whose flow is built-in, callers should pass the result through
    to cert-agent's TC `request_xml_template` field so the dispatched XML
    is self-contained instead of relying on cert-agent's own fallback at
    dispatch time. (Self-contained = the operator sees the actual XML in
    the cert-agent UI and can edit per-TC before runs.)
    """
    if not flow_code:
        return None, None
    code = flow_code.strip().upper()
    tpl = _CATALOG.get(code)
    if tpl is not None:
        return tpl, "catalog"
    return None, None


def known_flows() -> set[str]:
    """Returns the set of flow_codes the catalog ships with. Used by
    `_stub_to_parsed` and tests."""
    return set(_CATALOG.keys())


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
      1. `upi_xml_templates` row keyed by api_name (Postgres cache —
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
