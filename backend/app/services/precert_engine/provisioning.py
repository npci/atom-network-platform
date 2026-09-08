# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Provisioning — participant onboarding.

Writes a participant's configuration into precertdb so precert can route certification traffic to it,
and assigns its cert scope (which cert, which subset, which cases). This is the programmatic
form of the manual seed SQL, and the deterministic core behind the A2A
`cert_config_submission -> cert_setup_notification` handshake.

SAFE + idempotent: every write is check-then-insert. It NEVER deletes (the demo seed script's
delete-then-insert is what corrupted state earlier).

precertdb is Postgres (migrated off MariaDB). Ids are NEVER assigned by hand here: MySQL bumps
AUTO_INCREMENT on an explicit-id insert but Postgres does not touch the sequence, so writing our
own id would leave the sequence behind and the next Hibernate insert would collide on the
primary key. Identity columns are left to the database; `tbl_cert_file` (whose id Hibernate
drives from a sequence rather than an identity column) draws from that same sequence.
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg2


@dataclass(frozen=True)
class ParticipantConfig:
    # THIS DATACLASS IS OURS. The precertdb COLUMNS and the partner-sent
    # submission KEYS are not, and both still spell these in one ecosystem's
    # vocabulary — but the translation happens at exactly two boundaries
    # (`configure_participant`/`config_of` below for the SQL, and
    # `cert_agent/setup.py` for the submission dict). Everything downstream —
    # the runner, the orchestrator — reads the neutral names.
    #
    # Two identity levels, and they are not the same thing: the sponsoring
    # institution ("partner") and the service entity operating under it
    # ("participant"). precert keys its routing on the second and its
    # institution record on the first.
    #
    # identity
    partner_name: str
    partner_code: str
    partner_org_id: str
    partner_routing_code: str          # SQL: tbl_bank.bank_ifsc
    participant_name: str
    participant_org_id: str
    participant_code: str
    handler: str
    credential_length: str             # SQL: tbl_psp.mpinlength
    # network — where precert sends the request (the partner's endpoint)
    partner_server_ip: str
    partner_server_port: str
    # security — the partner's HSM public key (base64), used to encrypt the
    # credential block
    hsm_file_b64: str


class NfiniteProvisioner:
    def __init__(self, db: dict | None = None):
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

    def configure_participant(self, cfg: ParticipantConfig) -> list[str]:
        """Ensure the tbl_bank / tbl_psp / tbl_cert_file rows exist. Returns what changed.

        THE TABLE AND COLUMN NAMES BELOW ARE PINNED — precertdb is an external
        datastore we do not own. This method is one of the two boundaries where
        `ParticipantConfig`'s neutral field names are mapped onto them.
        """
        did: list[str] = []
        con = psycopg2.connect(**self._db)
        try:
            cur = con.cursor()
            if not self._exists(cur, "tbl_bank", "bank_code", cfg.partner_code):
                # id omitted on purpose — it is an identity column; see the module docstring.
                cur.execute(
                    "INSERT INTO tbl_bank (bank_code, bank_name, bank_org_id, bank_ifsc) "
                    "VALUES (%s,%s,%s,%s)",
                    (cfg.partner_code, cfg.partner_name, cfg.partner_org_id,
                     cfg.partner_routing_code))
                did.append(f"tbl_bank +{cfg.partner_code}")
            if not self._exists(cur, "tbl_psp", "psp_organization_id", cfg.participant_org_id):
                cur.execute(
                    "INSERT INTO tbl_psp (bank_name, bank_code, bank_organization_id, psp_name, "
                    "bank_server_ip, bank_server_port, handler, psp_code, psp_organization_id, "
                    "active, ppi_wallet, mpinlength) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'Y','N',%s)",
                    (cfg.partner_name, cfg.partner_code, cfg.partner_org_id, cfg.participant_name,
                     cfg.partner_server_ip, cfg.partner_server_port, cfg.handler,
                     cfg.participant_code, cfg.participant_org_id, cfg.credential_length))
                did.append(f"tbl_psp +{cfg.participant_org_id} -> routes to "
                           f"{cfg.partner_server_ip}:{cfg.partner_server_port}")
            if not self._exists(cur, "tbl_cert_file", "psp_organization_id", cfg.participant_org_id):
                # tbl_cert_file.id is NOT an identity column — Hibernate allocates it from
                # tbl_cert_file_seq, so draw from the same sequence rather than MAX(id)+1.
                cur.execute(
                    "INSERT INTO tbl_cert_file (id, bank_organization_id, psp_organization_id, hsm_file) "
                    "VALUES (nextval('tbl_cert_file_seq'),%s,%s,%s)",
                    (cfg.partner_org_id, cfg.participant_org_id, cfg.hsm_file_b64))
                did.append(f"tbl_cert_file +{cfg.participant_org_id}")
            con.commit()
        finally:
            con.close()
        return did or ["(already configured — no change)"]

    def assign_scope(self, cfg: ParticipantConfig, cert_name: str, subset: str, cases: list[str],
                     *, cert_round: str = "C", product: str = "the network") -> list[str]:
        """Ensure the cert (tbl_psp_subset), the subset (tbl_psp_subset_link) and the case
        mappings (tbl_psp_subset_testcase) exist for this participant."""
        did: list[str] = []
        con = psycopg2.connect(**self._db)
        try:
            cur = con.cursor()
            cur.execute("SELECT id FROM tbl_psp_subset WHERE bank_org_id=%s AND c_name=%s",
                        (cfg.partner_org_id, cert_name))
            row = cur.fetchone()
            if row:
                cert_id = row[0]
            else:
                cur.execute(
                    "INSERT INTO tbl_psp_subset (bank_org_id, c_name, psp_org_id, psp_name, handler, "
                    "certround, product) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (cfg.partner_org_id, cert_name, cfg.participant_org_id, cfg.participant_name,
                     cfg.handler, cert_round, product))
                # psycopg2 has no cursor.lastrowid — RETURNING is the portable equivalent.
                cert_id = cur.fetchone()[0]
                did.append(f"cert {cert_name}")

            # Scoped by subset_fk, NOT subset_name alone: the link row belongs to ONE
            # cert (subset_fk -> tbl_psp_subset.id). Matching on the name only meant the
            # second participant to request a subset reused the FIRST one's link, silently
            # attaching its cases to the other participant's cert. Latent while a single one
            # was provisioned by hand; live as soon as onboarding runs per A2A config.
            cur.execute("SELECT id FROM tbl_psp_subset_link WHERE subset_name=%s AND subset_fk=%s",
                        (subset, cert_id))
            row = cur.fetchone()
            if row:
                link_id = row[0]
            else:
                cur.execute("INSERT INTO tbl_psp_subset_link (subset_name, subset_fk) "
                            "VALUES (%s,%s) RETURNING id", (subset, cert_id))
                link_id = cur.fetchone()[0]
                did.append(f"subset {subset}")

            for tc in cases:
                cur.execute("SELECT 1 FROM tbl_psp_subset_testcase WHERE test_case=%s AND testcase_fk=%s",
                            (tc, link_id))
                if not cur.fetchone():
                    cur.execute("INSERT INTO tbl_psp_subset_testcase (test_case, testcase_fk) VALUES (%s,%s)",
                                (tc, link_id))
                    did.append(f"case {tc}")
            con.commit()
        finally:
            con.close()
        return did or ["(scope already assigned — no change)"]

    def hsm_of(self, participant_org_id: str) -> str:
        """Read an existing participant's stored HSM cert (handy for demos that reconfigure a known one)."""
        con = psycopg2.connect(**self._db)
        try:
            cur = con.cursor()
            cur.execute("SELECT hsm_file FROM tbl_cert_file WHERE psp_organization_id=%s",
                        (participant_org_id,))
            row = cur.fetchone()
            return row[0] if row else ""
        finally:
            con.close()

    def config_of(self, participant_org_id: str) -> "ParticipantConfig | None":
        """Reconstruct a participant's full config from what's ALREADY provisioned in precertdb —
        so the run uses the real onboarded participant, not a hardcoded identity. Returns None when
        it isn't onboarded (nothing to certify against; onboarding-from-scratch is the
        job of the cert_config_submission A2A round-trip, not this reader).

        The other of the two SQL-name boundaries; see `configure_participant`.
        """
        con = psycopg2.connect(**self._db)
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT bank_name, bank_code, bank_organization_id, psp_name, bank_server_ip, "
                "bank_server_port, handler, psp_code, mpinlength "
                "FROM tbl_psp WHERE psp_organization_id=%s", (participant_org_id,))
            p = cur.fetchone()
            if not p:
                return None
            (partner_name, partner_code, partner_org_id, participant_name,
             ip, port, handler, participant_code, cred_len) = p
            cur.execute("SELECT bank_ifsc FROM tbl_bank WHERE bank_code=%s", (partner_code,))
            b = cur.fetchone()
            cur.execute("SELECT hsm_file FROM tbl_cert_file WHERE psp_organization_id=%s",
                        (participant_org_id,))
            h = cur.fetchone()
            return ParticipantConfig(
                partner_name=partner_name or "", partner_code=partner_code or "",
                partner_org_id=partner_org_id or "",
                partner_routing_code=(b[0] if b else "") or "",
                participant_name=participant_name or "",
                participant_org_id=participant_org_id,
                participant_code=participant_code or "",
                handler=handler or "", credential_length=str(cred_len or ""),
                partner_server_ip=ip or "", partner_server_port=str(port or ""),
                hsm_file_b64=(h[0] if h else "") or "",
            )
        finally:
            con.close()

    def subsets_of(self, partner_org_id: str) -> list[str]:
        """Every subset assigned to this participant in precert — i.e. its actual cert scope."""
        con = psycopg2.connect(**self._db)
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT DISTINCT l.subset_name FROM tbl_psp_subset s "
                "JOIN tbl_psp_subset_link l ON l.subset_fk = s.id "
                "WHERE s.bank_org_id=%s ORDER BY l.subset_name", (partner_org_id,))
            return [r[0] for r in cur.fetchall()]
        finally:
            con.close()

    # -- helpers --
    def _exists(self, cur, table: str, col: str, val: str) -> bool:
        cur.execute(f"SELECT 1 FROM {table} WHERE {col}=%s LIMIT 1", (val,))
        return cur.fetchone() is not None
