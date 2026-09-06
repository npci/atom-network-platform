# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Connector to the real Nfinite simulators (precert + precert-bank-sim).

Given one certification case it:
  1. reads the case's definition (its EXPECTED result + any request hints) from precertdb,
  2. FIRES it at precert's /testcase/execute endpoint,
  3. WAITS for precert to route it to the bank switch and record the outcome in `upihosttxnlog`,
  4. reads the bank's OBSERVED response code and applies the domain pass/fail rule.

Robust to the variety in a real scope:
  - a case with no expected result raises `Unverifiable` (nothing to grade against),
  - a request precert refuses raises `SimulatorRejected` (it needs a message shape we don't
    build yet).
Callers classify these separately from a genuine pass/fail. Deliberately small: stdlib HTTP +
psycopg2 + the pure `verdict` rules.

precertdb is Postgres (migrated off MariaDB). psycopg2 uses the same `%s` pyformat
placeholders PyMySQL did, so the SQL below is unchanged by that move.
"""
from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.request
import uuid
from dataclasses import dataclass, field

import psycopg2

from .verdict import Expectation, ResponseOutcome, ResultStatus, Verdict, decide

logger = logging.getLogger(__name__)

_SUCCESS_CODE = "00"

# Base request fields for the ReqTransfer shape. A case's own `paytrans` hints and any explicit
# overrides are merged on top.
_DEFAULT_BODY = {
    "amount": "100.00", "mpin": "000012",
    "payerName": "Tester2", "payerVpa": "tester@mypsp",
    "ac": "99999999999999", "acType": "SAVINGS", "ifsc": "YYYY9999901",
    "mobile": "919999878898",
    "payeename": "the Authority Tester", "payeeacc": "99999999999999",
    "payeeacctype": "SAVINGS", "payeeifsc": "MYPS0000001", "payeeVpa": "tester2@mypsp",
    "seq": "1",
}
_PAYTRANS_HINTS = ("initmode", "purposemode", "expireValue", "type")


def _build_ssl_context() -> ssl.SSLContext:
    """Build the SSLContext used for the connection to precert.

    VERIFICATION IS MANDATORY. The old `precert_engine_verify_peers=false`
    escape hatch (which set verify_mode=CERT_NONE and accepted ANY
    certificate) has been removed — see CBOM-TLS-CERTNONE-3.

    The precert module was removed (commit b25b4ea) and this engine is
    dormant: `precert_engine_enabled` is False and every NfiniteConnector
    instantiation sits inside `orchestrate_cert_run_precert_engine`, which
    only runs when that flag is on. Reviving the integration requires
    supplying `precert_engine_ca_cert_path` alongside
    `precert_engine_enabled`.

    HOSTNAME VERIFICATION stays off even when verifying. The certificate this
    dialled (`cfg/precert.cer` in the removed module) carried no
    SubjectAlternativeName, and modern Python ignores CN for hostname
    matching, so enabling it would reject every connection regardless of
    truststore. Chain validation is what closes the general MITM hole; the
    hostname residual (CWE-297) needs a reissued certificate and is tracked
    separately rather than blocking this.
    """
    from app.core.config import settings

    ctx = ssl.create_default_context()

    ca_path = (settings.precert_engine_ca_cert_path or "").strip()
    if not ca_path:
        raise RuntimeError(
            "precert_engine_ca_cert_path is not set. The precert simulator uses a "
            "self-signed certificate, so verification needs that certificate on disk: "
            "point PRECERT_ENGINE_CA_CERT_PATH at it (PEM). This is only reachable when "
            "precert_engine_enabled=true — if you are not using the precert engine, "
            "leave it disabled rather than turning verification off."
        )
    ctx.check_hostname = False  # see docstring — no SAN on the cert yet
    ctx.load_verify_locations(cafile=ca_path)
    return ctx


class Unverifiable(Exception):
    """The case carries no expected result (no simvalidator.respVal) — nothing to grade against."""


class SimulatorRejected(Exception):
    """precert refused the request — the case needs a message shape this connector doesn't build yet."""


@dataclass(frozen=True)
class CaseSpec:
    psp: str
    subset: str
    cert: str
    tc: str
    certgroup: str
    api: str = "ReqTransfer"
    ver: str = "2.0"
    overrides: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CaseResult:
    tc: str
    txnid: str
    expected: Expectation
    observed: ResponseOutcome
    verdict: Verdict
    precert_review: str | None       # precert's own pass/fail — a cross-check, not the source of truth
    # The exchange as the simulator recorded it (upihosttxnlog.request/response,
    # full XML) — what the field-level assertion engine (CERT-2) grades.
    # Default None keeps every existing constructor valid; absent payloads make
    # field assertions SKIP, never FAIL (C-3's safety rule).
    request_payload: str | None = None
    response_payload: str | None = None


class NfiniteConnector:
    def __init__(self, precert_url: str = "https://localhost:8090", db: dict | None = None):
        self._url = precert_url.rstrip("/")
        # Fallback only — every real call site passes `db` built from Settings
        # (see cert_orchestrator.py). The literal credential that used to sit
        # here was flagged as CWE-798 and there is no reason to keep it: the
        # same values already live in Settings, where they are env-overridable
        # and have exactly one home.
        if db is None:
            from app.core.config import settings
            db = dict(host=settings.precert_engine_db_host,
                      port=settings.precert_engine_db_port,
                      user=settings.precert_engine_db_user,
                      password=settings.precert_engine_db_password,
                      dbname=settings.precert_engine_db_name)
        self._db = db
        self._ssl = _build_ssl_context()

    def run_case(self, case: CaseSpec, *, timeout_s: float = 20.0,
                 expected: Expectation | None = None) -> CaseResult:
        payload = self._read_payload(case.tc, case.certgroup)
        exp = expected or _expected_from(payload, case.tc)  # override: a fixed re-run asserts the corrected outcome
        txnid = self._fire(case, payload)                   # raises SimulatorRejected
        rc, review, req_xml, resp_xml = self._await_outcome(txnid, timeout_s)
        observed = _to_outcome(rc)
        return CaseResult(case.tc, txnid, exp, observed, decide(exp, observed), review,
                          request_payload=req_xml, response_payload=resp_xml)

    def cases_in_subset(self, subset: str) -> list[tuple[str, str]]:
        """Every (test_case, certgroup) mapped to a subset — i.e. the run's whole scope."""
        rows = self._query_all(
            "SELECT DISTINCT t.test_case, uc.certgroup "
            "FROM tbl_psp_subset_testcase t "
            "JOIN tbl_psp_subset_link l ON t.testcase_fk = l.id "
            "JOIN tbl_upi_testcases uc ON uc.name = t.test_case "
            "WHERE l.subset_name = %s ORDER BY t.test_case", (subset,))
        return [(r[0], r[1]) for r in rows]

    def case_details(self, tc: str, certgroup: str) -> dict:
        """Per-case metadata for the A2A spec's cert_setup_notification.case_list[]
        (Appendix B): the assigned API, initiator direction, expected outcome and
        the Authority-owned scenario values — read straight from precertdb."""
        row = self._query_one(
            "SELECT api_name, initiatedby, type, scope, payload "
            "FROM tbl_upi_testcases WHERE name=%s AND certgroup=%s", (tc, certgroup))
        if not row:
            return {"case_id": tc, "sheet": certgroup, "initiator": "npci"}
        api_name, initiatedby, ctype, cscope, payload = row
        p = json.loads(payload) if payload else {}
        respval = (p.get("simvalidator") or {}).get("respVal") or {}
        paytrans = p.get("paytrans") or {}
        authority_batch = {k: paytrans[k] for k in
                     ("initmode", "purposemode", "expireValue", "type", "amount") if k in paytrans}
        if respval.get("rc"):
            authority_batch["expected_rc"] = respval["rc"]
        result = (respval.get("result") or "").upper()
        return {
            "case_id": tc,
            "sheet": certgroup,
            "initiator": (initiatedby or "npci").strip().lower(),
            "api": api_name or "",
            "type": ctype,
            "scope": cscope,
            "expected_status": ("Success" if result == "SUCCESS"
                                else "Failure" if result == "FAILURE" else None),
            "authority_batch": authority_batch,
        }

    # -- steps -----------------------------------------------------------------
    def _read_payload(self, tc: str, certgroup: str) -> dict:
        row = self._query_one(
            "SELECT payload FROM tbl_upi_testcases WHERE name=%s AND certgroup=%s", (tc, certgroup))
        if not row:
            raise LookupError(f"test case {tc}/{certgroup} not found")
        return json.loads(row[0]) if row[0] else {}

    def _fire(self, case: CaseSpec, payload: dict) -> str:
        paytrans = (payload or {}).get("paytrans", {})
        hints = {k: paytrans[k] for k in _PAYTRANS_HINTS if k in paytrans}
        body = {
            "tc": case.tc, "certgroup": case.certgroup, "api": case.api, "ver": case.ver,
            "orgtxnid": ("NPCI" + uuid.uuid4().hex)[:30], **_DEFAULT_BODY, **hints, **case.overrides,
        }
        url = f"{self._url}/testcase/execute/{case.psp}/{case.subset}/{case.cert}/"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, context=self._ssl, timeout=15) as r:
            resp = json.loads(r.read())
        if not resp.get("status") or not resp.get("tid"):
            raise SimulatorRejected(f"{case.tc}: {resp.get('msg')!r}")
        return resp["tid"]

    def _await_outcome(self, txnid: str, timeout_s: float
                       ) -> tuple[str, str | None, str | None, str | None]:
        # `request`/`response` (full exchange XML, already stored per txn) feed
        # the CERT-2 field assertions. Deliberately NOT selected: `apiname` —
        # redundant, `CaseSpec.api` / `case_details()` already carry it.
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            row = self._query_one(
                "SELECT rc, review, request, response FROM upihosttxnlog WHERE txnid=%s",
                (txnid,))
            if row and row[0] is not None:
                return row[0], row[1], row[2], row[3]
            time.sleep(0.5)
        raise TimeoutError(f"no result recorded for txn {txnid} within {timeout_s:.0f}s")

    def _query_one(self, sql: str, params: tuple):
        con = psycopg2.connect(**self._db)
        try:
            with con.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone()
        finally:
            con.close()

    def _query_all(self, sql: str, params: tuple = ()):
        con = psycopg2.connect(**self._db)
        try:
            with con.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        finally:
            con.close()


def _expected_from(payload: dict, tc: str) -> Expectation:
    respval = (payload or {}).get("simvalidator", {}).get("respVal")
    if not respval:
        raise Unverifiable(f"{tc}: no expected result (respVal) to grade against")
    return Expectation(ResultStatus(respval.get("result", "SUCCESS")), respval.get("rc", ""))


def _to_outcome(rc: str) -> ResponseOutcome:
    return ResponseOutcome(ResultStatus.SUCCESS if rc == _SUCCESS_CODE else ResultStatus.FAILURE, rc)
