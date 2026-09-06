# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Generic outbound A2A client.

Wraps `a2a.client.ClientFactory` so any backend can send a Task to a
remote agent (partner / cert-agent / the Authority) by URL, with optional Bearer
auth. Cert-agent's outbound code (`certagent/cert-agent/app/a2a/client.py`)
remains the reference for cert-specific helpers; this module is the
generic primitive used by `services/a2a_client.py` after Slice 5.

Slice 1 ships only `send_a2a_message`. Streaming, multi-turn, and
push-notification variants land alongside the per-backend Executors.
"""
from __future__ import annotations

import uuid
from typing import Optional

import httpx
from google.protobuf import json_format, struct_pb2

from a2a.client import ClientConfig, ClientFactory
from a2a.types.a2a_pb2 import Message, Part, Role, SendMessageRequest


def _dict_to_part(payload: dict) -> Part:
    """Wrap a flat dict as a structured `Part` (the SDK's payload primitive).

    A2A `Message`s are composed of `Part`s; structured data goes in a
    `data` part backed by google.protobuf.Struct. JSON-serialisable dicts
    map cleanly via `json_format.ParseDict`.
    """
    s = struct_pb2.Struct()
    json_format.ParseDict(payload, s)
    v = struct_pb2.Value()
    v.struct_value.CopyFrom(s)
    part = Part()
    part.data.CopyFrom(v)
    return part


def _extract_artifact_dict(event) -> Optional[dict]:
    """Pull a structured-Part dict out of any A2A SDK event.

    The SDK yields several event types from `send_message`:

      * Task                       — terminal state with .artifacts
      * Message                    — incremental message
      * TaskStatusUpdateEvent      — status delta
      * TaskArtifactUpdateEvent    — artifact emitted mid-stream

    We're after whatever structured response body the receiver chose
    to emit. Look across the common shapes; return the first
    parseable structured `Part` we find as a dict, or None when the
    event doesn't carry one.
    """
    # With streaming=False the SDK yields a StreamResponse that wraps the payload in a
    # oneof (task / message / status_update / artifact_update). The Task carrying
    # `.artifacts` lives INSIDE that oneof, not directly on `event` — without this
    # unwrap every receiver reply reads as None.
    inner = event
    for _field in ("task", "message", "status_update", "artifact_update"):
        try:
            if event.HasField(_field):
                inner = getattr(event, _field)
                break
        except (ValueError, AttributeError):
            continue

    # Scan candidate locations for a list of Parts.
    candidates: list = []
    artifacts = getattr(inner, "artifacts", None) or []
    for art in artifacts:
        candidates.extend(getattr(art, "parts", None) or [])
    one_artifact = getattr(inner, "artifact", None)
    if one_artifact is not None:
        candidates.extend(getattr(one_artifact, "parts", None) or [])
    if hasattr(inner, "parts"):
        candidates.extend(getattr(inner, "parts", None) or [])

    for part in candidates:
        try:
            if part.HasField("data") and part.data.HasField("struct_value"):
                return json_format.MessageToDict(part.data.struct_value)
        except Exception:  # noqa: BLE001 — defensive against unknown shapes
            continue
    return None


async def send_a2a_message(
    base_url: str,
    *,
    context_id: str,
    task_id: str,
    data: dict,
    auth_header: Optional[str] = None,
    hmac_secret: Optional[str] = None,
    timeout: float = 30.0,
    verify: "bool | str | ssl.SSLContext" = True,
    rpc_url: Optional[str] = None,
    correlation_id: Optional[str] = None,
    change_id: Optional[str] = None,
) -> Optional[dict]:
    """Send a single A2A message to a remote agent and drain the response.

    Args:
        base_url:    Remote agent's host (e.g. `http://cert-agent:8000`
                     or `https://partner.example.com`). The SDK appends
                     `/.well-known/agent-card.json` to discover the RPC
                     endpoint, so pass the bare host — NOT the `/a2a-rpc`
                     subpath.
        context_id:  Conversational thread key — typically a
                     `change_request_id` or cert `run_id`.
        task_id:     Idempotency / correlation id. Reuse across retries
                     so the remote can dedupe.
        data:        JSON-serialisable dict; sent as a structured Part.
        auth_header: Optional `Authorization` header (e.g. "Bearer <jwt>").
                     Plug `fetch_bearer_jwt(...)` from auth.py here when
                     calling cert-agent or any partner that requires JWT.
        hmac_secret: Optional Slice 5 HMAC envelope secret. When set,
                     attaches `X-NPCI-Timestamp / -Nonce / -Signature`
                     headers signing the actual HTTP request body. None
                     = no envelope (back-compat for partners not yet
                     onboarded; the receiver's HMAC middleware will
                     pass the request through with a warn).
        timeout:     httpx client timeout in seconds. 30s is a reasonable
                     default for synchronous task acceptance; longer
                     calls should switch to streaming.
        verify:      httpx TLS-verification value (bool | CA-bundle path |
                     ssl.SSLContext). Default True = full verification.
                     Pass False to skip verification (e.g. a partner with a
                     self-signed cert) or an SSLContext trusting a specific
                     CA. Callers resolve this via `a2a_client.partner_verify`.
        rpc_url:     Explicit JSON-RPC endpoint to send to, BYPASSING agent-card
                     discovery. Default None = normal flow (fetch the card from
                     `base_url/.well-known/...` and use its advertised interface
                     URL). Set this as a fallback when the card's interface URL
                     is unreachable/misconfigured — the card is still fetched by
                     the caller's primary attempt; here we skip it and post
                     straight to `rpc_url`.
        correlation_id: A12 (architecture review Advisory #18, promoted to
                     High per ARCHITECTURE_REVIEW_ACTIONS.md §2.1) — sent as
                     the `X-NPCI-Correlation-ID` HTTP header, in ADDITION to
                     the envelope's own `correlation_id` field, so a
                     partner's edge/proxy logs can correlate a delivery back
                     to the originating authority job/run WITHOUT parsing the
                     JSON-RPC body. None = header omitted (back-compat).
        change_id:   Same rationale, sent as `X-NPCI-Change-ID` — lets an
                     operator grep nginx/partner access logs for a specific
                     change_request_id across every outbound call it made,
                     independent of envelope parsing.

    Returns:
        None — response Tasks are drained but not inspected at this
        layer. Callers that need the response should subscribe via the
        higher-level executor APIs (Slice 5+).
    """
    headers: dict[str, str] = {}
    if auth_header:
        headers["Authorization"] = auth_header
    # A12 — correlation/change headers travel ALONGSIDE the envelope's own
    # correlation_id/change_id fields (belt-and-suspenders): the envelope
    # requires the receiver to parse the JSON-RPC body to correlate a call,
    # while the header lets ANY intermediary (partner's nginx/WAF/APM) tag a
    # request without touching the payload — this is what makes
    # security_architecture_skills.md §3.5 ("globally unique correlation/
    # execution IDs" propagated "across sync calls") and §13.1 actionable at
    # the transport layer, not just inside application logs.
    if correlation_id:
        headers["X-NPCI-Correlation-ID"] = correlation_id
    if change_id:
        headers["X-NPCI-Change-ID"] = change_id

    # Slice 5 outbound signing — register an httpx event hook that
    # signs the actual serialized request bytes the SDK builds. Doing
    # it via the hook (instead of computing the body up front) means
    # we don't need to predict / replicate the SDK's JSON-RPC envelope
    # serialisation; we sign whatever the SDK actually puts on the wire.
    event_hooks: dict[str, list] = {}
    if hmac_secret:
        from .hmac_signer import sign as _hmac_sign

        async def _attach_hmac(request: "httpx.Request") -> None:
            envelope = _hmac_sign(request.content or b"", hmac_secret)
            for k, v in envelope.items():
                request.headers[k] = v

        event_hooks["request"] = [_attach_hmac]

    async with httpx.AsyncClient(
        timeout=timeout,
        headers=headers,
        event_hooks=event_hooks or None,
        verify=verify,
    ) as http:
        factory = ClientFactory(ClientConfig(httpx_client=http, streaming=False))
        if rpc_url:
            # Fallback path: skip agent-card discovery and post the JSON-RPC
            # straight to an explicit endpoint. Used when the partner's card
            # advertises an unreachable/misconfigured interface URL (e.g. an
            # `http://` scheme on the :443 TLS port). Auth header + HMAC hook
            # are still applied via the httpx client above.
            from a2a.client.client_factory import minimal_agent_card
            from a2a.utils.constants import TransportProtocol
            card = minimal_agent_card(rpc_url, transports=[TransportProtocol.JSONRPC])
            client = factory.create(card)
        else:
            client = await factory.create_from_url(base_url)
        # task_id on Message means "continue this EXISTING task on the
        # receiver" per the A2A spec — the server returns
        # "Task X was specified but does not exist" if it doesn't know
        # the id yet. For first sends we omit it so the receiver
        # allocates a new task; the caller's `task_id` arg is the local
        # audit-row id we'll persist regardless. Multi-turn / response
        # flows that need to continue a remote task should be handled
        # by a separate helper that takes the receiver-issued id.
        req = SendMessageRequest(
            message=Message(
                role=Role.ROLE_USER,
                message_id=str(uuid.uuid4()),
                context_id=context_id,
                parts=[_dict_to_part(data)],
            )
        )
        # Slice 25 (admin A2A logs UI) — capture the response artifact
        # so callers can persist it on the audit row. The SDK yields
        # Task / Message / TaskStatusUpdateEvent / TaskArtifactUpdateEvent.
        # We collect EVERY artifact part as a structured dict so the
        # admin UI sees the full receiver reply, then return the last
        # such dict (most receivers send exactly one). Returning None
        # is fine for receivers that don't emit artifacts.
        last_response: Optional[dict] = None
        async for event in client.send_message(req):
            artifact = _extract_artifact_dict(event)
            if artifact is not None:
                last_response = artifact
        return last_response
