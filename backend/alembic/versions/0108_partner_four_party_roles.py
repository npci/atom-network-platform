# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""partner_agents: remap entity types → four-party UPI roles

The partner registry's `partner_type` used entity types (bank / psp / tpap / cert_engine).
Those are replaced by the four-party UPI transaction roles the platform reasons about
(payer_psp / payee_psp / remitter / beneficiary), with cert_engine kept as an internal type.

`partner_type` is a JSON list column (NOT a native enum), so this is a pure DATA remap — no
type/DDL change. Mapping applied to every existing row:

    bank → remitter        (account-holding bank; remitter side chosen as the default)
    psp  → payer_psp        (generic PSP defaults to the payer side)
    tpap → payer_psp        (third-party app → payer-side PSP)
    cert_engine → cert_engine (unchanged — internal cert-agent partner)

bank↔(remitter|beneficiary) and psp/tpap↔(payer_psp|payee_psp) are ambiguous without
per-partner intent; the defaults above are a starting point operators can correct in
Admin → Partners. Idempotent (already-migrated values pass through unchanged) and only writes
rows whose value actually changes. Downgrade is a no-op: psp and tpap both fold into payer_psp,
so the original entity type can't be recovered.

Revision ID: 0108
Revises: 0107
"""
from alembic import op
import sqlalchemy as sa

revision = "0108"
down_revision = "0107"
branch_labels = None
depends_on = None

_REMAP = {"bank": "remitter", "psp": "payer_psp", "tpap": "payer_psp"}


def _dedupe(values):
    seen, out = set(), []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "partner_agents" not in insp.get_table_names():
        return
    # Lightweight typed handle so the JSON adapter (de)serializes for both PG json + SQLite text.
    pa = sa.table("partner_agents",
                  sa.column("id", sa.String),
                  sa.column("partner_type", sa.JSON))
    for rid, ptype in bind.execute(sa.select(pa.c.id, pa.c.partner_type)):
        if not isinstance(ptype, list):
            continue
        remapped = _dedupe(_REMAP.get(t, t) for t in ptype)
        if remapped != ptype:
            bind.execute(pa.update().where(pa.c.id == rid).values(partner_type=remapped))


def downgrade() -> None:
    # Irreversible: psp and tpap both fold into payer_psp, so the pre-migration entity type
    # can't be reconstructed. Leaving the four-party roles in place is harmless.
    pass
