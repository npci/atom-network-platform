# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""xml_template_generator — drafts a Mustache XML request template for a new
network API at cert-simulator "Add test cases" time.

Invoked when `tc_store_sync._stub_to_parsed` resolves a TC's flow but the
flow isn't in the cert-simulator catalog yet (i.e. Phase A introduces a
new API like `ReqDispute` and either authored `flow_definitions[]` with
`confidence: low` (empty XML) OR didn't author it at all). The agent
produces a draft template, persisted with `source='llm'` and
`approved_at=NULL`; the SyncDiffModal renders it for operator review, and
`/cert-simulator/apply` refuses to register the flow until an operator
sets `approved_at`.

Design:
  * Single `call_llm` round-trip (not streaming) — output is a single XML
    artifact, no incremental UX. Caller can `await` and decode the result.
  * Few-shot prompt: 2 closest known templates (by flow family) included
    inline as exemplars so the model anchors on the actual authority envelope
    (`<NetworkRequest xmlns="http://example.org/network/schema/">`, `<Head>`, `<Meta>`,
    `<Txn>`, `<CallbackUrl>`).
  * Output validation in-agent: well-formed XML, only the allowed
    placeholder set, must include `<Txn>` + `<CallbackUrl>`. Caller never
    sees invalid output — `generate()` raises `XmlTemplateValidationError`
    instead.
  * `max_tokens=8000` — XML for complex APIs is 4-8KB; AiNxt strips
    `stop_reason`  so we size for the artifact's
    real ceiling instead of trusting finish-reason detection.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.agents._prompt_safety import ANTI_INJECTION_CLAUSE, wrap_untrusted
from app.core.llm import call_llm
from app.services.xml_template_resolver import (
    ALLOWED_PLACEHOLDERS,
    resolve_for_flow,
)

logger = logging.getLogger(__name__)


def _pack_flow_codes() -> list[str]:
    """Flow-family codes this domain declares, in pack order, de-duplicated.

    The exemplar lists used to be the payments catalogue spelled out here
    (PAY / COLLECT / REVERSAL / VALCUST / CHKTXN / BALANCE / REFUND), which
    made every domain few-shot its template generator on flows it does not
    have — and, since `resolve_for_flow` would find none of them, silently
    generate with no exemplars at all. `message_flows:` already declares this
    per pack, so read it.
    """
    from app.core.domain.contract import message_flows_of
    from app.core.domain.registry import get_active_pack

    out: list[str] = []
    for code in message_flows_of(get_active_pack()).values():
        code = (code or "").strip()
        if code and code not in out:
            out.append(code)
    return out


def _exemplar_flows(key: str) -> list[str]:
    """A pack-declared, comma-separated exemplar preference list; empty when
    the pack has no opinion (callers then fall back to declaration order)."""
    from app.core.domain.registry import prompt_block

    return [c.strip() for c in (prompt_block(key, "") or "").split(",") if c.strip()]


# Strip the XML declaration line from exemplars so the prompt is more compact
# and the model is less likely to skip its own declaration in the output.
_XML_DECL_RE = re.compile(r"^\s*<\?xml[^>]+\?>\s*", re.MULTILINE)

# Placeholder pattern — `{{name}}` with optional whitespace.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

# Detect a Markdown ```xml fenced block (some providers wrap output).
_FENCE_RE = re.compile(r"^\s*```(?:xml)?\s*\n(?P<body>[\s\S]*?)\n?```\s*$", re.MULTILINE)


class XmlTemplateValidationError(RuntimeError):
    """Raised when the LLM's output isn't a usable template."""


@dataclass
class GeneratedTemplate:
    """Validated agent output. Caller persists this verbatim."""
    api_name: str
    flow_code: str
    xml: str
    placeholders_used: list[str]
    exemplars_used: list[str]   # flow_codes of the exemplars fed to the LLM


def _pick_exemplars(flow_code: str, direction: str) -> list[tuple[str, str]]:
    """Return up to 2 (flow_code, xml) exemplar pairs from the catalog.

    Exemplar 1 is the domain's canonical envelope (the first flow the pack
    declares, or `xml_exemplar_canonical_flow`). Exemplar 2 is direction-aware:
    a partner-initiated new flow prefers a partner-initiated exemplar, an
    authority-initiated one prefers an authority-initiated exemplar. Both
    preference lists are pack data; a pack that declares neither just walks its
    own flows in declaration order.
    """
    chosen: list[tuple[str, str]] = []
    seen: set[str] = set()
    pack_codes = _pack_flow_codes()

    # Exemplar 1 — the canonical envelope shape.
    canonical = (_exemplar_flows("xml_exemplar_canonical_flow")
                 or pack_codes[:1])
    for code in canonical[:1]:
        xml, _ = resolve_for_flow(code)
        if xml:
            chosen.append((code, xml))
            seen.add(code)

    # Exemplar 2 — steered by who originates the direction. The partner token
    # comes from the pack rather than being the literal "BANK"; the EXTRA
    # tokens a domain also treats as partner-initiated (the network pack's
    # "PSP", which survived the first pass of that same fix) are pack data too.
    from app.core.domain.registry import prompt_block
    from app.services.cert_roles import partner_role

    d = (direction or "").upper()
    partner_tokens = [t for t in
                      [(partner_role() or "").upper()]
                      + [t.strip().upper() for t in
                         (prompt_block("partner_direction_tokens", "") or "").split(",")]
                      if t]
    partner_side = any(t in d for t in partner_tokens)
    key = ("xml_exemplar_flows_partner" if partner_side
           else "xml_exemplar_flows_authority")
    candidates = _exemplar_flows(key) or [c for c in pack_codes if c not in seen]
    for code in candidates:
        if code in seen:
            continue
        xml, _ = resolve_for_flow(code)
        if xml:
            chosen.append((code, xml))
            seen.add(code)
            break

    # Defensive: if both lookups missed, walk the pack's own declaration order.
    if len(chosen) < 2:
        for code in pack_codes:
            if code in seen:
                continue
            xml, _ = resolve_for_flow(code)
            if xml:
                chosen.append((code, xml))
                seen.add(code)
            if len(chosen) >= 2:
                break

    return chosen[:2]


SYSTEM_PROMPT = (
    "You are the network XML Template Generator. Given a new network API name "
    "and 1-2 example templates from the existing catalog, you produce a "
    "Mustache-style XML request body the cert-simulator can render and POST "
    "to a partner's endpoint.\n\n"
    "STRICT RULES (output is rejected if any are violated):\n"
    "1. Output ONLY the XML body — no commentary, no Markdown fences, no leading text.\n"
    "2. Start with `<?xml version=\"1.0\" encoding=\"UTF-8\"?>` followed by a single "
    "`<NetworkRequest xmlns=\"http://example.org/network/schema/\">…</NetworkRequest>` root.\n"
    "3. Include both `<Txn …>` and `<CallbackUrl>…</CallbackUrl>` elements — the "
    "cert-simulator's dispatcher requires them.\n"
    "4. Mustache placeholders are LIMITED to this exact set "
    f"(any other placeholder is rejected): {sorted(ALLOWED_PLACEHOLDERS)}.\n"
    "5. Mirror the envelope of the exemplars: same `<Head>`, `<Meta>`, `<Txn>` "
    "shape; only the inner payload changes for the new API's semantics.\n"
    "6. The `<Txn type=\"…\">` attribute should match the new flow_code "
    "(e.g. `type=\"DISPUTE\"` for `ReqDispute`).\n\n"
    + ANTI_INJECTION_CLAUSE
)


def _strip_fence(text: str) -> str:
    """Some providers wrap output in ```xml…``` despite instructions —
    unwrap once before validating."""
    m = _FENCE_RE.search(text)
    if m:
        return m.group("body").strip()
    return text.strip()


def _validate(xml: str, api_name: str) -> list[str]:
    """Run the in-agent validation gate. Returns sorted placeholders_used.

    Raises XmlTemplateValidationError on any failure — caller treats this
    as "model botched the output, surface to operator for manual fill".
    """
    if not xml:
        raise XmlTemplateValidationError("empty output")

    # 1. Allowed placeholders only.
    placeholders = set(_PLACEHOLDER_RE.findall(xml))
    extras = placeholders - ALLOWED_PLACEHOLDERS
    if extras:
        raise XmlTemplateValidationError(
            f"placeholders outside the allowed set: {sorted(extras)}"
        )

    # 2. Required elements present (textual check — lxml is the gold standard
    #    but adds a runtime dep we don't otherwise need on the cert-platform
    #    image; a textual sweep is correct for the templates the cert-agent
    #    actually dispatches).
    if "<Txn" not in xml:
        raise XmlTemplateValidationError("missing required <Txn …> element")
    if "<CallbackUrl>" not in xml:
        raise XmlTemplateValidationError("missing required <CallbackUrl> element")

    # 3. Outer envelope must be NetworkRequest with the schema namespace, so
    #    the cert-simulator's request handler recognises it. Substring check
    #    rather than parse so a `xmlns:foo="…"` second attribute still passes.
    if 'http://example.org/network/schema/' not in xml:
        raise XmlTemplateValidationError("missing required xmlns=\"http://example.org/network/schema/\"")
    if "<NetworkRequest" not in xml:
        raise XmlTemplateValidationError("missing required <NetworkRequest> root")

    # 4. Cheapest well-formedness check: try minidom parse with the
    #    placeholders left as-is (Mustache placeholders are valid XML text).
    try:
        from xml.dom.minidom import parseString
        parseString(xml)
    except Exception as exc:  # noqa: BLE001
        raise XmlTemplateValidationError(f"XML not well-formed: {exc}") from exc

    return sorted(placeholders)


async def generate(
    *,
    api_name: str,
    flow_code: str,
    direction: str = "",
    role: str = "",
    description: str = "",
) -> GeneratedTemplate:
    """Synthesize a Mustache XML template for a new network API.

    Caller chain: SyncDiffModal `/diff` → cert_simulator_sync → resolver →
    here. Returns a validated `GeneratedTemplate`. Raises
    `XmlTemplateValidationError` when the model produces unusable output —
    the operator then types the XML manually in the diff modal.

    Args:
      api_name: wire API name from the stub (e.g. `ReqDispute`)
      flow_code: uppercase flow code (e.g. `DISPUTE`)
      direction: partner-initiated / authority-initiated (informs exemplar selection)
      role: participant role (informs Txn elements only, optional)
      description: free-text BRD/circular hint, when available
    """
    api_name = (api_name or "").strip()
    flow_code = (flow_code or "").strip().upper()
    if not api_name or not flow_code:
        raise XmlTemplateValidationError("api_name and flow_code are required")

    exemplars = _pick_exemplars(flow_code, direction)
    exemplar_block = "\n\n".join(
        f"### Exemplar: {code}\n```xml\n{_XML_DECL_RE.sub('', xml).strip()}\n```"
        for code, xml in exemplars
    )

    user_msg = (
        f"Generate the Mustache XML request template for the new network API "
        f"`{api_name}` (flow_code `{flow_code}`).\n"
        f"Direction: {direction or 'unspecified'}\n"
        f"Role: {role or 'unspecified'}\n"
        f"Description: {wrap_untrusted(description, 'API_DESCRIPTION') if description else '(none provided)'}\n\n"
        f"Existing catalog exemplars to mirror:\n\n{exemplar_block}\n\n"
        f"Produce ONLY the XML body — no commentary, no fences."
    )

    raw = await call_llm(
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
        max_tokens=8000,
        agent_name="xml_template_generator",
    )
    xml = _strip_fence(raw)
    placeholders = _validate(xml, api_name)
    logger.info(
        "xml_template_generator: api=%s flow=%s exemplars=%s placeholders=%s len=%d",
        api_name, flow_code, [c for c, _ in exemplars], placeholders, len(xml),
    )
    return GeneratedTemplate(
        api_name=api_name,
        flow_code=flow_code,
        xml=xml,
        placeholders_used=placeholders,
        exemplars_used=[c for c, _ in exemplars],
    )
