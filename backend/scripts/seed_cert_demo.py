"""Seed a demo certification scenario so the new features can be exercised.

Creates an operator login, a change request, a small API-registry delta, a
published baseline pack, and two completed certification rounds — enough for
every new endpoint to return something real:

    /api/sim/packs                      the pack store
    /api/sim/packs/{ref}/effective      the merged contract
    /api/sim/packs/{ref}/diff           what publishing changed
    /api/sim/execute                    the pack-driven simulator
    /api/cert/flows/{cflow}/report      round history + per-round diffs

Idempotent: re-running wipes and recreates the demo rows only. Safe to run
against a dev database; it touches nothing outside the demo ids.
"""
import asyncio
import secrets
import sys

sys.path.insert(0, "/app")

from app.core.database import SessionLocal
from app.models.api_registry import ApiField, ApiMessage
from app.models.base import generate_uuid
from app.models.change_request import ChangeRequest
from app.models.phase_c import (CertRun, CertTestResult, PartnerAgent)
from app.models.sim_pack import SimPackPublication, SimPackRecord
from app.models.user import User, UserRole
from app.services import cert_pack_run
from app.services.sim_packs.builder import build_baseline_pack
from app.services.simulator import store

CHG = "demo-change-0001"
PARTNER = "demo-partner-001"
USER = "demo-operator-01"
BASELINE = "baseline@demo"

db = SessionLocal()

# ── clean previous demo rows (demo ids only) ────────────────────────────────
runs = db.query(CertRun).filter_by(change_request_id=CHG).all()
for run in runs:
    db.query(CertTestResult).filter_by(cert_run_id=run.id).delete()
db.query(CertRun).filter_by(change_request_id=CHG).delete()
for model in (SimPackRecord, SimPackPublication):
    db.query(model).filter(model.pack_ref.like("%demo%")).delete(
        synchronize_session=False)
# Order matters on a real database: cert_case_specs / cert_request_variants
# foreign-key api_fields and api_messages, so the graded scope goes first.
# (SQLite does not enforce this; Postgres does.)
from app.models.phase_c import CertCaseSpec, CertRequestVariant

for model in (CertCaseSpec, CertRequestVariant):
    db.query(model).filter(model.cflow_id.like("CF-%")).delete(
        synchronize_session=False)
db.commit()
msgs = db.query(ApiMessage).filter_by(introduced_by_change_id=CHG).all()
for m in msgs:
    db.query(ApiField).filter_by(message_id=m.id).delete()
db.query(ApiMessage).filter_by(introduced_by_change_id=CHG).delete()
db.query(ChangeRequest).filter_by(id=CHG).delete()
db.query(PartnerAgent).filter_by(id=PARTNER).delete()
db.query(User).filter_by(id=USER).delete()
db.commit()

# ── operator ────────────────────────────────────────────────────────────────
from app.core.security import hash_password

db.add(User(id=USER, username="demo", email="demo@atom.local",
            password_hash=hash_password("demo1234"),
            full_name="Demo Operator", role=UserRole.ADMIN, is_active=True))

# ── change + partner ────────────────────────────────────────────────────────
db.add(ChangeRequest(id=CHG, title="Add ReqDispute escalation API",
                     initial_prompt="Add a dispute-escalation API to the network",
                     created_by=USER))
# A signing secret is REQUIRED, not decoration: startup validation refuses to
# boot when an ACTIVE partner has none, because such a partner would bypass
# HMAC envelope verification on the A2A wire. Seed a real one rather than
# disabling the guard.
db.add(PartnerAgent(id=PARTNER, name="Demo Partner",
                    signing_secret=secrets.token_urlsafe(32),
                    jwt_signing_secret=secrets.token_urlsafe(32)))
db.flush()

# ── the registry delta this change introduces ───────────────────────────────
# The element/attribute names and the enum members below are SELF-CONTAINED
# demo data, not wire values: the pack the simulator validates against is built
# from these very rows (`build_baseline_pack` a few lines down), so nothing
# outside this file has an opinion about them. They may be renamed freely —
# but the sample XML, the xpaths and the enum must be changed TOGETHER, or the
# runtime refuses its own sample.
msg = ApiMessage(
    id=generate_uuid(), api_name="ReqDispute", direction="request",
    sample_xml='<ReqDispute><Head ver="2.0" ts="2026-08-31T00:00:00"/>'
               '<Txn id="TXN001" type="DISPUTE"/>'
               '<Party addr="party@member" amount="100.00"/></ReqDispute>',
    introduced_by_change_id=CHG)
db.add(msg)
db.flush()
db.add_all([
    ApiField(id=generate_uuid(), message_id=msg.id, position=1, xml_tag="ver",
             is_attribute=True, xpath="ReqDispute/Head/@ver",
             occurrence="1..1", mandatory="Y", datatype="String",
             length_rule="Min Length: 1, Max Length: 5",
             introduced_by_change_id=CHG),
    ApiField(id=generate_uuid(), message_id=msg.id, position=2, xml_tag="type",
             is_attribute=True, xpath="ReqDispute/Txn/@type",
             occurrence="1..1", mandatory="Y", datatype="Code",
             enum_values=["DISPUTE", "PRE_ARB", "ARBITRATION"],
             introduced_by_change_id=CHG),
    ApiField(id=generate_uuid(), message_id=msg.id, position=3, xml_tag="id",
             is_attribute=True, xpath="ReqDispute/Txn/@id",
             occurrence="1..1", mandatory="Y", datatype="String",
             pattern_rule="^TXN[0-9]{3,}$", introduced_by_change_id=CHG),
    ApiField(id=generate_uuid(), message_id=msg.id, position=4,
             xml_tag="amount", is_attribute=True,
             xpath="ReqDispute/Party/@amount", occurrence="1..1",
             mandatory="Y", datatype="Numeric",
             length_rule="minInclusive: 1, maxInclusive: 100000",
             introduced_by_change_id=CHG),
    ApiField(id=generate_uuid(), message_id=msg.id, position=5, xml_tag="addr",
             is_attribute=True, xpath="ReqDispute/Party/@addr",
             occurrence="1..1", mandatory="C",
             condition_text="Mandatory when the party is identified by network "
                            "address rather than by member account",
             introduced_by_change_id=CHG),
])
db.commit()
print(f"seeded change {CHG} with 1 API / 5 constrained fields")

# ── baseline pack, built from the registry and published ────────────────────
base = build_baseline_pack(db, pack_ref=BASELINE, pack=object(),
                           available_cases=[])
store.save_draft(db, base, created_by="demo")
row = store.publish(db, BASELINE, actor="demo")
print(f"published {row.pack_ref}  {row.pack_id}")

# ── two certification rounds ────────────────────────────────────────────────
# `initiator` is PINNED, not leakage: "npci" is the authority role token the
# harness itself defaults to (`cert_pack_run.py`) and the value the network
# pack declares under `cert_roles.authority`. Renaming it here would send the
# demo down the partner-initiated leg and the round would stop matching.
CATALOGUE = [
    {"case_id": "TC_DISPUTE_01", "api": "ReqDispute", "initiator": "npci",
     "expected_status": "PASS", "authority_batch": {"expected_rc": "00"}},
]
r1 = asyncio.run(cert_pack_run.run_round(
    db, change_id=CHG, partner_id=PARTNER,
    test_data={"case_catalogue": CATALOGUE},
    dispatch_meta={"dispatched_by": "operator"}))
print("round 1:", {k: r1.get(k) for k in ("status", "total", "pass", "fail")})

r2 = asyncio.run(cert_pack_run.run_round(
    db, change_id=CHG, partner_id=PARTNER,
    test_data={"case_catalogue": CATALOGUE}))
print("round 2:", {k: r2.get(k) for k in ("status", "total", "pass", "fail")})

print(f"""
────────────────────────────────────────────────────────────
  login          demo / demo1234
  change         {CHG}
  cflow          {r1['cflow_id']}
  baseline pack  {BASELINE}
  round packs    {CHG}@r1, {CHG}@r2
────────────────────────────────────────────────────────────""")
db.close()
