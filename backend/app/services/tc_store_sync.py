# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Sync Phase A test cases (from the Excel test-case engine) into cert-agent's
`tc_store`.

Pipeline:
  1. parse_rendered_plan — read the latest 03-rendered_plan.json artifact for a
     change, transform each TestCaseStub into a tc_store dict using the
     field-mapping table from the plan.
  2. fetch_existing_subset — GET cert-agent's current TCs tagged with
     subset=cr-{change_id_short}.
  3. compute_diff — return added / changed / removed.
  4. apply_diff — execute per-row decisions against cert-agent's CRUD,
     write a sync_log entry, return the result.

cert-agent's CRUD endpoints (no auth required inside cert-net):
  GET    {engine_endpoint}/api/certification/test-cases?subset=...
  POST   {engine_endpoint}/api/certification/test-cases
  PUT    {engine_endpoint}/api/certification/test-cases/{tc_id}
  DELETE {engine_endpoint}/api/certification/test-cases/{tc_id}

The plan's field-mapping table is the source of truth for transforms; this
file is its executable form. See:
  C:\\Users\\localadmin\\.claude\\plans\\see-if-certain-functionalities-whimsical-sunrise.md
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.base import generate_uuid
from app.models.cert_sync import CertSimulatorSyncLog
from app.models.phase_c import PartnerAgent, PartnerStatus
from app.core.error_taxonomy import client_safe_detail
from app.services.xml_template_resolver import resolve_for_flow as _resolve_xml_for_flow

logger = logging.getLogger(__name__)


# ── Field mapping ─────────────────────────────────────────────────────────────

def _api_to_flow() -> dict[str, str]:
    """Built-in API name → cert-agent SupportedFlow value, from the active
    pack's ``message_flows``. cert-agent's flow_registry is the authoritative
    source at runtime; this map is the offline/baseline fallback used when the
    engine is unreachable. A pack that declares none contributes no baseline —
    every unmapped stub then surfaces in ``unknown_apis`` for the operator
    rather than being guessed into a flow."""
    from app.core.domain.contract import message_flows_of
    from app.core.domain.registry import get_active_pack

    return dict(message_flows_of(get_active_pack()))


def fetch_engine_api_to_flow(cert_engine: PartnerAgent | None) -> dict[str, str]:
    """Pull the live api_request → flow_code map from cert-agent's flow_registry.

    Used to extend the pack-declared `message_flows` baseline at sync time so
    newly-registered flows are immediately recognised. Returns an empty dict
    if the engine is unreachable
    (caller falls back to the built-in map).
    """
    if not cert_engine:
        return {}
    base = _engine_endpoint(cert_engine)
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f"{base}/api/flows", params={"active_only": "true"})
            r.raise_for_status()
            rows = r.json() or []
            extra: dict[str, str] = {}
            for row in rows:
                code = (row.get("flow_code") or "").strip().upper()
                req = (row.get("api_request") or "").strip()
                resp = (row.get("api_response") or "").strip()
                if code and req:
                    extra[req] = code
                if code and resp:
                    extra[resp] = code
            return extra
    except Exception as e:
        logger.warning("fetch_engine_api_to_flow failed (%s); falling back to built-in map", e)
        return {}


def register_flow_on_engine(cert_engine: PartnerAgent, payload: dict) -> dict:
    """POST a new flow definition to cert-agent's flow_registry.

    Idempotent: if the flow_code already exists, returns the existing row instead
    of erroring. The caller (apply endpoint) typically batches several of these
    before applying TC decisions.
    """
    base = _engine_endpoint(cert_engine)
    code = (payload.get("flow_code") or "").strip().upper()
    if not code:
        raise ValueError("flow_code is required")
    with httpx.Client(timeout=15.0) as client:
        r = client.post(f"{base}/api/flows", json=payload)
        if r.status_code == 409:
            existing = client.get(f"{base}/api/flows/{code}")
            existing.raise_for_status()
            return existing.json()
        r.raise_for_status()
        return r.json()

def _authority_role() -> str:
    """The authority participant's role key (UPI: "NPCI"), UPPER-cased label
    of the pack's ``is_authority`` participant; "" when the pack declares no
    authority."""
    from app.core.domain.contract import participants_of
    from app.core.domain.registry import get_active_pack

    p = next((p for p in participants_of(get_active_pack()) if p.is_authority), None)
    return (p.label or "").upper() if p else ""


def _prefix_role() -> dict[str, str]:
    """Sheet prefix on test_id → fallback role: the active pack's
    ``cert_vocabulary.role_prefixes`` inverted (UPI: PR_ → PAYER_PSP), plus
    the authority's own case prefix (UPI: MT_ → NPCI) from the
    ``authority_case_prefix`` prompt block. Empty for a pack that declares no
    prefixes — an unprefixed tc_id then resolves to role "" and the operator
    sees the gap in the diff modal."""
    from app.core.domain.contract import cert_vocabulary_of
    from app.core.domain.registry import get_active_pack, prompt_block

    vocab = cert_vocabulary_of(get_active_pack())
    out = {prefix: role for role, prefix in vocab.role_prefixes.items()}
    authority_prefix = prompt_block("authority_case_prefix", "")
    authority = _authority_role()
    if authority_prefix and authority:
        out[authority_prefix] = authority
    return out


def _role_initiator() -> dict[str, str]:
    """Role → initiator fallback used when the stub omits `txn_initiated_by`.
    Partner roles (the pack's cert vocabulary) initiate from the participant
    side; the authority's own role is authority-initiated. Anything else
    returns "" so the parse explicitly marks the TC as "indeterminate
    initiator" rather than silently defaulting to one side — the operator sees
    the gap in the diff modal instead of mis-grouped runs.

    NOTE: the VALUES "BANK"/"NPCI" are cert-agent WIRE constants (the engine
    matches them exactly — see workbook_plan.TestCaseStub.txn_initiated_by),
    not display labels; display goes through `initiator_*_label` blocks."""
    from app.core.domain.contract import cert_vocabulary_of
    from app.core.domain.registry import get_active_pack

    vocab = cert_vocabulary_of(get_active_pack())
    out = {role: "BANK" for role in vocab.role_prefixes}
    authority = _authority_role()
    if authority:
        out[authority] = "NPCI"
    return out

# coverage_tag (from Phase A engine) → reference subsets the TC also belongs to
_COVERAGE_SUBSETS = {
    "happy_path": ["Subset-P", "Subset-X"],
    "timeout":    ["Subset-X"],
    "neg_ack":    ["Subset-P"],
    "decline":    ["Subset-P", "Subset-X"],
    "deemed":     ["Subset-DEEMED"],
    "revoke":     ["Subset-P"],
    "partial":    ["Subset-X"],
}


# Regex precedence for expected_resp_code (from the v2.7 spec's prose):
# 1. "Error Code: U03" or "Error Code - Z8" → "U03" / "Z8"
# 2. "response code -00" or "response code 00" → "00"
# Else fall back to expected_status: Success → "00", Deemed → "DEEMED".
_RE_ERROR_CODE = re.compile(r"Error\s*Code\s*[:\-]\s*([A-Z0-9]+)", re.IGNORECASE)
_RE_RESP_CODE  = re.compile(r"response\s*code\s*[-]\s*(\w+)",      re.IGNORECASE)

# Sentinel rows in the v2.7 spec that aren't real test cases
_SENTINEL_RE = re.compile(r"Test\s*case\s*(?:removed|moved\s+from\s+mandatory\s+to\s+extra)", re.IGNORECASE)


@dataclass
class ParsedTC:
    """One row ready to POST/PUT to cert-agent."""
    tc_id: str
    name: str
    flow: str
    expected_resp_code: str
    description: str
    test_data: dict[str, Any]
    request_xml_template: str | None
    enabled: bool
    subsets: list[str]
    role: str
    # Who originates the flow — "NPCI" or "BANK". Cert-agent uses this to label
    # the cert-run batch (authority-initiated vs Bank-initiated) so the operator can
    # tell at a glance which side drove each TC. Sourced from the stub's
    # `txn_initiated_by`; inferred from test_id prefix when missing (PR_/PE_ →
    # BANK, MT_ → NPCI). Empty when neither source resolves.
    initiated_by: str = ""
    # Which side of the txn the PSP/bank participates as — "Payer" or "Payee".
    # Surfaced for filtering; not load-bearing for execution today.
    psp_as: str = ""
    # Ordered request/response steps. Sourced from `rendered.test_steps[]` when
    # Phase A emits it; otherwise auto-derived from the stub's `apis[]` —
    # `["ReqTransfer", "RespTransfer"]` yields a 2-step request/response pair.
    # Each step: {step_no, api, direction, expected_resp_code, role}.
    # The cert-agent dispatcher iterates this list in order so the simulator
    # sends APIs in the sequence the test author wrote, not just `apis[0]`.
    steps: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "tc_id":                self.tc_id,
            "name":                 self.name,
            "flow":                 self.flow,
            "expected_resp_code":   self.expected_resp_code,
            "description":          self.description,
            "test_data":            self.test_data,
            "request_xml_template": self.request_xml_template,
            "enabled":              self.enabled,
            "subsets":              self.subsets,
            "role":                 self.role,
            "initiated_by":         self.initiated_by,
            "psp_as":               self.psp_as,
            "steps":                self.steps,
        }


@dataclass
class ParseResult:
    ok: list[ParsedTC] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)  # [{tc_id, reason}]
    # api_request → list of tc_ids that referenced it but cert-agent has no flow for it.
    # Surfaced in the diff modal so the operator can register the flow inline.
    unknown_apis: dict[str, list[str]] = field(default_factory=dict)
    # Pre-authored flow definitions emitted by Phase A's flow_generator agent.
    # Filtered to apis cert-agent doesn't already know. Each entry is shaped as
    # cert-agent's POST /api/flows body so the modal can hydrate the inline form
    # directly and the apply path forwards verbatim. Empty list when Phase A's
    # artifact lacks the field — falls through to the operator-types-from-scratch path.
    proposed_flow_defs: list[dict] = field(default_factory=list)


@dataclass
class Diff:
    added:   list[dict] = field(default_factory=list)            # TCDicts only present in plan
    changed: list[dict] = field(default_factory=list)            # [{tc_id, before, after, fields_changed}]
    removed: list[dict] = field(default_factory=list)            # TCDicts only present in cert-agent


@dataclass
class ApplyResult:
    applied: int = 0
    skipped: int = 0
    failed:  list[dict] = field(default_factory=list)
    log_id:  str = ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _short(change_id: str) -> str:
    return change_id[:8] if change_id else ""


def _subset_tag(change_id: str) -> str:
    return f"cr-{_short(change_id)}"


def _engine_endpoint(cert_engine: PartnerAgent) -> str:
    if not cert_engine or not cert_engine.endpoint_url:
        raise RuntimeError("No cert_engine partner with endpoint_url is registered")
    return cert_engine.endpoint_url.rstrip("/")


def find_cert_engine_partner(db: Session) -> PartnerAgent | None:
    """Look up the registered cert_engine partner."""
    for p in db.scalars(select(PartnerAgent).where(PartnerAgent.status == PartnerStatus.ACTIVE)).all():
        types = p.partner_type or []
        if isinstance(types, str):
            types = [types]
        if "cert_engine" in types:
            return p
    return None


# ── 1. Parse ──────────────────────────────────────────────────────────────────

def _resolve_artifacts_dir() -> Path:
    """Where the Excel engine writes intermediate JSON artifacts.

    The engine writes to `outputs/excel_engine_artifacts/<job_id>/` by default
    (see backend/app/excel_testcase_engine/orchestrator/graph.py:54-55). On the
    NPCI backend container this is mapped under /app/artifacts/excel_engine/.
    Honour an env override for tests.
    """
    override = os.environ.get("EXCEL_ENGINE_ARTIFACTS_DIR")
    if override:
        return Path(override)
    candidates = [
        Path("/app/artifacts/excel_engine/artifacts"),
        Path("artifacts/excel_engine/artifacts"),
        Path("outputs/excel_engine_artifacts"),
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0]  # default; may not exist yet


def _find_latest_job_id(db: Session, change_id: str) -> str | None:
    """Look up the most recent succeeded cert_test_cases AgentJob for this change.

    Falls back to scanning the artifacts dir for any JSON containing the
    change_id (defensive; agent_jobs table has been observed missing in dev).
    """
    try:
        # Check if agent_jobs table exists
        has_jobs = db.execute(text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='agent_jobs')"
        )).scalar()
        if has_jobs:
            row = db.execute(text("""
                SELECT id FROM agent_jobs
                WHERE module = 'product_kit' AND subtype = 'cert_test_cases'
                  AND change_request_id = :cid
                  AND status = 'succeeded'
                ORDER BY completed_at DESC NULLS LAST, started_at DESC
                LIMIT 1
            """), {"cid": change_id}).first()
            if row:
                return row[0]
    except Exception as e:
        logger.warning("agent_jobs lookup failed (%s); falling back to disk scan", e)

    # Fallback: scan artifacts dir for a directory whose 03-rendered_plan.json
    # mentions this change_id. Slow but defensive.
    art = _resolve_artifacts_dir()
    if not art.is_dir():
        return None
    candidates = sorted(art.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    for d in candidates:
        plan = d / "03-rendered_plan.json"
        if plan.is_file():
            try:
                txt = plan.read_text(encoding="utf-8")
                if change_id in txt:
                    return d.name
            except Exception:
                continue
    return None


def _load_rendered_plan(job_id: str) -> dict | None:
    art = _resolve_artifacts_dir()
    plan_path = art / job_id / "03-rendered_plan.json"
    if not plan_path.is_file():
        return None
    try:
        return json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Failed to read rendered plan %s: %s", plan_path, e)
        return None


def _first_nonblank_line(text_block: str | None) -> str:
    if not text_block:
        return ""
    for line in text_block.splitlines():
        s = line.strip()
        if s:
            return s
    return ""


def _resolve_flow(stub: dict, extra_api_to_flow: dict[str, str] | None = None) -> tuple[str | None, str | None]:
    """Map TestCaseStub → (flow_code, unknown_api).

    Returns:
      (flow_code, None) when a known api maps to a registered flow.
      (None, api_name) when none of the stub's apis match — caller surfaces
        the unknown api in the diff modal so the operator can register it.
    """
    api_map = {**_api_to_flow(), **(extra_api_to_flow or {})}
    apis = stub.get("apis") or []
    first_api: str | None = None
    for api in apis:
        api_clean = (api or "").strip()
        if not api_clean:
            continue
        if first_api is None:
            first_api = api_clean
        flow = api_map.get(api_clean)
        if flow:
            return flow, None
    # Fallback: api_type field or test_id prefix
    api_type = (stub.get("api_type") or "").strip().upper()
    if api_type and api_type in api_map.values():
        return api_type, None
    return None, first_api or (stub.get("api_type") or "")


def _resolve_resp_code(stub: dict, rendered: dict, expected_status: str) -> str | None:
    """Extract expected response code per the regex precedence in the plan."""
    if stub.get("response_code"):
        return str(stub["response_code"]).strip().upper()
    desc = (rendered or {}).get("description_block") or stub.get("scenario_summary") or ""
    m = _RE_ERROR_CODE.search(desc)
    if m:
        return m.group(1).upper()
    m = _RE_RESP_CODE.search(desc)
    if m:
        return m.group(1).upper()
    es = (expected_status or "").strip().lower()
    if es == "success":
        return "00"
    if es == "deemed":
        return "DEEMED"
    return None


def _resolve_role(tc_id: str) -> str:
    for prefix, role in _prefix_role().items():
        if tc_id.startswith(prefix):
            return role
    return ""


def _resolve_initiated_by(stub: dict, role: str) -> str:
    """Normalize stub's `txn_initiated_by` to "NPCI" | "BANK" | "".

    Falls back to the role-derived initiator when the stub omits the field;
    empty when neither source resolves so the operator sees the gap explicitly
    in the diff modal rather than mis-grouped cert-run batches.
    """
    raw = (stub.get("txn_initiated_by") or "").strip().upper()
    if raw in {"NPCI", "BANK"}:
        return raw
    if raw:  # any other non-empty value — flag but don't silently default
        return ""
    return _role_initiator().get(role, "")


def _resolve_psp_as(stub: dict) -> str:
    """Normalize stub's `psp_as` to "Payer" | "Payee" | "" (title-case)."""
    raw = (stub.get("psp_as") or "").strip().lower()
    if raw in {"payer", "payee"}:
        return raw.capitalize()
    return ""


def _api_direction(api_name: str, initiated_by: str) -> str:
    """Direction label for a single step's API name.

    For a Bank-initiated TC, the request originates from the bank → npci_to_bank
    is the response leg; bank_to_npci is the request leg. Mirrored for NPCI-
    initiated. Falls back to npci_to_bank when initiated_by is unknown.
    """
    api = (api_name or "").strip()
    is_request = api.startswith("Req") or api.startswith("req")
    if (initiated_by or "").upper() == "BANK":
        return "bank_to_npci" if is_request else "npci_to_bank"
    return "npci_to_bank" if is_request else "bank_to_npci"


def _resolve_steps(
    stub: dict,
    *,
    flow: str,
    initiated_by: str,
    role: str,
    expected_resp_code: str,
) -> list[dict[str, Any]]:
    """Build the ordered step list cert-agent's dispatcher will iterate.

    Source priority:
      1. `rendered.test_steps[]` when Phase A emits it — used verbatim
         after light normalisation (step_no + missing direction filled in).
      2. Auto-derive from `apis[]` — pair-based when the list is
         `[Req*, Resp*]`; single-step otherwise. This is the common case:
         today's Phase A artifacts ship `apis` but not `test_steps`, so
         honouring that pattern unblocks the feature without an engine
         change.

    Returns at least one step. The cert-agent dispatcher short-circuits on
    the first failure, so step order is load-bearing — request before
    response, then any follow-up step (CHKTXN, REVERSAL).
    """
    rendered = stub.get("rendered") or {}
    explicit = rendered.get("test_steps")
    if isinstance(explicit, list) and explicit:
        out: list[dict[str, Any]] = []
        for i, step in enumerate(explicit, start=1):
            if not isinstance(step, dict):
                continue
            api = (step.get("api") or "").strip()
            if not api:
                continue
            out.append({
                "step_no":            int(step.get("step_no") or i),
                "api":                api,
                "direction":          (step.get("direction") or _api_direction(api, initiated_by)).lower(),
                "expected_resp_code": (step.get("expected_resp_code") or expected_resp_code or "").upper(),
                "role":               step.get("role") or role,
            })
        if out:
            return out

    # Auto-derive from apis[].
    apis = [a for a in (stub.get("apis") or []) if isinstance(a, str) and a.strip()]
    if not apis:
        return []
    out = []
    for i, api in enumerate(apis, start=1):
        out.append({
            "step_no":            i,
            "api":                api.strip(),
            "direction":          _api_direction(api, initiated_by),
            "expected_resp_code": expected_resp_code or "",
            "role":               role,
        })
    return out


def _resolve_test_data(stub: dict) -> dict:
    """Build canonical test_data dict from payer/payee handle types, then
    overlay any stub-supplied `test_data` keys so per-TC values (different
    amounts, VPAs, error codes) reach the simulator instead of every TC
    sending the same defaults.
    """
    payer_handle = (stub.get("payer_handle") or "").strip()
    payee_handle = (stub.get("payee_handle") or "").strip()
    td: dict[str, Any] = {
        "payer_vpa":     "test@npci",
        "payee_vpa":     "merchant@npci",
        "amount":        "1.00",
        "currency":      "INR",
    }
    if "ifsc" in payer_handle.lower() or "a/c" in payer_handle.lower():
        td.update({"ifsc": "SBIN0001234", "account_type": "SAVINGS", "account_number": "0000000000000001"})
    if "mobile" in payee_handle.lower():
        td["mobile_number"] = "9000000001"
    overrides = stub.get("test_data") or {}
    if isinstance(overrides, dict):
        for k, v in overrides.items():
            if v is None or v == "":
                continue
            td[str(k)] = v
    return td


def _resolve_subsets(change_id: str, coverage_tag: str) -> list[str]:
    primary = _subset_tag(change_id)
    extra = _COVERAGE_SUBSETS.get((coverage_tag or "").strip().lower(), [])
    seen = set()
    out = []
    for s in [primary, *extra]:
        if s not in seen:
            out.append(s)
            seen.add(s)
    return out


def _is_sentinel(rendered: dict, stub: dict) -> bool:
    blob = " ".join([
        (rendered or {}).get("details_block") or "",
        (rendered or {}).get("description_block") or "",
        stub.get("scenario_summary") or "",
    ])
    return bool(_SENTINEL_RE.search(blob))


def _stub_to_parsed(
    stub: dict,
    change_id: str,
    extra_api_to_flow: dict[str, str] | None = None,
) -> tuple[ParsedTC | None, str | None, str | None]:
    """Apply the field-mapping table.

    Returns (parsed, skip_reason, unknown_api):
      (ParsedTC, None, None)        — success
      (None, reason, None)          — skipped for non-flow reason (no rendered, sentinel, no resp_code)
      (None, reason, "ReqXyz")      — skipped because no flow matches the stub's apis;
                                      caller collects unknown_api into the diff result so
                                      the modal can prompt for flow registration.
    """
    raw_tc_id = (stub.get("test_id") or "").strip()
    if not raw_tc_id:
        return None, "missing test_id", None
    tc_id = f"{_short(change_id)}-{raw_tc_id}"

    rendered = stub.get("rendered") or {}
    if not rendered:
        return None, "no rendered output (Writer not run)", None

    if _is_sentinel(rendered, stub):
        return None, "sentinel row (Test case removed / moved)", None

    flow, unknown_api = _resolve_flow(stub, extra_api_to_flow)
    if not flow:
        return None, f"unknown flow (apis={stub.get('apis')!r}, api_type={stub.get('api_type')!r})", unknown_api

    expected_status = stub.get("expected_status") or ""
    code = _resolve_resp_code(stub, rendered, expected_status)
    if not code:
        return None, f"could not extract expected_resp_code (status={expected_status})", None

    name = (
        _first_nonblank_line(rendered.get("details_block"))
        or (stub.get("scenario_summary") or "")[:100].strip()
        or f"TC {raw_tc_id}"
    )

    description = (rendered.get("description_block") or "").strip()[:2000]

    role = _resolve_role(raw_tc_id)
    initiated_by = _resolve_initiated_by(stub, role)
    psp_as = _resolve_psp_as(stub)
    steps = _resolve_steps(
        stub,
        flow=flow,
        initiated_by=initiated_by,
        role=role,
        expected_resp_code=code,
    )

    catalog_xml, _ = _resolve_xml_for_flow(flow)

    return ParsedTC(
        tc_id=tc_id,
        name=name,
        flow=flow,
        expected_resp_code=code,
        description=description,
        test_data=_resolve_test_data(stub),
        request_xml_template=catalog_xml,
        enabled=True,
        subsets=_resolve_subsets(change_id, stub.get("coverage_tag") or ""),
        role=role,
        initiated_by=initiated_by,
        psp_as=psp_as,
        steps=steps,
    ), None, None


def parse_rendered_plan(
    change_id: str,
    db: Session,
    cert_engine: PartnerAgent | None = None,
) -> ParseResult:
    """Read latest 03-rendered_plan.json and produce a ParseResult.

    If `cert_engine` is supplied, the engine's live flow_registry is queried so
    runtime-registered flows extend the pack-declared `message_flows` baseline. Stubs whose
    api_request still doesn't match anything are not silently dropped — they
    accumulate in `result.unknown_apis` so the diff modal can prompt the operator
    to register the missing flow inline.
    """
    job_id = _find_latest_job_id(db, change_id)
    if not job_id:
        return ParseResult(skipped=[{"tc_id": "*", "reason": "no Phase A cert_test_cases run found for this change"}])

    plan = _load_rendered_plan(job_id)
    if plan is None:
        return ParseResult(skipped=[{"tc_id": "*", "reason": f"rendered plan not found (job_id={job_id})"}])

    stubs = plan.get("test_cases") or plan.get("stubs") or []
    if not stubs:
        return ParseResult(skipped=[{"tc_id": "*", "reason": "rendered plan has no test_cases"}])

    extra = fetch_engine_api_to_flow(cert_engine) if cert_engine else {}

    result = ParseResult()
    for stub in stubs:
        parsed, reason, unknown_api = _stub_to_parsed(stub, change_id, extra)
        tc_id_raw = stub.get("test_id") or "?"
        if parsed:
            result.ok.append(parsed)
        else:
            result.skipped.append({"tc_id": tc_id_raw, "reason": reason or "unknown"})
            if unknown_api:
                result.unknown_apis.setdefault(unknown_api, []).append(tc_id_raw)

    # Phase A's flow_generator agent emits `flow_definitions` at the top level for
    # any new UPI api in the test-case batch. Filter to apis cert-agent doesn't
    # already know — registered ones don't need re-registering.
    raw_flow_defs = plan.get("flow_definitions") or []
    if raw_flow_defs:
        known_apis = set(_api_to_flow()) | set(extra)
        for fd in raw_flow_defs:
            api_req = (fd.get("api_request") or "").strip()
            if api_req and api_req not in known_apis:
                # Pass through verbatim; matches cert-agent's POST /api/flows body.
                result.proposed_flow_defs.append(dict(fd))
    return result


# ── 2. Fetch existing ─────────────────────────────────────────────────────────

def fetch_existing_subset(cert_engine: PartnerAgent, change_id: str) -> list[dict]:
    """GET cert-agent's TCs tagged with subset=cr-{change_id_short}."""
    base = _engine_endpoint(cert_engine)
    url = f"{base}/api/certification/test-cases"
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(url, params={"subset": _subset_tag(change_id), "enabled_only": "false"})
            r.raise_for_status()
            return r.json() or []
    except Exception as e:
        logger.error("fetch_existing_subset failed: %s", e)
        return []


# ── 3. Diff ───────────────────────────────────────────────────────────────────

# Fields compared for "changed" detection (excludes timestamps + auto-generated).
_DIFF_FIELDS = (
    "name", "flow", "expected_resp_code", "description", "test_data",
    "subsets", "role", "enabled", "initiated_by", "psp_as", "steps",
)


def _normalize_for_diff(d: dict) -> dict:
    """Strip volatile fields before comparing existing vs planned."""
    return {k: d.get(k) for k in _DIFF_FIELDS}


def compute_diff(planned: list[ParsedTC], existing: list[dict]) -> Diff:
    by_id_existing = {e.get("tc_id"): e for e in existing if e.get("tc_id")}
    by_id_planned  = {p.tc_id: p for p in planned}

    out = Diff()

    # added: in planned, not in existing
    for tc_id, p in by_id_planned.items():
        if tc_id not in by_id_existing:
            out.added.append(p.to_dict())

    # removed: in existing, not in planned
    for tc_id, e in by_id_existing.items():
        if tc_id not in by_id_planned:
            out.removed.append({k: e.get(k) for k in ("tc_id", "name", "flow", "expected_resp_code", "subsets", "role")})

    # changed: in both, fields differ
    for tc_id, p in by_id_planned.items():
        e = by_id_existing.get(tc_id)
        if not e:
            continue
        before = _normalize_for_diff(e)
        after  = _normalize_for_diff(p.to_dict())
        if before != after:
            fields_changed = [f for f in _DIFF_FIELDS if before.get(f) != after.get(f)]
            out.changed.append({
                "tc_id": tc_id,
                "before": before,
                "after":  after,
                "fields_changed": fields_changed,
            })

    return out


# ── 4. Apply ──────────────────────────────────────────────────────────────────

def _safe_sync_failure(
    tc_id: str,
    action: str,
    *,
    status: int | None = None,
    upstream_body: str | None = None,
    exc: BaseException | None = None,
) -> dict:
    """Build one entry for `apply_diff`'s `failed` list.

    SCR #6. This list has TWO client-visible destinations, which is why the
    scrub belongs here rather than at either call site:

      1. `POST /changes/{id}/cert-simulator/apply` returns it directly as
         `response["failed"]`;
      2. it is persisted into `CertSimulatorSyncLog.summary` a few lines below
         and then re-served **indefinitely** by
         `GET /changes/{id}/cert-simulator/log`.

    The two leaking inputs were:

      * `str(e)` from a broad handler wrapping `httpx` calls — internal
        hostnames, ports and TLS internals;
      * `r.text[:240]`, the cert-engine's **raw response body**. A 500 from
        that service can be an HTML error page or its own stack trace, which
        we would store and replay to any admin who opens the log. `status`
        already tells the caller what went wrong; the body does not belong in
        our response.

    `tc_id` and `action` are our own values and stay.
    """
    entry: dict = {"tc_id": tc_id, "action": action}
    if status is not None:
        entry["status"] = status
    if exc is not None:
        logger.warning("cert-sync %s failed for tc_id=%s: %s", action, tc_id, exc)
        entry["error"] = client_safe_detail(exc)
    elif upstream_body is not None:
        logger.warning(
            "cert-sync %s rejected for tc_id=%s: status=%s body=%s",
            action, tc_id, status, (upstream_body or "")[:500],
        )
        # Never echo the upstream body. `client_safe_message` would pass plain
        # prose through, but we cannot establish that a remote service's body
        # IS prose, so this is a fixed label.
        entry["error"] = "the certification engine rejected this test case"
    else:
        entry["error"] = "the certification engine rejected this test case"
    return entry


def apply_diff(
    cert_engine: PartnerAgent,
    change_id: str,
    parsed_by_id: dict[str, ParsedTC],
    decisions: list[dict],
    db: Session,
    actor_user_id: str | None = None,
) -> ApplyResult:
    """Execute per-row decisions against cert-agent's CRUD."""
    base = _engine_endpoint(cert_engine)
    crud_root = f"{base}/api/certification/test-cases"

    result = ApplyResult()
    failed: list[dict] = []

    with httpx.Client(timeout=30.0) as client:
        for d in decisions or []:
            tc_id = d.get("tc_id")
            action = (d.get("action") or "skip").strip().lower()
            if not tc_id or action == "skip":
                result.skipped += 1
                continue

            try:
                if action == "add":
                    p = parsed_by_id.get(tc_id)
                    if not p:
                        failed.append({"tc_id": tc_id, "action": action, "error": "not in planned set"})
                        continue
                    r = client.post(crud_root, json=p.to_dict())
                    if r.status_code in (200, 201):
                        result.applied += 1
                    else:
                        failed.append(_safe_sync_failure(tc_id, action, status=r.status_code, upstream_body=r.text))

                elif action == "update":
                    p = parsed_by_id.get(tc_id)
                    if not p:
                        failed.append({"tc_id": tc_id, "action": action, "error": "not in planned set"})
                        continue
                    payload = p.to_dict()
                    payload.pop("tc_id", None)  # tc_id is the path param
                    r = client.put(f"{crud_root}/{tc_id}", json=payload)
                    if r.status_code == 200:
                        result.applied += 1
                    else:
                        failed.append(_safe_sync_failure(tc_id, action, status=r.status_code, upstream_body=r.text))

                elif action == "delete":
                    r = client.delete(f"{crud_root}/{tc_id}")
                    if r.status_code in (200, 204):
                        result.applied += 1
                    else:
                        failed.append(_safe_sync_failure(tc_id, action, status=r.status_code, upstream_body=r.text))

                else:
                    failed.append({"tc_id": tc_id, "action": action, "error": f"unknown action '{action}'"})

            except Exception as e:
                failed.append(_safe_sync_failure(tc_id, action, exc=e))

    result.failed = failed

    log = CertSimulatorSyncLog(
        id=generate_uuid(),
        change_request_id=change_id,
        cert_engine_partner_id=cert_engine.id,
        actor_user_id=actor_user_id,
        operation="apply",
        summary={
            "applied": result.applied,
            "skipped": result.skipped,
            "failed": failed,
            "subset": _subset_tag(change_id),
            "decisions_count": len(decisions or []),
        },
    )
    db.add(log)
    db.commit()
    result.log_id = log.id

    logger.info(
        "tc_store_sync apply: change=%s applied=%d skipped=%d failed=%d log=%s",
        change_id, result.applied, result.skipped, len(failed), log.id,
    )
    return result


def write_diff_view_log(
    db: Session,
    *,
    change_id: str,
    cert_engine: PartnerAgent,
    actor_user_id: str | None,
    summary: dict,
) -> str:
    """Persist a diff_view audit row. Returns log_id."""
    log = CertSimulatorSyncLog(
        id=generate_uuid(),
        change_request_id=change_id,
        cert_engine_partner_id=cert_engine.id if cert_engine else None,
        actor_user_id=actor_user_id,
        operation="diff_view",
        summary=summary,
    )
    db.add(log)
    db.commit()
    return log.id
