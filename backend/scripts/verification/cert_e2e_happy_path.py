"""End-to-end: the real chain on real Postgres. Not a unit test — no SQLite,
no in-memory tables, foreign keys ENFORCED."""
import asyncio, sys, traceback
sys.path.insert(0, "/app")

from app.core.database import SessionLocal
from app.models.base import generate_uuid
from app.models.api_registry import ApiField, ApiMessage
from app.models.change_request import ChangeRequest
from app.models.phase_c import (CertRun, CertRunStatus, CertTestResult,
                                CertTestStatus, PartnerAgent)
from app.services import cert_pack_run, cert_reporting
from app.services.sim_packs.builder import build_baseline_pack
from app.services.sim_packs.diff import diff_packs
from app.services.simulator import resolver, runtime, store

CHG = "e2e-change-0001"
PARTNER = "e2e-partner-001"
OK, FAIL = [], []

def check(name, cond, detail=""):
    (OK if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))

db = SessionLocal()

print("\n[1] seed a real change + partner + registry delta (FKs enforced)")
from app.models.user import User, UserRole
_u = User(id="e2e-user-0001", username="e2e", email="e2e@test.local",
          password_hash="x", full_name="E2E", role=UserRole.ADMIN)
db.add(_u); db.flush()
db.add(ChangeRequest(id=CHG, title="E2E dispute API",
                     initial_prompt="add a dispute API", created_by=_u.id))
db.add(PartnerAgent(id=PARTNER, name="E2E Bank"))
msg = ApiMessage(id=generate_uuid(), api_name="ReqDispute", direction="request",
                 sample_xml='<ReqDispute><Head ver="2.0"/><Txn type="CHARGEBACK"/></ReqDispute>',
                 introduced_by_change_id=CHG)
db.add(msg); db.flush()
db.add_all([
    ApiField(id=generate_uuid(), message_id=msg.id, position=1, xml_tag="ver",
             is_attribute=True, xpath="ReqDispute/Head/@ver", occurrence="1..1",
             mandatory="Y", introduced_by_change_id=CHG),
    ApiField(id=generate_uuid(), message_id=msg.id, position=2, xml_tag="type",
             is_attribute=True, xpath="ReqDispute/Txn/@type", occurrence="1..1",
             mandatory="Y", enum_values=["CHARGEBACK", "PRE_ARB"],
             introduced_by_change_id=CHG),
])
db.commit()
check("registry delta persisted", len(db.get(ApiMessage, msg.id).fields) == 2)

print("\n[2] build + publish the baseline pack from the real registry")
base = build_baseline_pack(db, pack_ref="baseline@e2e", pack=object())
store.save_draft(db, base)
row = store.publish(db, "baseline@e2e", actor="e2e-operator")
check("baseline published", row.status == "published")
check("pack_id is a content address", row.pack_id.startswith("sha256:"))
check("publish recorded the actor", True)
from app.models.sim_pack import SimPackPublication
pub = db.query(SimPackPublication).filter_by(pack_ref="baseline@e2e").one()
check("published_by persisted on real PG", pub.published_by == "e2e-operator", pub.published_by)

print("\n[3] round 1 — a real certification round through the harness")
CAT = [{"case_id": "TC1", "api": "ReqDispute", "initiator": "npci",
        "expected_status": "PASS", "authority_batch": {"expected_rc": "00"}}]
r1 = asyncio.run(cert_pack_run.run_round(
    db, change_id=CHG, partner_id=PARTNER,
    test_data={"case_catalogue": CAT}, dispatch_meta={"dispatched_by": "operator"}))
print("     summary:", {k: r1.get(k) for k in ("status","passed","total","pass","fail","pack_ref")})
check("round 1 certified", r1["passed"] is True, str(r1))
run1 = db.query(CertRun).filter_by(change_request_id=CHG).one()
check("CertRun stamped with pack", run1.pack_ref == f"{CHG}@r1")
check("CertRun stamped with modes", (run1.npci_mode, run1.partner_mode) == ("simulator","simulator"))
check("coverage note persisted as JSON", isinstance(run1.coverage, dict) and "summary" in run1.coverage)
check("evidence recorded (§3.6.3)", "simulators" in run1.coverage.get("evidence",""))
res1 = db.query(CertTestResult).filter_by(cert_run_id=run1.id).all()
check("result rows carry pack + variant", all(r.pack_ref and r.actual_response.get("variant_id") for r in res1))

print("\n[4] the simulator runtime: validation, scenarios, the no-fallback rule")
reply = asyncio.run(runtime.handle(db, body='<ReqDispute><Head ver="2.0"/><Txn type="CHARGEBACK"/></ReqDispute>',
                                   pack=f"{CHG}@r1", tc_id="TC1"))
check("conforming request answered", reply.rc == "00", reply.rc)
check("response names its contract", reply.pack_header.startswith(f"{CHG}@r1 sha256:"))
try:
    asyncio.run(runtime.handle(db, body='<ReqDispute><Txn type="NOPE"/></ReqDispute>', pack=f"{CHG}@r1"))
    check("violating request refused", False, "no refusal raised")
except runtime.SimRefusal as e:
    fields = {v["field"] for v in e.payload.get("violations", [])}
    check("violating request refused 422", e.status == 422)
    check("refusal names the failing enum + missing mandatory",
          "ReqDispute/Txn/@type" in fields and "ReqDispute/Head/@ver" in fields, str(fields))
try:
    asyncio.run(runtime.handle(db, body="<ReqDispute/>", pack="NOPE@1"))
    check("unknown pack refused (never a fallback)", False, "resolved anyway!")
except runtime.SimRefusal as e:
    check("unknown pack refused 400 unknown_pack",
          e.status == 400 and e.payload["error"] == "unknown_pack")

print("\n[5] round 2 + CERT-7 reporting on real data")
r2 = asyncio.run(cert_pack_run.run_round(db, change_id=CHG, partner_id=PARTNER,
                                         test_data={"case_catalogue": CAT}))
check("round 2 ran", r2["run_number"] == 2, str(r2.get("run_number")))
check("cflow continuity", r2["cflow_id"] == r1["cflow_id"])
hist = cert_reporting.round_history(db, r1["cflow_id"])
check("reporting sees both rounds", [h["run_number"] for h in hist] == [1, 2], str(hist))
d2 = cert_reporting.round_diff(db, r1["cflow_id"], 2)
check("round-2 diff computed", d2["previous_run_number"] == 1)
check("no phantom regressions", d2["newly_failing"] == [] and d2["still_failing"] == [], str(d2))
rep = cert_reporting.flow_report(db, r1["cflow_id"])
check("flow report assembles", len(rep["rounds"]) == 2 and len(rep["diffs"]) == 2)

print("\n[6] pack diff (S-5) on the real chain")
d = diff_packs(resolver.resolve(db, "baseline@e2e").content,
               resolver.resolve(db, f"{CHG}@r2").content)
check("diff runs on real packs", isinstance(d, dict) and "apis_changed" in d, str(d)[:120])

print("\n[7] mode axis — application without a trigger URL refuses")
out = asyncio.run(cert_pack_run.run_round(db, change_id=CHG, partner_id=PARTNER,
                                          test_data={"case_catalogue": CAT,
                                                     "modes": {"npci_mode": "application"}}))
check("application mode refuses without trigger", out.get("error") == "no_authority_trigger_url", str(out)[:120])
check("nothing extra persisted", db.query(CertRun).filter_by(change_request_id=CHG).count() == 2)

print("\n[8] empty delta builds NO pack")
from app.services.sim_packs import builder
check("empty delta -> None", builder.build_pack(db, change_id="nonexistent",
      pack_ref="x@1", base_pack_ref="baseline@e2e") is None)

db.close()
print(f"\n{'='*60}\nPASS {len(OK)}   FAIL {len(FAIL)}")
if FAIL:
    print("FAILURES:", FAIL); sys.exit(1)
print("END-TO-END CLEAN")
