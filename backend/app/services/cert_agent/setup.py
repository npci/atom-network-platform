# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Turn the bank's ``cert_config_submission`` into a real precert environment.

This is the step the spec calls for and the platform never did. Per Appendix B,
``cert_setup_notification`` fires once "NPCI validated the config, provisioned
the simulator, mapped the suite, generated credentials" — but the cert path only
ever READ precertdb and skipped the whole run when the bank was absent
(``skip_reason="bank … not onboarded / empty scope"``), which surfaced nowhere
because the orchestrator is fire-and-forget.

So: config arrives -> onboard the bank -> map the subset it asked for -> and only
then announce what was actually built.

Everything here is blocking psycopg2 work; callers run it in a thread.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg2

from app.services.precert_engine.provisioning import BankConfig, NfiniteProvisioner

logger = logging.getLogger(__name__)


@dataclass
class ProvisionResult:
    bank: BankConfig
    subset: str
    cases: list[tuple[str, str]]      # (test_case, certgroup)
    changes: list[str]                # what configure_bank / assign_scope actually did
    requested_subset: str | None      # what the bank asked for, before any fallback


def bank_config_from_submission(cfg: dict, *, defaults: dict | None = None) -> BankConfig | None:
    """Map a ``cert_config_submission`` payload onto precert's BankConfig.

    Accepts BOTH shapes on purpose:

      * the FLAT dict the partner sends today (bank_org_id, psp_org_id, …), and
      * the spec's NESTED shape (bank_identity / network / security / roles).

    Phase 5 moves the partner to the nested shape; until then the flat one is
    what actually arrives, and a mapper that only understood the spec would
    break the working demo.

    IMPEDANCE MISMATCH (worth knowing before relying on the nested branch): the
    spec's ``bank_identity`` has no ``bank_code`` and no ``psp_org_id``. precert
    requires both — bank_code is its 3-letter key and psp_org_id is what every
    cert run is addressed to. The nearest spec fields are ``participant_code``
    and ``nbin``, neither of which is the same thing. So the flat extension
    fields stay authoritative where present, and the nested branch is
    best-effort. Reconciling that properly is a spec question, not a code one.

    HSM/SSL/signer are deliberately NOT carried: certificate bodies are never
    inline per spec (only ``cert_ref`` + ``fingerprint_sha256``), and the
    connector never reads ``hsm_file`` — verified, it appears nowhere in
    connector.py — so an empty value onboards fine and transactions are
    unaffected. If MPIN cred-block encryption is ever driven from this, that
    stops being true.

    Returns None when the payload carries too little to identify a PSP.
    """
    cfg = cfg or {}
    d = defaults or {}
    ident = cfg.get("bank_identity") or {}
    net = cfg.get("network") or {}

    def pick(*candidates, fallback=""):
        for c in candidates:
            if c:
                return str(c).strip()
        return fallback

    psp_org_id = pick(cfg.get("psp_org_id"), ident.get("participant_code"),
                      d.get("psp_org_id"))
    if not psp_org_id:
        return None

    handler = pick(cfg.get("handler"), (ident.get("handle") or "").lstrip("@"))

    # `network.port` is a NUMBER in the spec, and the A2A wire is protobuf Struct —
    # which has one numeric type, so 8443 arrives as 8443.0. str() on that yields
    # "8443.0", which precert would store as the bank's switch port and then fail to
    # route to. Coerce through int; a non-numeric value falls through untouched
    # rather than being silently zeroed.
    port = net.get("port")
    if isinstance(port, (int, float)) and float(port).is_integer():
        port = str(int(port))
    elif port is not None:
        port = str(port)

    return BankConfig(
        bank_name=pick(cfg.get("bank_name"), ident.get("bank_name"), fallback="Bank"),
        bank_code=pick(cfg.get("bank_code"), d.get("bank_code")),
        bank_org_id=pick(cfg.get("bank_org_id"), ident.get("org_id"), d.get("bank_org_id")),
        bank_ifsc=pick(cfg.get("bank_ifsc"), ident.get("ifsc")),
        psp_name=pick(cfg.get("psp_name"), fallback="PSP"),
        psp_org_id=psp_org_id,
        psp_code=pick(cfg.get("psp_code"), ident.get("participant_code")),
        handler=handler,
        mpinlength=pick(cfg.get("mpinlength"), fallback="6"),
        bank_server_ip=pick(cfg.get("bank_server_ip"), net.get("host")),
        bank_server_port=pick(cfg.get("bank_server_port"), port),
        # Certificates are out of scope — see the docstring.
        hsm_file_b64="",
    )


def catalogue_cases(db_cfg: dict, subset: str) -> list[tuple[str, str]]:
    """The cases a subset contains in the CATALOGUE — (test_case, certgroup).

    Distinct from ``NfiniteConnector.cases_in_subset``, which reads the ASSIGNED
    scope (tbl_psp_subset_testcase) and so returns nothing before provisioning.
    This reads tbl_subset_testcase, i.e. what the subset is defined to hold —
    which is what ``assign_scope`` needs as input.
    """
    con = psycopg2.connect(**db_cfg)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT u.name, u.certgroup FROM tbl_subset_testcase st "
            "JOIN tbl_upi_testcases u ON u.id = st.testcase_id "
            "WHERE st.subset_id = %s ORDER BY u.name", (subset,))
        return [(r[0], r[1] or "") for r in cur.fetchall()]
    finally:
        con.close()


def subset_exists(db_cfg: dict, subset: str) -> bool:
    con = psycopg2.connect(**db_cfg)
    try:
        cur = con.cursor()
        cur.execute("SELECT 1 FROM tbl_subset WHERE name = %s", (subset,))
        return cur.fetchone() is not None
    finally:
        con.close()


def provision_from_config(
    cfg: dict,
    *,
    db_cfg: dict,
    cert_name: str,
    default_subset: str,
    defaults: dict | None = None,
) -> ProvisionResult | None:
    """Onboard the bank and map its requested suite. Idempotent.

    `configure_bank` and `assign_scope` both no-op on rows that already exist, so
    re-running a cert for an already-onboarded bank changes nothing — which is
    what keeps this safe to put in the middle of a live conversation.

    Subset resolution: the bank's ``requested_subset`` wins (the spec makes the
    subset the bank's choice, not NPCI's). An unknown one falls back to
    `default_subset` with a WARNING rather than aborting — a typo'd subset name
    should not kill a certification, and the fallback is visible in the log and
    in the returned `requested_subset`.

    Returns None if the payload cannot identify a PSP, or if the resolved subset
    has no cases to map.

    HSM cert seeding removed (CBOM-PQC-HSM-CERT-13, docs/adr/ADR-0006 —
    Withdrawn): this used to copy a shared RSA public key into
    `tbl_cert_file.hsm_file` for the deleted Java simulator's
    `CredEncryptorService` to read. Neither that service nor the column it
    fed exists as a live consumer in this repository any more, so
    `bank.hsm_file_b64` is written empty, same as `bank_config_from_submission`
    already leaves it.
    """
    bank = bank_config_from_submission(cfg, defaults=defaults)
    if bank is None:
        logger.warning("cert_setup: config submission carried no psp_org_id — cannot onboard")
        return None

    prov = NfiniteProvisioner(db_cfg)
    changes = prov.configure_bank(bank)

    requested = (cfg or {}).get("requested_subset")
    subset = (requested or "").strip() or default_subset
    if requested and not subset_exists(db_cfg, subset):
        logger.warning(
            "cert_setup: bank %s requested unknown subset %r — falling back to %r",
            bank.psp_org_id, requested, default_subset)
        subset = default_subset

    cases = catalogue_cases(db_cfg, subset)
    if not cases:
        logger.warning("cert_setup: subset %r has no catalogue cases — nothing to map", subset)
        return None

    changes += prov.assign_scope(bank, cert_name=cert_name, subset=subset,
                                 cases=[tc for tc, _ in cases])
    logger.info("cert_setup: psp=%s subset=%s cases=%d changes=%s",
                bank.psp_org_id, subset, len(cases), changes)
    return ProvisionResult(bank=bank, subset=subset, cases=cases,
                           changes=changes, requested_subset=requested)


def simulator_block(*, alias: str, protocol_version: str | None = None,
                    cflow_id: str | None = None,
                    pack_ref: str | None = None) -> dict:
    """The ``simulator`` block of ``cert_setup_notification``.

    ITA I-6 / plan item 3.5: ``endpoint`` is now an **alias declaration**
    (``a2a://<alias>``), never a raw URL. A URL on the wire is an address for
    the partner to dial straight into NPCI's test estate — the exact shape the
    tunnel's §2 refuses; the alias is resolved by the RECEIVING side of each
    tunnelled hop against its own allowlist. This changes the wire (the block
    used to carry ``settings.precert_engine_precert_url`` verbatim), and both
    halves of item 3.5 are written in one edit: ``pack_ref`` appends the
    SIM thread's ``?pack=`` selector (percent-encoded) when a pack is bound.

    ``protocol_version`` defaults from the ACTIVE DOMAIN PACK (``key-version``,
    e.g. ``upi-1.0`` / ``nlln-1.0``) — it used to be the literal ``"UPI-2.x"``,
    which put a payments protocol label on every domain's wire. A caller with
    a real protocol registry can still pass its own string.

    ``credentials_ref`` is an opaque reference by design; there are no simulator
    credentials to hand out in this stack, so it names the cflow rather than
    inventing a vault path that resolves to nothing.
    """
    from urllib.parse import quote

    if protocol_version is None:
        from app.core.domain.registry import get_active_pack

        _pack = get_active_pack()
        protocol_version = (f"{getattr(_pack, 'key', 'unknown')}-"
                            f"{getattr(_pack, 'version', '0')}")

    endpoint = f"a2a://{alias}"
    if pack_ref:
        endpoint += f"?pack={quote(pack_ref, safe='')}"
    return {
        "endpoint": endpoint,
        "protocol_version": protocol_version,
        "credentials_ref": f"cflow://{cflow_id}/sim-credentials" if cflow_id else None,
    }
